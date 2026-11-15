"""Strict local eisy-ui HTTPS tests: no network, no IoX state changes."""

from __future__ import annotations

import socket
import ssl

import pytest

from iox_transport import (
    BoundedStateVariableTransport,
    ConfiguredTargetRegistry,
    EisyUiIoxAuthorization,
    IoxAuthorization,
    IoxTransportError,
)
from native_loopback_tls import (
    NATIVE_LOOPBACK_PEER,
    NativeLoopbackHTTPSConnection,
    native_loopback_connection,
)


class SDK:
    ISY = "ISY"

    def __init__(self):
        self.handler = None
        self.messages = []

    def subscribe(self, kind, handler):
        assert kind == "ISY"
        self.handler = handler

    def send(self, message, target):
        self.messages.append((message, target))


def sdk_data(**changes):
    data = {
        "isy_ip_address": "127.0.0.1",
        "isy_port": 443,
        "isy_username": "device-granted-user",
        "isy_password": "SECRET-SDK-VALUE",
        "isy_https": 1,
    }
    data.update(changes)
    return data


class FakeSocket:
    def __init__(self, peer=NATIVE_LOOPBACK_PEER):
        self.peer = peer
        self.deadlines = []

    def getpeername(self):
        return self.peer

    def settimeout(self, value):
        self.deadlines.append(value)


class FakeResponse:
    status = 200

    def getheader(self, _name):
        return None

    def read(self, _limit):
        return b'<var id="38" type="2"><val>1</val></var>'


class FakeConnection:
    def __init__(self, peer=NATIVE_LOOPBACK_PEER):
        self.sock = FakeSocket(peer)
        self.requests = []
        self.closed = False

    def connect(self):
        pass

    def request(self, method, path, headers):
        self.requests.append((method, path, headers))

    def getresponse(self):
        return FakeResponse()

    def close(self):
        self.closed = True


def transport_for_data(changes=None, peer=NATIVE_LOOPBACK_PEER):
    sdk = SDK()
    authorization = EisyUiIoxAuthorization(sdk)
    targets = ConfiguredTargetRegistry()
    targets.replace({38})
    connection = FakeConnection(peer)
    created = []
    def factory(auth, timeout):
        created.append((auth, timeout))
        return connection
    transport = BoundedStateVariableTransport(
        authorization,
        targets,
        connection_factory=factory,
        native_read_only=True,
    )
    accepted = sdk.handler(sdk_data(**(changes or {})))
    return transport, connection, created, accepted, sdk


def test_valid_sdk_endpoint_allows_only_canonical_get():
    transport, connection, created, accepted, sdk = transport_for_data()
    assert accepted is True
    assert sdk.messages == [({"getIsyInfo": {}}, "system")]
    assert transport.get("/rest/vars/get/2/38") == '<var id="38" type="2"><val>1</val></var>'
    assert len(created) == 1
    assert connection.sock.deadlines == [5.0]
    method, path, headers = connection.requests[0]
    assert (method, path) == ("GET", "/rest/vars/get/2/38")
    assert headers["Authorization"].startswith("Basic ")
    assert "SECRET-SDK-VALUE" not in repr(headers)
    assert connection.closed is True
    with pytest.raises(IoxTransportError, match="not allowed"):
        transport.set("/rest/vars/set/2/38/1")
    with pytest.raises(IoxTransportError, match="not allowed"):
        transport.get("/rest/vars/get/1/38")
    assert len(connection.requests) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"isy_ip_address": "127.0.0.2"},
        {"isy_ip_address": "127.1.1.1"},
        {"isy_ip_address": "::1"},
        {"isy_ip_address": "localhost"},
        {"isy_ip_address": "192.0.2.50"},
        {"isy_port": 80},
        {"isy_port": 444},
        {"isy_port": 8080},
        {"isy_port": True},
        {"isy_https": 0},
        {"isy_https": "true"},
        {"isy_password": None},
        {"isy_username": "", "isy_password": ""},
    ],
)
def test_wrong_sdk_endpoint_never_connects_or_sends_credentials(changes):
    transport, connection, created, _accepted, _sdk = transport_for_data(changes)
    with pytest.raises(IoxTransportError) as error:
        transport.get("/rest/vars/get/2/38")
    assert "SECRET-SDK-VALUE" not in str(error.value)
    assert created == []
    assert connection.requests == []


