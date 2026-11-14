import os
import socket
import ssl
import sys
from http.client import HTTPSConnection
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep

import pytest


PLUGIN = Path(__file__).resolve().parents[1] / "plugin"
sys.path.insert(0, str(PLUGIN))

import iox_transport
from iox_transport import (
    BoundedStateVariableTransport,
    ConfiguredTargetRegistry,
    ConfiguredVariablesReadOnlyProbe,
    EisyUiIoxAuthorization,
    IoxTransportError,
    TRUST_ANCHOR_FILE,
    TRUST_DIRECTORY,
    _verified_loopback_context,
)
from eisy_google_calendar.iox_variables import read_state_variable


READ_ZERO = '<var id="38" type="2"><val>0</val></var>'
READ_ONE = '<var id="38" type="2"><val>1</val></var>'
SET_OK = '<RestResponse succeeded="true"><status>200</status></RestResponse>'
TLS_FIXTURES = Path(__file__).parent / "fixtures" / "tls"


@pytest.fixture(scope="module", autouse=True)
def synthetic_tls_test_certificates(tmp_path_factory):
    """Create temporary, synthetic TLS identities; never commit private keys."""
    from datetime import datetime, timedelta, timezone
    from ipaddress import ip_address

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    global TLS_FIXTURES
    original = TLS_FIXTURES
    TLS_FIXTURES = tmp_path_factory.mktemp("synthetic-tls")
    now = datetime.now(timezone.utc)
    cases = [
        ("valid", x509.IPAddress(ip_address("127.0.0.1")), -1, 30),
        ("mismatch", x509.DNSName("example.invalid"), -1, 30),
        ("expired", x509.IPAddress(ip_address("127.0.0.1")), -30, -1),
    ]
    try:
        for name, san, begins, ends in cases:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CalendarRules synthetic test")])
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now + timedelta(days=begins))
                .not_valid_after(now + timedelta(days=ends))
                .add_extension(x509.SubjectAlternativeName([san]), critical=False)
                .sign(key, hashes.SHA256())
            )
            (TLS_FIXTURES / f"{name}.cert").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            (TLS_FIXTURES / f"{name}.key").write_bytes(
                key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )
        yield
    finally:
        TLS_FIXTURES = original


class FakePolyglot:
    ISY = "ISY"

    def __init__(self):
        self.subscriptions = []
        self.sent = []

    def subscribe(self, topic, callback):
        self.subscriptions.append((topic, callback))

    def send(self, message, destination):
        self.sent.append((message, destination))

    @property
    def info_handler(self):
        return dict(self.subscriptions)[self.ISY]


def authorization_info(**changes):
    value = {
        "isy_ip_address": "127.0.0.1",
        "isy_port": 8080,
        "isy_username": "plugin-token",
        "isy_password": "private-value",
        "isy_https": 1,
    }
    value.update(changes)
    return value


class FakeSocket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)


class FakeResponse:
    def __init__(self, body=READ_ZERO.encode(), status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, limit):
        return self.body[:limit]