@pytest.mark.parametrize(
    "peer",
    [
        ("127.0.0.2", 443),
        ("127.0.0.1", 444),
        ("192.0.2.10", 443),
        ("::1", 443, 0, 0),
        None,
    ],
)
def test_unexpected_tls_socket_peer_blocks_basic_auth(peer):
    transport, connection, created, accepted, _sdk = transport_for_data(peer=peer)
    assert accepted is True
    with pytest.raises(IoxTransportError, match="peer") as error:
        transport.get("/rest/vars/get/2/38")
    assert error.value.category == "authorization"
    assert len(created) == 1
    assert connection.requests == []
    assert connection.closed is True


def test_missing_peer_method_is_not_trusted():
    transport, connection, _, _, _ = transport_for_data()
    connection.sock = object()
    with pytest.raises(IoxTransportError, match="peer"):
        transport.get("/rest/vars/get/2/38")
    assert connection.requests == []


def test_native_read_only_opt_in_is_explicit_and_factory_required():
    sdk = SDK()
    auth = EisyUiIoxAuthorization(sdk)
    registry = ConfiguredTargetRegistry()
    registry.replace({38})
    with pytest.raises(ValueError, match="exactly one mode and a local factory"):
        BoundedStateVariableTransport(auth, registry, native_read_only=True)
    with pytest.raises(ValueError, match="exactly one mode and a local factory"):
        BoundedStateVariableTransport(
            auth, registry, native_read_only=1, connection_factory=lambda *_: None
        )


def test_factory_is_exact_local_endpoint_and_disables_cert_identity_by_design():
    auth = IoxAuthorization(
        host="127.0.0.1",
        port=443,
        use_https=True,
        username="private",
        password="private",
        generation=1,
    )
    connection = native_loopback_connection(auth, 2.0)
    assert isinstance(connection, NativeLoopbackHTTPSConnection)
    assert (connection.host, connection.port) == NATIVE_LOOPBACK_PEER
    assert connection._context.verify_mode == ssl.CERT_NONE
    assert connection._context.check_hostname is False
    assert connection._context.minimum_version == ssl.TLSVersion.TLSv1_2
    connection.close()

    bad_auth = IoxAuthorization(
        host="127.0.0.2", port=443, use_https=True,
        username="private", password="private", generation=1,
    )
    with pytest.raises(IoxTransportError, match="endpoint"):
        native_loopback_connection(bad_auth, 2.0)


def test_native_connector_uses_ipv4_numeric_socket_and_refuses_bad_peer(monkeypatch):
    raw_sockets = []
    class Socket:
        def __init__(self):
            self.timeouts = []
            self.closed = False
            self.connected = None
        def settimeout(self, value):
            self.timeouts.append(value)
        def connect(self, target):
            self.connected = target
        def getpeername(self):
            return ("127.0.0.2", 443)
        def close(self):
            self.closed = True
    def socket_factory(family, kind):
        assert family == socket.AF_INET
        assert kind == socket.SOCK_STREAM
        sock = Socket()
        raw_sockets.append(sock)
        return sock

    monkeypatch.setattr("native_loopback_tls.socket.socket", socket_factory)
    auth = IoxAuthorization(
        host="127.0.0.1", port=443, use_https=True,
        username="secret", password="secret", generation=1,
    )
    connection = native_loopback_connection(auth, 1.5)
    with pytest.raises(IoxTransportError, match="TCP peer"):
        connection.connect()
    assert len(raw_sockets) == 1
    assert raw_sockets[0].connected == NATIVE_LOOPBACK_PEER
    assert raw_sockets[0].timeouts == [1.5]
    assert raw_sockets[0].closed is True


def test_verified_home_transport_is_unchanged_in_native_source():
    source = __import__("inspect").getsource(
        BoundedStateVariableTransport._connection
    )
    assert "_verified_loopback_context(self._plugin_directory)" in source
    assert "native_loopback_connection" not in source


def test_native_writable_transport_requires_explicit_mode_and_fixed_factory():
    sdk = SDK()
    authorization = EisyUiIoxAuthorization(sdk)
    targets = ConfiguredTargetRegistry()
    targets.replace({38})
    factory = lambda auth, timeout: FakeConnection()
    for kwargs in (
        {"native_writable": True},
        {"native_read_only": True, "native_writable": True, "connection_factory": factory},
        {"native_writable": 1, "connection_factory": factory},
    ):
        with pytest.raises(ValueError, match="exactly one mode and a local factory"):
            BoundedStateVariableTransport(authorization, targets, **kwargs)


def test_native_writable_rejects_wrong_sdk_endpoint_and_peer_before_basic_auth():
    for changes in (
        {"isy_ip_address": "127.0.0.2"},
        {"isy_ip_address": "::1"},
        {"isy_ip_address": "192.0.2.11"},
        {"isy_port": 444},
        {"isy_https": 0},
    ):
        sdk = SDK()
        auth = EisyUiIoxAuthorization(sdk)
        targets = ConfiguredTargetRegistry()
        targets.replace({38})
        connection = FakeConnection()
        seen = []
        def factory(snapshot, timeout):
            seen.append(snapshot)
            return connection
        transport = BoundedStateVariableTransport(
            auth, targets,
            connection_factory=factory,
            native_writable=True,
        )
        sdk.handler(sdk_data(**changes))
        with pytest.raises(IoxTransportError):
            transport.set("/rest/vars/set/2/38/1")
        assert seen == []
        assert connection.requests == []

    sdk = SDK()
    auth = EisyUiIoxAuthorization(sdk)
    targets = ConfiguredTargetRegistry()
    targets.replace({38})
    connection = FakeConnection(peer=("127.0.0.2", 443))
    transport = BoundedStateVariableTransport(
        auth, targets,
        connection_factory=lambda *_: connection,
        native_writable=True,
    )
    assert sdk.handler(sdk_data()) is True
    with pytest.raises(IoxTransportError, match="peer") as captured:
        transport.set("/rest/vars/set/2/38/1")
    assert captured.value.category == "authorization"
    assert connection.requests == []