class FakeConnection:
    def __init__(self, response=None, connect_error=None):
        self.sock = FakeSocket()
        self.response = response or FakeResponse()
        self.connect_error = connect_error
        self.requests = []
        self.closed = False

    def connect(self):
        if self.connect_error is not None:
            raise self.connect_error

    def request(self, method, path, headers):
        self.requests.append((method, path, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def authorized_transport(*, response=None, connect_error=None, **kwargs):
    poly = FakePolyglot()
    authorization = EisyUiIoxAuthorization(poly)
    connection = FakeConnection(response=response, connect_error=connect_error)
    target_ids = kwargs.pop("target_ids", {38})
    targets = ConfiguredTargetRegistry()
    targets.replace(target_ids)
    transport = BoundedStateVariableTransport(
        authorization,
        targets,
        connection_factory=lambda _authorization, _timeout: connection,
        **kwargs,
    )
    poly.info_handler(authorization_info())
    return poly, authorization, connection, transport


def test_authorization_uses_supported_isy_event_without_exposing_secrets():
    poly = FakePolyglot()
    authorization = EisyUiIoxAuthorization(poly)

    assert poly.sent == [({"getIsyInfo": {}}, "system")]
    assert authorization.ready is False
    assert poly.info_handler(authorization_info()) is True
    assert authorization.ready is True
    snapshot = authorization.snapshot()
    assert snapshot.host == "127.0.0.1"
    assert snapshot.port == 8080
    assert "private-value" not in repr(snapshot)
    assert "plugin-token" not in repr(snapshot)


@pytest.mark.parametrize(
    "changes",
    [
        {"isy_ip_address": "192.0.2.236"},
        {"isy_ip_address": "localhost"},
        {"isy_port": 0},
        {"isy_port": True},
        {"isy_https": 0},
        {"isy_https": 1.0},
        {"isy_https": "false"},
        {"isy_username": "", "isy_password": ""},
        {"isy_password": None},
    ],
)
def test_authorization_rejects_nonlocal_or_malformed_connection_data(changes):
    poly = FakePolyglot()
    authorization = EisyUiIoxAuthorization(poly)

    assert poly.info_handler(authorization_info(**changes)) is False
    assert authorization.ready is False
    with pytest.raises(IoxTransportError, match="unavailable"):
        authorization.snapshot()


def test_only_exact_configured_type2_variable_paths_and_binary_values_are_accepted():
    _poly, _authorization, connection, transport = authorized_transport(
        target_ids={38, 104}
    )

    assert transport.get("/rest/vars/get/2/38") == READ_ZERO
    assert transport.get("/rest/vars/get/2/104") == READ_ZERO
    connection.response = FakeResponse(SET_OK.encode())
    assert transport.set("/rest/vars/set/2/38/1") == SET_OK
    assert transport.set("/rest/vars/set/2/104/0") == SET_OK
    for method, path in (
        (transport.get, "/rest/vars/get/2/39"),
        (transport.get, "/rest/vars/get/1/38"),
        (transport.get, "/rest/vars/get/2/038"),
        (transport.get, "/rest/vars/get/2/+38"),
        (transport.get, "/rest/vars/get/2/38?x=1"),
        (transport.get, "/rest/vars/get/2/38/extra"),
        (transport.get, "/rest/vars/get/2/38%2fset%2f2%2f104%2f1"),
        (transport.set, "/rest/vars/set/2/38/2"),
        (transport.set, "/rest/vars/set/1/38/1"),
        (transport.set, "/rest/vars/set/2/39/1"),
        (transport.set, "/rest/nodes"),
        (transport.set, "https://127.0.0.1/rest/vars/set/2/38/1"),
    ):
        with pytest.raises(IoxTransportError, match="not allowed"):
            method(path)


@pytest.mark.parametrize(
    "variable_ids",
    ({0}, {-1}, {True}, {"38"}, None),
)
def test_configured_target_registry_rejects_nonpositive_or_nonnumeric_ids(
    variable_ids,
):
    targets = ConfiguredTargetRegistry()
    with pytest.raises(ValueError, match="configured IoX targets"):
        targets.replace(variable_ids)
    assert targets.snapshot().variable_ids == frozenset()


def test_transport_rejects_a_target_immediately_after_configuration_removes_it():
    poly = FakePolyglot()
    authorization = EisyUiIoxAuthorization(poly)
    connection = FakeConnection()
    targets = ConfiguredTargetRegistry()
    targets.replace({38})
    transport = BoundedStateVariableTransport(
        authorization,
        targets,
        connection_factory=lambda _authorization, _timeout: connection,
    )
    poly.info_handler(authorization_info())

    assert transport.get("/rest/vars/get/2/38") == READ_ZERO
    targets.replace({104})
    with pytest.raises(IoxTransportError, match="not allowed"):
        transport.get("/rest/vars/get/2/38")
    assert len(connection.requests) == 1


def test_target_removed_during_connect_is_never_dispatched():
    connect_entered = Event()
    resume_connect = Event()

    class PausedConnection(FakeConnection):
        def connect(self):
            connect_entered.set()
            if not resume_connect.wait(2):
                raise TimeoutError("test connection was not resumed")

    poly = FakePolyglot()
    authorization = EisyUiIoxAuthorization(poly)
    connection = PausedConnection(response=FakeResponse(SET_OK.encode()))
    targets = ConfiguredTargetRegistry()
    targets.replace({104})
    transport = BoundedStateVariableTransport(
        authorization,
        targets,
        connection_factory=lambda _authorization, _timeout: connection,
    )
    poly.info_handler(authorization_info())
    result = []

    def write_removed_target():
        try:
            transport.set("/rest/vars/set/2/104/1")
        except Exception as error:  # Capture the worker result for assertions.
            result.append(error)

    worker = Thread(target=write_removed_target)
    worker.start()
    assert connect_entered.wait(1)

    replaced = Event()

    def replace_targets():
        targets.replace({38})
        replaced.set()

    replace_worker = Thread(target=replace_targets)
    replace_worker.start()
    assert replaced.wait(1), "configured-target replacement deadlocked"
    replace_worker.join(1)

    revoked = Event()

    def revoke_authorization():
        authorization.revoke()
        revoked.set()

    revoke_worker = Thread(target=revoke_authorization)
    revoke_worker.start()
    assert revoked.wait(1), "authorization revocation deadlocked"
    revoke_worker.join(1)

    resume_connect.set()
    worker.join(2)

    assert worker.is_alive() is False
    assert len(result) == 1
    assert isinstance(result[0], IoxTransportError)
    assert str(result[0]) == "IoX configured targets changed"
    assert connection.requests == []


def test_authorization_revocation_does_not_deadlock_guarded_dispatch():
    request_entered = Event()
    resume_request = Event()

    class PausedRequestConnection(FakeConnection):
        def request(self, method, path, headers):
            request_entered.set()
            if not resume_request.wait(2):
                raise TimeoutError("test request was not resumed")
            super().request(method, path, headers)

    poly = FakePolyglot()
    authorization = EisyUiIoxAuthorization(poly)
    connection = PausedRequestConnection()
    targets = ConfiguredTargetRegistry()
    targets.replace({104})
    transport = BoundedStateVariableTransport(
        authorization,
        targets,
        connection_factory=lambda _authorization, _timeout: connection,
    )
    poly.info_handler(authorization_info())
    result = []

    def read_target():
        try:
            result.append(transport.get("/rest/vars/get/2/104"))
        except Exception as error:  # Capture the worker result for assertions.
            result.append(error)

    worker = Thread(target=read_target)
    worker.start()
    assert request_entered.wait(1)

    revoked = Event()

    def revoke_authorization():
        authorization.revoke()
        revoked.set()

    revoke_worker = Thread(target=revoke_authorization)
    revoke_worker.start()
    assert revoked.wait(1), "authorization revocation deadlocked"
    revoke_worker.join(1)

    resume_request.set()
    worker.join(2)

    assert worker.is_alive() is False
    assert result == [READ_ZERO]
    assert len(connection.requests) == 1
    assert authorization.ready is False


def test_request_uses_basic_auth_deadlines_size_bound_and_no_redirect_logic():
    _poly, _authorization, connection, transport = authorized_transport()

    assert transport.get("/rest/vars/get/2/38") == READ_ZERO
    assert connection.sock.timeouts == [5.0]
    method, path, headers = connection.requests[0]
    assert (method, path) == ("GET", "/rest/vars/get/2/38")
    assert headers["Authorization"].startswith("Basic ")
    assert "private-value" not in str(headers)
    assert headers["Connection"] == "close"
    assert connection.closed is True


def install_test_anchor(plugin_directory, certificate="valid.cert"):
    trust_directory = plugin_directory / TRUST_DIRECTORY
    trust_directory.mkdir(mode=0o700)
    anchor = trust_directory / TRUST_ANCHOR_FILE
    anchor.write_bytes((TLS_FIXTURES / certificate).read_bytes())
    if os.name == "posix":
        trust_directory.chmod(0o700)
        anchor.chmod(0o400)
    return anchor


class LocalTlsServer:
    def __init__(self, certificate="valid.cert", key="valid.key", body=READ_ZERO):
        self.body = body.encode("utf-8")
        self.requests = []
        self.error = None
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(1)
        self.port = self.socket.getsockname()[1]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            TLS_FIXTURES / certificate,
            TLS_FIXTURES / key,
        )
        self.context = context
        self.thread = Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        try:
            self.socket.close()
        finally:
            self.thread.join(timeout=2)

    def _serve(self):
        try:
            raw, _address = self.socket.accept()
            with raw:
                with self.context.wrap_socket(raw, server_side=True) as client:
                    request = bytearray()
                    while b"\r\n\r\n" not in request and len(request) <= 8192:
                        chunk = client.recv(1024)
                        if not chunk:
                            break
                        request.extend(chunk)
                    self.requests.append(bytes(request))
                    response = (
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\n"
                        + f"Content-Length: {len(self.body)}\r\n".encode("ascii")
                        + b"Connection: close\r\n\r\n"
                        + self.body
                    )
                    client.sendall(response)
        except Exception as exc:
            self.error = exc


def live_tls_transport(tmp_path, server, anchor="valid.cert"):
    install_test_anchor(tmp_path, anchor)
    poly = FakePolyglot()
    authorization = EisyUiIoxAuthorization(poly)
    poly.info_handler(authorization_info(isy_port=server.port))
    targets = ConfiguredTargetRegistry()
    targets.replace({38})
    return authorization, BoundedStateVariableTransport(
        authorization,
        targets,
        plugin_directory=tmp_path,
    )


def test_exact_trust_anchor_keeps_certificate_and_hostname_verification(tmp_path):
    install_test_anchor(tmp_path)
    context = _verified_loopback_context(tmp_path)

    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


def test_exact_trust_anchor_permits_verified_variable_38_read(tmp_path):
    with LocalTlsServer() as server:
        _authorization, transport = live_tls_transport(tmp_path, server)
        assert read_state_variable(transport, 38) == 0

    request = server.requests[0]
    assert request.startswith(b"GET /rest/vars/get/2/38 HTTP/")
    assert b"Authorization: Basic " in request
    assert b"private-value" not in request


def test_default_untrusted_context_rejects_local_self_signed_certificate():
    with LocalTlsServer() as server:
        connection = HTTPSConnection(
            "127.0.0.1",
            server.port,
            context=ssl.create_default_context(),
        )
        with pytest.raises(ssl.SSLCertVerificationError):
            connection.connect()
        connection.close()
@pytest.mark.parametrize(
    ("server_certificate", "server_key", "anchor"),
    [
        ("valid.cert", "valid.key", "mismatch.cert"),
        ("mismatch.cert", "mismatch.key", "mismatch.cert"),
        ("expired.cert", "expired.key", "expired.cert"),
    ],
)
def test_wrong_san_mismatched_or_expired_certificates_fail_closed(
    tmp_path, server_certificate, server_key, anchor
):
    with LocalTlsServer(server_certificate, server_key) as server:
        _authorization, transport = live_tls_transport(tmp_path, server, anchor)
        with pytest.raises(IoxTransportError) as captured:
            transport.get("/rest/vars/get/2/38")

    assert captured.value.category == "tls-certificate-validation"
    assert server.requests == []


@pytest.mark.parametrize("case", ["missing", "malformed", "file-symlink", "dir-symlink"])
def test_missing_malformed_or_symlinked_trust_anchor_fails_closed(tmp_path, case):
    if case == "malformed":
        trust_directory = tmp_path / TRUST_DIRECTORY
        trust_directory.mkdir(mode=0o700)
        (trust_directory / TRUST_ANCHOR_FILE).write_text(
            "not a certificate", encoding="ascii"
        )
        if os.name == "posix":
            (trust_directory / TRUST_ANCHOR_FILE).chmod(0o400)
    elif case == "file-symlink":
        trust_directory = tmp_path / TRUST_DIRECTORY
        trust_directory.mkdir(mode=0o700)
        try:
            (trust_directory / TRUST_ANCHOR_FILE).symlink_to(
                TLS_FIXTURES / "valid.cert"
            )
        except OSError:
            pytest.skip("symlinks are unavailable")
    elif case == "dir-symlink":
        target = tmp_path / "alternate"
        target.mkdir()
        try:
            (tmp_path / TRUST_DIRECTORY).symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are unavailable")

    poly = FakePolyglot()
    authorization = EisyUiIoxAuthorization(poly)
    poly.info_handler(authorization_info())
    targets = ConfiguredTargetRegistry()
    targets.replace({38})
    transport = BoundedStateVariableTransport(
        authorization,
        targets,
        plugin_directory=tmp_path,
    )
    with pytest.raises(IoxTransportError) as captured:
        transport.get("/rest/vars/get/2/38")
    assert captured.value.category == "trust-anchor"


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and mode contract")
@pytest.mark.parametrize("target", ["directory", "file"])
def test_group_or_other_access_to_trust_anchor_is_rejected(tmp_path, target):
    anchor = install_test_anchor(tmp_path)
    path = anchor.parent if target == "directory" else anchor
    path.chmod(0o750 if target == "directory" else 0o440)

    with pytest.raises(IoxTransportError) as captured:
        _verified_loopback_context(tmp_path)
    assert captured.value.category == "trust-anchor"


def test_trust_anchor_replacement_between_check_and_open_fails_closed(
    tmp_path, monkeypatch
):
    anchor = install_test_anchor(tmp_path)
    original_open = iox_transport.os.open
    replaced = False

    def replace_before_open(path, flags):
        nonlocal replaced
        if not replaced and Path(path) == anchor:
            replaced = True
            anchor.unlink()
            anchor.write_bytes((TLS_FIXTURES / "mismatch.cert").read_bytes())
            if os.name == "posix":
                anchor.chmod(0o400)
        return original_open(path, flags)

    monkeypatch.setattr(iox_transport.os, "open", replace_before_open)
    with pytest.raises(IoxTransportError) as captured:
        _verified_loopback_context(tmp_path)
    assert captured.value.category == "trust-anchor"


@pytest.mark.parametrize(
    ("response", "message", "category"),
    [
        (FakeResponse(status=302), "unsuccessful status", "http-status"),
        (FakeResponse(status=500), "unsuccessful status", "http-status"),
        (
            FakeResponse(body=b"x" * 33, headers={"Content-Length": "33"}),
            "size limit",
            "response-size",
        ),
        (FakeResponse(body=b"\xff"), "encoding", "response-encoding"),
        (
            FakeResponse(headers={"Content-Length": "invalid"}),
            "length",
            "response-size",
        ),
    ],
)
def test_status_size_and_encoding_fail_closed(response, message, category):
    _poly, _authorization, _connection, transport = authorized_transport(
        response=response,
        max_response_bytes=32,
    )
    with pytest.raises(IoxTransportError, match=message) as captured:
        transport.get("/rest/vars/get/2/38")
    assert captured.value.category == category


def test_unauthorized_response_revokes_transport_until_new_credentials_arrive():
    poly, authorization, connection, transport = authorized_transport(
        response=FakeResponse(status=401)
    )

    with pytest.raises(IoxTransportError, match="rejected") as captured:
        transport.get("/rest/vars/get/2/38")
    assert captured.value.category == "authorization"
    assert authorization.ready is False
    connection.response = FakeResponse(READ_ONE.encode())
    with pytest.raises(IoxTransportError, match="unavailable"):
        transport.get("/rest/vars/get/2/38")
    assert poly.info_handler(authorization_info(isy_password="rotated")) is True
    assert transport.get("/rest/vars/get/2/38") == READ_ONE


def test_connect_timeout_is_sanitized_and_never_returns_credentials():
    _poly, _authorization, _connection, transport = authorized_transport(
        connect_error=TimeoutError("private-value")
    )
    with pytest.raises(IoxTransportError) as captured:
        transport.get("/rest/vars/get/2/38")
    assert str(captured.value) == "IoX request failed: TimeoutError"
    assert captured.value.category == "connection-timeout"
    assert "private" not in str(captured.value)


def test_transport_source_has_no_unverified_tls_or_general_endpoint_client():
    source = (PLUGIN / "iox_transport.py").read_text(encoding="utf-8")
    assert "verify=False" not in source
    assert "CERT_NONE" not in source
    assert "import requests" not in source
    assert '"rest", "vars", "set" if write else "get", "2"' in source
    assert 'parts[6] not in ("0", "1")' in source
    assert "TARGET_VARIABLE_ID" not in source


class ProbeTransport:
    def __init__(self, body=READ_ZERO, entered=None, release=None):
        self.body = body
        self.calls = []
        self.entered = entered
        self.release = release

    def get(self, path):
        self.calls.append(("get", path))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(2)
        if isinstance(self.body, Exception):
            raise self.body
        variable_id = int(path.rsplit("/", 1)[-1])
        if isinstance(self.body, dict):
            result = self.body[variable_id]
            if isinstance(result, Exception):
                raise result
            return result
        return self.body

    def set(self, path):
        self.calls.append(("set", path))
        raise AssertionError("read-only probe must never write")


def wait_for_probe(probe, expected, timeout=2.0):
    deadline = monotonic() + timeout
    status = probe.status
    while monotonic() < deadline:
        status = probe.request()
        if status == expected:
            return status
        sleep(0.005)
    return status


def configured_probe(authorization, transport, *variable_ids):
    targets = ConfiguredTargetRegistry()
    targets.replace(variable_ids or (38,))
    return ConfiguredVariablesReadOnlyProbe(authorization, transport, targets)


def test_read_only_probe_requires_authorization_and_reads_only_variable_38():
    poly = FakePolyglot()
    authorization = EisyUiIoxAuthorization(poly)
    transport = ProbeTransport()
    probe = configured_probe(authorization, transport, 38)
    try:
        probe.start()
        assert probe.status == "unauthorized"
        assert transport.calls == []
        poly.info_handler(authorization_info())
        assert wait_for_probe(probe, "ready") == "ready"
        assert probe.ready is True
        assert transport.calls == [("get", "/rest/vars/get/2/38")]
    finally:
        probe.close()


def test_read_only_probe_validates_every_configured_target_and_revalidates_changes():
    poly = FakePolyglot()
    authorization = EisyUiIoxAuthorization(poly)
    transport = ProbeTransport(
        {
            38: '<var id="38" type="2"><val>0</val></var>',
            104: '<var id="104" type="2"><val>1</val></var>',
            205: '<var id="205" type="2"><val>0</val></var>',
        }
    )
    targets = ConfiguredTargetRegistry()
    targets.replace({38, 104})
    probe = ConfiguredVariablesReadOnlyProbe(authorization, transport, targets)
    try:
        probe.start()
        poly.info_handler(authorization_info())
        assert wait_for_probe(probe, "ready") == "ready"
        assert probe.ready is True
        assert probe.target_ids == frozenset({38, 104})
        assert transport.calls == [
            ("get", "/rest/vars/get/2/38"),
            ("get", "/rest/vars/get/2/104"),
        ]

        probe.configure_targets({205})
        assert probe.ready is False
        assert wait_for_probe(probe, "ready") == "ready"
        assert probe.target_ids == frozenset({205})
        assert transport.calls[-1] == ("get", "/rest/vars/get/2/205")
    finally:
        probe.close()


def test_partial_configured_target_probe_failure_blocks_entire_set():
    poly = FakePolyglot()
    authorization = EisyUiIoxAuthorization(poly)
    transport = ProbeTransport(
        {
            38: '<var id="38" type="2"><val>0</val></var>',
            104: TimeoutError("unavailable"),
        }
    )
    targets = ConfiguredTargetRegistry()
    targets.replace({38, 104})
    probe = ConfiguredVariablesReadOnlyProbe(authorization, transport, targets)
    try:
        probe.start()
        poly.info_handler(authorization_info())
        assert wait_for_probe(probe, "failed") == "failed"
        assert probe.ready is False
        assert probe.failed_variable_id == 104
        assert all(kind == "get" for kind, _path in transport.calls)
    finally:
        probe.close()


def test_out_of_order_probe_completion_is_ignored_after_target_change():
    entered = Event()
    release = Event()
    poly = FakePolyglot()
    authorization = EisyUiIoxAuthorization(poly)
    transport = ProbeTransport(
        {
            38: '<var id="38" type="2"><val>0</val></var>',
            104: '<var id="104" type="2"><val>1</val></var>',
        },
        entered=entered,
        release=release,
    )
    targets = ConfiguredTargetRegistry()
    targets.replace({38})
    probe = ConfiguredVariablesReadOnlyProbe(authorization, transport, targets)
    try:
        probe.start()
        poly.info_handler(authorization_info())
        assert entered.wait(1)
        probe.configure_targets({104})
        assert probe.ready is False
        release.set()
        assert wait_for_probe(probe, "ready") == "ready"
        assert probe.target_ids == frozenset({104})
        assert transport.calls == [
            ("get", "/rest/vars/get/2/38"),
            ("get", "/rest/vars/get/2/104"),
        ]
    finally:
        probe.close()


@pytest.mark.parametrize(
    "body",
    [
        '<var id="39" type="2"><val>0</val></var>',
        '<var id="38" type="1"><val>0</val></var>',
        '<var id="38" type="2"><val>2</val></var>',
        "<malformed",
        TimeoutError("bounded timeout"),
    ],
)
def test_read_only_probe_fails_closed_for_wrong_or_unavailable_variable(body):
    poly = FakePolyglot()
    authorization = EisyUiIoxAuthorization(poly)
    transport = ProbeTransport(body)
    probe = configured_probe(authorization, transport, 38)
    try:
        probe.start()
        poly.info_handler(authorization_info())
        deadline = monotonic() + 2
        observed_failed = False
        while monotonic() < deadline:
            probe.request()
            if probe.status == "failed":
                observed_failed = True
                break
            sleep(0.005)
        assert observed_failed is True
        assert probe.ready is False
        assert all(kind == "get" for kind, _path in transport.calls)
    finally:
        probe.close()


def test_probe_exposes_only_a_fixed_sanitized_failure_category():
    class CategorizedTransport(ProbeTransport):
        def get(self, path):
            self.calls.append(("get", path))
            error = IoxTransportError(
                "must-not-be-published", category="tls-certificate-validation"
            )
            raise error

    poly = FakePolyglot()
    authorization = EisyUiIoxAuthorization(poly)
    transport = CategorizedTransport()
    probe = configured_probe(authorization, transport, 38)
    try:
        probe.start()
        poly.info_handler(authorization_info())
        assert wait_for_probe(probe, "failed") == "failed"
        assert probe.failure_category == "tls-certificate-validation"
        assert "must-not-be-published" not in str(probe.failure_category)
        assert transport.calls == [("get", "/rest/vars/get/2/38")]
    finally:
        probe.close()


def test_out_of_order_probe_completion_is_ignored_after_credential_rotation():
    entered = Event()
    release = Event()
    poly = FakePolyglot()
    authorization = EisyUiIoxAuthorization(poly)
    transport = ProbeTransport(READ_ZERO, entered=entered, release=release)
    probe = configured_probe(authorization, transport, 38)
    try:
        probe.start()
        poly.info_handler(authorization_info(isy_password="first"))
        assert entered.wait(1)
        first_generation = authorization.generation
        poly.info_handler(authorization_info(isy_password="second"))
        assert authorization.generation != first_generation
        release.set()
        deadline = monotonic() + 2
        while monotonic() < deadline and len(transport.calls) < 2:
            probe.request()
            sleep(0.005)
        assert wait_for_probe(probe, "ready") == "ready"
        assert probe.ready is True
        assert len(transport.calls) == 2
    finally:
        probe.close()


def test_stop_during_probe_never_promotes_stale_completion_to_ready():
    entered = Event()
    release = Event()
    poly = FakePolyglot()
    authorization = EisyUiIoxAuthorization(poly)
    transport = ProbeTransport(READ_ZERO, entered=entered, release=release)
    probe = configured_probe(authorization, transport, 38)
    try:
        probe.start()
        poly.info_handler(authorization_info())
        assert entered.wait(1)
        probe.stop()
        release.set()
        sleep(0.02)
        assert probe.request() == "inactive"
        assert probe.ready is False
        assert all(kind == "get" for kind, _path in transport.calls)
    finally:
        probe.close()