def test_enabled_rule_reconciles_with_no_marker_and_never_touches_disabled_target():
    from iox_write_runtime import TrustedStateWriter, WriteActivationGate

    class StateResponse:
        status = 200
        def __init__(self, body):
            self.body = body.encode("utf-8")
        def getheader(self, _name):
            return None
        def read(self, limit):
            return self.body[:limit]

    class StateConnection(FakeConnection):
        value = 0
        def __init__(self):
            super().__init__()
            self.last_path = ""
        def request(self, method, path, headers):
            super().request(method, path, headers)
            self.last_path = path
            if path.startswith("/rest/vars/set/2/38/"):
                self.value = int(path.rsplit("/", 1)[1])
        def getresponse(self):
            if "/rest/vars/set/" in self.last_path:
                return StateResponse('<RestResponse succeeded="true"><status>200</status></RestResponse>')
            return StateResponse(f'<var id="38" type="2"><val>{self.value}</val></var>')

    sdk = SDK()
    auth = EisyUiIoxAuthorization(sdk)
    targets = ConfiguredTargetRegistry()
    connection = StateConnection()
    transport = BoundedStateVariableTransport(
        auth, targets,
        connection_factory=lambda *_: connection,
        native_writable=True,
    )
    now = [1000.0]
    verified_type2 = [False]
    safe_logs = [True]
    gate = WriteActivationGate(
        # Native 'Enabled' is already represented by the configured target set.
        # No file/marker is required (or created) for a native store install.
        marker_check=lambda: bool(targets.snapshot().variable_ids),
        transport_ready=lambda: auth.ready and verified_type2[0],
        configured_ids=lambda: targets.snapshot().variable_ids,
        monotonic_clock=lambda: now[0],
    )
    writer = TrustedStateWriter(
        transport, gate, safe_logging=lambda: safe_logs[0]
    )
    assert sdk.handler(sdk_data()) is True
    assert gate.enabled is False
    assert writer({38: 1}).status == "blocked"
    assert connection.requests == []

    targets.replace({38})  # Owner enabled and saved the configured rule.
    gate.update_runtime(
        started=True,
        poll_requested=True,
        minute_poll_verified=False,
        last_short_poll_at=None,
    )
    assert writer({38: 1}).status == "blocked"
    verified_type2[0] = True
    assert writer({38: 1}).status == "blocked"  # Poll cadence not verified.
    gate.update_runtime(
        started=True,
        poll_requested=True,
        minute_poll_verified=True,
        last_short_poll_at=now[0],
    )
    assert gate.enabled is True
    assert writer({38: 1}).status == "written"
    assert connection.value == 1
    assert [method for method, *_ in connection.requests] == ["GET"] * 3
    assert [path for _method, path, _headers in connection.requests] == [
        "/rest/vars/get/2/38",
        "/rest/vars/set/2/38/1",
        "/rest/vars/get/2/38",
    ]
    assert writer({38: 1}).status == "noop"
    assert [path for _method, path, _headers in connection.requests].count(
        "/rest/vars/set/2/38/1"
    ) == 1

    # Disabling or deleting the rule must not force OFF.
    previous_count = len(connection.requests)
    targets.replace(frozenset())
    assert writer({38: 0}).status == "blocked"
    assert writer({38: 1}).status == "blocked"
    assert connection.value == 1
    assert len(connection.requests) == previous_count

    targets.replace({38})
    safe_logs[0] = False
    assert writer({38: 0}).status == "blocked"
    safe_logs[0] = True
    verified_type2[0] = False
    assert writer({38: 0}).status == "blocked"
    verified_type2[0] = True
    now[0] += 100  # Stale observed polling closes the gate.
    assert writer({38: 0}).status == "blocked"
    now[0] -= 100
    assert writer({39: 1}).status == "blocked"  # Unconfigured ID.
    assert writer({38: 0}).status == "written"
    assert connection.value == 0
    assert len(connection.requests) == previous_count + 3


def test_disabling_one_of_two_rules_removes_only_that_target():
    from iox_write_runtime import TrustedStateWriter, WriteActivationGate

    sdk = SDK()
    auth = EisyUiIoxAuthorization(sdk)
    registry = ConfiguredTargetRegistry()
    registry.replace({38, 39})
    conn = FakeConnection()
    transport = BoundedStateVariableTransport(
        auth, registry,
        connection_factory=lambda *_: conn,
        native_writable=True,
    )
    clock = [50.0]
    gate = WriteActivationGate(
        marker_check=lambda: bool(registry.snapshot().variable_ids),
        transport_ready=lambda: auth.ready,
        configured_ids=lambda: registry.snapshot().variable_ids,
        monotonic_clock=lambda: clock[0],
    )
    gate.update_runtime(
        started=True,
        poll_requested=True,
        minute_poll_verified=True,
        last_short_poll_at=50.0,
    )
    writer = TrustedStateWriter(transport, gate, safe_logging=lambda: True)
    assert sdk.handler(sdk_data()) is True
    # An earlier schedule with both targets is now stale after disabling 39.
    registry.replace({38})
    assert writer({38: 1, 39: 1}).status == "blocked"
    assert writer({39: 0}).status == "blocked"
    assert conn.requests == []
