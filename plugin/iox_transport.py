"""Bounded eisy-ui-authorized transport for configured IoX state variables."""

from __future__ import annotations

import base64
import ipaddress
import os
import socket
import ssl
import stat
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from http.client import HTTPSConnection
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Collection, Mapping

from eisy_google_calendar.iox_variables import read_state_variable


CONNECT_TIMEOUT_SECONDS = 2.0
READ_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 32 * 1024
TRUST_DIRECTORY = ".iox-trust"
TRUST_ANCHOR_FILE = "eisyui.cert"
MAX_TRUST_ANCHOR_BYTES = 32 * 1024


FAILURE_LABELS = {
    "authorization": "authorization rejected",
    "connection-refused": "connection refused",
    "connection-timeout": "connection timed out",
    "connection": "connection failed",
    "http-status": "unexpected HTTP status",
    "response-encoding": "response encoding invalid",
    "response-size": "response size invalid",
    "response-validation": "response validation failed",
    "tls": "TLS connection failed",
    "tls-certificate-validation": "TLS certificate validation failed",
    "trust-anchor": "TLS trust anchor invalid",
    "transport": "transport failed",
}


class IoxTransportError(RuntimeError):
    """Sanitized bounded-transport failure."""

    def __init__(self, message: str, *, category: str = "transport"):
        super().__init__(message)
        self.category = category if category in FAILURE_LABELS else "transport"


@dataclass(frozen=True)
class ConfiguredTargetSnapshot:
    """One immutable configured-target generation."""

    variable_ids: frozenset[int]
    generation: int


class ConfiguredTargetRegistry:
    """Thread-safe enabled-rule target set shared by probe, gate, and transport."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._variable_ids: frozenset[int] = frozenset()
        self._generation = 0

    def replace(self, variable_ids: Collection[int]) -> ConfiguredTargetSnapshot:
        try:
            candidate = frozenset(variable_ids)
        except TypeError:
            raise ValueError("configured IoX targets must be a collection") from None
        if any(
            type(variable_id) is not int or variable_id <= 0
            for variable_id in candidate
        ):
            raise ValueError("configured IoX targets must be positive integers")
        with self._lock:
            if candidate != self._variable_ids:
                self._variable_ids = candidate
                self._generation += 1
            return ConfiguredTargetSnapshot(self._variable_ids, self._generation)

    def snapshot(self) -> ConfiguredTargetSnapshot:
        with self._lock:
            return ConfiguredTargetSnapshot(self._variable_ids, self._generation)

    def dispatch_if_current(
        self,
        variable_id: int,
        generation: int,
        dispatch: Callable[[], None],
    ) -> bool:
        """Atomically validate one generation and dispatch its bounded request."""

        with self._lock:
            if (
                generation != self._generation
                or variable_id not in self._variable_ids
            ):
                return False
            dispatch()
            return True


def _fail_trust_anchor() -> IoxTransportError:
    return IoxTransportError(
        "IoX TLS trust anchor is unavailable or invalid",
        category="trust-anchor",
    )


def _protected_trust_anchor(plugin_directory: Path) -> str:
    """Read one fixed, owner-protected certificate without following symlinks."""

    try:
        root = Path(plugin_directory).resolve(strict=True)
        trust_directory = root / TRUST_DIRECTORY
        anchor_path = trust_directory / TRUST_ANCHOR_FILE
        directory_info = trust_directory.lstat()
        anchor_info = anchor_path.lstat()
        if not stat.S_ISDIR(directory_info.st_mode) or stat.S_ISLNK(
            directory_info.st_mode
        ):
            raise _fail_trust_anchor()
        if not stat.S_ISREG(anchor_info.st_mode) or stat.S_ISLNK(anchor_info.st_mode):
            raise _fail_trust_anchor()
        if not 0 < anchor_info.st_size <= MAX_TRUST_ANCHOR_BYTES:
            raise _fail_trust_anchor()
        if os.name == "posix":
            effective_uid = os.geteuid()
            if (
                directory_info.st_uid != effective_uid
                or anchor_info.st_uid != effective_uid
                or directory_info.st_mode & 0o077
                or anchor_info.st_mode & 0o077
            ):
                raise _fail_trust_anchor()

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(anchor_path, flags)
        try:
            opened_info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_info.st_mode)
                or opened_info.st_dev != anchor_info.st_dev
                or opened_info.st_ino != anchor_info.st_ino
                or opened_info.st_size != anchor_info.st_size
            ):
                raise _fail_trust_anchor()
            chunks: list[bytes] = []
            remaining = MAX_TRUST_ANCHOR_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(4096, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
        finally:
            os.close(descriptor)
    except IoxTransportError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise _fail_trust_anchor() from None

    try:
        certificate = data.decode("ascii").strip()
        begin = "-----BEGIN CERTIFICATE-----"
        end = "-----END CERTIFICATE-----"
        if (
            certificate.count(begin) != 1
            or certificate.count(end) != 1
            or not certificate.startswith(begin)
            or not certificate.endswith(end)
            or "PRIVATE KEY" in certificate
        ):
            raise ValueError
        ssl.PEM_cert_to_DER_cert(certificate)
        return certificate + "\n"
    except (UnicodeDecodeError, ValueError):
        raise _fail_trust_anchor() from None


def _verified_loopback_context(plugin_directory: Path) -> ssl.SSLContext:
    """Create a hostname-verifying context that trusts one device certificate."""

    certificate = _protected_trust_anchor(plugin_directory)
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.load_verify_locations(cadata=certificate)
        return context
    except (OSError, ssl.SSLError, ValueError):
        raise _fail_trust_anchor() from None


@dataclass(frozen=True)
class IoxAuthorization:
    """Validated authorization snapshot with secret-free representation."""

    host: str
    port: int
    use_https: bool
    username: str = field(repr=False)
    password: str = field(repr=False)
    generation: int


class EisyUiIoxAuthorization:
    """Receive owner-approved IoX credentials through the SDK ISY event."""

    def __init__(self, polyglot: Any):
        self._polyglot = polyglot
        self._lock = Lock()
        self._authorization: IoxAuthorization | None = None
        self._generation = 0
        self._change_callback: Callable[[], None] | None = None
        self._unauthorized = False
        polyglot.subscribe(polyglot.ISY, self._info)
        polyglot.send({"getIsyInfo": {}}, "system")

    def set_change_callback(self, callback: Callable[[], None]) -> None:
        self._change_callback = callback

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._authorization is not None and not self._unauthorized

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def unauthorized(self) -> bool:
        with self._lock:
            return self._unauthorized

    def snapshot(self) -> IoxAuthorization:
        with self._lock:
            if self._authorization is None or self._unauthorized:
                raise IoxTransportError("IoX authorization is unavailable")
            return self._authorization

    def revoke(self) -> None:
        self._replace(None, unauthorized=True)

    def _info(self, info: Any) -> bool:
        authorization = self._validated(info)
        self._replace(authorization, unauthorized=info is None)
        return authorization is not None

    def _replace(
        self,
        authorization: IoxAuthorization | None,
        *,
        unauthorized: bool,
    ) -> None:
        with self._lock:
            self._generation += 1
            if authorization is not None:
                authorization = IoxAuthorization(
                    host=authorization.host,
                    port=authorization.port,
                    use_https=authorization.use_https,
                    username=authorization.username,
                    password=authorization.password,
                    generation=self._generation,
                )
            self._authorization = authorization
            self._unauthorized = unauthorized
        callback = self._change_callback
        if callable(callback):
            try:
                callback()
            except Exception:
                pass

    @staticmethod
    def _validated(info: Any) -> IoxAuthorization | None:
        if not isinstance(info, Mapping):
            return None
        host = info.get("isy_ip_address")
        port = info.get("isy_port")
        username = info.get("isy_username")
        password = info.get("isy_password")
        https_value = info.get("isy_https")
        try:
            address = ipaddress.ip_address(host)
        except (TypeError, ValueError):
            return None
        if not address.is_loopback:
            return None
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            return None
        if not (
            https_value is True
            or (type(https_value) is int and https_value == 1)
        ):
            return None
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        if not username and not password:
            return None
        if len(username) > 4096 or len(password) > 4096:
            return None
        return IoxAuthorization(
            host=str(address),
            port=port,
            use_https=True,
            username=username,
            password=password,
            generation=0,
        )


class BoundedStateVariableTransport:
    """Exact-path transport restricted to the current configured target set."""

    def __init__(
        self,
        authorization: EisyUiIoxAuthorization,
        targets: ConfiguredTargetRegistry,
        *,
        plugin_directory: Path | None = None,
        connection_factory: Callable[[IoxAuthorization, float], Any] | None = None,
        native_read_only: bool = False,
        native_writable: bool = False,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        read_timeout: float = READ_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        if connect_timeout <= 0 or read_timeout <= 0:
            raise ValueError("IoX timeouts must be positive")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("IoX response limit must be a positive integer")
        if (
            type(native_read_only) is not bool
            or type(native_writable) is not bool
            or (native_read_only and native_writable)
            or (
                (native_read_only or native_writable)
                and not callable(connection_factory)
            )
        ):
            raise ValueError("native loopback transport requires exactly one mode and a local factory")
        # Native GET-only and writable paths share the same exact SDK endpoint
        # and established TLS peer checks. Neither can use remote fallback.
        self._native_loopback = native_read_only or native_writable
        self._native_read_only = native_read_only
        self._native_writable = native_writable
        self._authorization = authorization
        self._targets = targets
        self._plugin_directory = (
            Path(plugin_directory).resolve()
            if plugin_directory is not None
            else None
        )
        self._connection_factory = connection_factory
        self._connect_timeout = float(connect_timeout)
        self._read_timeout = float(read_timeout)
        self._max_response_bytes = max_response_bytes
        self._request_lock = Lock()

    @property
    def ready(self) -> bool:
        return self._authorization.ready

    def get(self, path: str) -> str:
        variable_id = self._validated_path(path, write=False)
        target_snapshot = self._targets.snapshot()
        if variable_id not in target_snapshot.variable_ids:
            raise IoxTransportError("IoX read path is not allowed")
        return self._request(path, variable_id, target_snapshot.generation)

    def set(self, path: str) -> str:
        if self._native_read_only:
            raise IoxTransportError("IoX native write path is not allowed")
        variable_id = self._validated_path(path, write=True)
        target_snapshot = self._targets.snapshot()
        if variable_id not in target_snapshot.variable_ids:
            raise IoxTransportError("IoX write path is not allowed")
        return self._request(path, variable_id, target_snapshot.generation)

    @staticmethod
    def _validated_path(path: str, *, write: bool) -> int:
        if not isinstance(path, str) or len(path) > 128:
            raise IoxTransportError(
                f"IoX {'write' if write else 'read'} path is not allowed"
            )
        parts = path.split("/")
        expected_prefix = ["", "rest", "vars", "set" if write else "get", "2"]
        if parts[:5] != expected_prefix or len(parts) != (7 if write else 6):
            raise IoxTransportError(
                f"IoX {'write' if write else 'read'} path is not allowed"
            )
        variable_text = parts[5]
        if (
            not variable_text.isascii()
            or not variable_text.isdecimal()
            or variable_text.startswith("0")
        ):
            raise IoxTransportError(
                f"IoX {'write' if write else 'read'} path is not allowed"
            )
        variable_id = int(variable_text)
        if variable_id <= 0 or (write and parts[6] not in ("0", "1")):
            raise IoxTransportError(
                f"IoX {'write' if write else 'read'} path is not allowed"
            )
        return variable_id

    def _request(self, path: str, variable_id: int, target_generation: int) -> str:
        wait_limit = self._connect_timeout + self._read_timeout + 1.0
        if not self._request_lock.acquire(timeout=wait_limit):
            raise IoxTransportError("IoX transport is busy")
        connection = None
        try:
            current_targets = self._targets.snapshot()
            if (
                current_targets.generation != target_generation
                or variable_id not in current_targets.variable_ids
            ):
                raise IoxTransportError("IoX configured targets changed")
            authorization = self._authorization.snapshot()
            if self._native_loopback and (
                authorization.host != "127.0.0.1"
                or type(authorization.port) is not int
                or authorization.port != 443
                or authorization.use_https is not True
            ):
                raise IoxTransportError(
                    "IoX native SDK endpoint is unavailable",
                    category="authorization",
                )
            connection = (
                self._connection_factory(authorization, self._connect_timeout)
                if self._connection_factory is not None
                else self._connection(authorization, self._connect_timeout)
            )
            connection.connect()
            sock = getattr(connection, "sock", None)
            if sock is None:
                raise IoxTransportError(
                    "IoX connection is unavailable", category="connection"
                )
            # On native unverified loopback TLS, guard *before* constructing
            # the Basic Authorization header. A malicious factory/socket or
            # changed listener cannot redirect credentials off 127.0.0.1:443.
            if self._native_loopback:
                try:
                    peer = sock.getpeername()
                except (OSError, AttributeError):
                    peer = None
                if peer != ("127.0.0.1", 443):
                    raise IoxTransportError(
                        "IoX native HTTPS peer is not allowed",
                        category="authorization",
                    )
            sock.settimeout(self._read_timeout)
            encoded = base64.b64encode(
                f"{authorization.username}:{authorization.password}".encode("utf-8")
            ).decode("ascii")
            dispatched = self._targets.dispatch_if_current(
                variable_id,
                target_generation,
                lambda: connection.request(
                    "GET",
                    path,
                    headers={
                        "Accept": "application/xml, text/xml",
                        "Authorization": f"Basic {encoded}",
                        "Connection": "close",
                        "User-Agent": "CalendarRules/IoX-state-variables",
                    },
                ),
            )
            if not dispatched:
                raise IoxTransportError("IoX configured targets changed")
            response = connection.getresponse()
            if response.status in (401, 403):
                self._authorization.revoke()
                raise IoxTransportError(
                    "IoX authorization was rejected", category="authorization"
                )
            if response.status != 200:
                raise IoxTransportError(
                    "IoX returned an unsuccessful status", category="http-status"
                )
            length = response.getheader("Content-Length")
            if length is not None:
                try:
                    if int(length) > self._max_response_bytes:
                        raise IoxTransportError(
                            "IoX response exceeded the size limit",
                            category="response-size",
                        )
                except ValueError:
                    raise IoxTransportError(
                        "IoX response length is invalid", category="response-size"
                    ) from None
            body = response.read(self._max_response_bytes + 1)
            if len(body) > self._max_response_bytes:
                raise IoxTransportError(
                    "IoX response exceeded the size limit", category="response-size"
                )
            try:
                return body.decode("utf-8")
            except UnicodeDecodeError:
                raise IoxTransportError(
                    "IoX response encoding is invalid", category="response-encoding"
                ) from None
        except IoxTransportError:
            raise
        except ssl.SSLCertVerificationError:
            raise IoxTransportError(
                "IoX request failed: SSLCertVerificationError",
                category="tls-certificate-validation",
            ) from None
        except ssl.SSLError:
            raise IoxTransportError(
                "IoX request failed: SSLError", category="tls"
            ) from None
        except (socket.timeout, TimeoutError):
            raise IoxTransportError(
                "IoX request failed: TimeoutError", category="connection-timeout"
            ) from None
        except ConnectionRefusedError:
            raise IoxTransportError(
                "IoX request failed: ConnectionRefusedError",
                category="connection-refused",
            ) from None
        except OSError:
            raise IoxTransportError(
                "IoX request failed: OSError", category="connection"
            ) from None
        except Exception:
            raise IoxTransportError(
                "IoX request failed: internal transport error"
            ) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            self._request_lock.release()

    def _connection(
        self,
        authorization: IoxAuthorization,
        connect_timeout: float,
    ) -> HTTPSConnection:
        if not authorization.use_https or self._plugin_directory is None:
            raise IoxTransportError(
                "IoX HTTPS trust is unavailable", category="trust-anchor"
            )
        return HTTPSConnection(
            authorization.host,
            authorization.port,
            timeout=connect_timeout,
            context=_verified_loopback_context(self._plugin_directory),
        )


class ConfiguredVariablesReadOnlyProbe:
    """Serialized existence/type/value checks for all enabled-rule targets."""

    def __init__(
        self,
        authorization: EisyUiIoxAuthorization,
        transport: BoundedStateVariableTransport,
        targets: ConfiguredTargetRegistry,
    ):
        self._authorization = authorization
        self._transport = transport
        self._targets = targets
        self._worker = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="calendar-iox-probe",
        )
        self._lock = Lock()
        self._future: Future | None = None
        self._future_generation = 0
        self._future_target_generation = 0
        self._active = False
        self._status = "inactive"
        self._verified_generation = 0
        self._verified_target_generation = 0
        self._failure_category: str | None = None
        self._failed_variable_id: int | None = None
        authorization.set_change_callback(self.request)

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def ready(self) -> bool:
        with self._lock:
            verified = self._verified_generation
            verified_targets = self._verified_target_generation
            status = self._status
        target_snapshot = self._targets.snapshot()
        return (
            status == "ready"
            and self._authorization.ready
            and verified == self._authorization.generation
            and bool(target_snapshot.variable_ids)
            and verified_targets == target_snapshot.generation
        )

    @property
    def target_ids(self) -> frozenset[int]:
        return self._targets.snapshot().variable_ids

    @property
    def failed_variable_id(self) -> int | None:
        with self._lock:
            return self._failed_variable_id

    @property
    def failure_category(self) -> str | None:
        with self._lock:
            return self._failure_category

    def start(self) -> None:
        with self._lock:
            self._active = True
            if self._status == "inactive":
                self._status = "unconfigured"
        self.request()

    def configure_targets(self, variable_ids: Collection[int]) -> str:
        snapshot = self._targets.replace(variable_ids)
        with self._lock:
            if self._verified_target_generation != snapshot.generation:
                self._verified_generation = 0
                self._verified_target_generation = 0
                self._failure_category = None
                self._failed_variable_id = None
                self._status = (
                    "unconfigured" if not snapshot.variable_ids else "pending"
                )
            active = self._active
        return self.request() if active else self.status

    def stop(self) -> None:
        with self._lock:
            self._active = False
            self._status = "inactive"
            self._verified_generation = 0
            self._verified_target_generation = 0
            self._failure_category = None
            self._failed_variable_id = None
            future = self._future
            self._future = None
        if future is not None:
            future.cancel()

    def request(self) -> str:
        with self._lock:
            if not self._active:
                return self._status
            future = self._future
            completed = False
            if future is not None and future.done():
                completed = True
                generation = self._future_generation
                target_generation = self._future_target_generation
                self._future = None
                try:
                    checked_ids = future.result()
                    succeeded = bool(checked_ids)
                    failure_category = None
                    failed_variable_id = None
                except Exception as exc:
                    succeeded = False
                    failure_category = getattr(
                        exc, "category", "response-validation"
                    )
                    failed_variable_id = getattr(exc, "variable_id", None)
                current_targets = self._targets.snapshot()
                if (
                    generation == self._authorization.generation
                    and target_generation == current_targets.generation
                    and succeeded
                    and frozenset(checked_ids) == current_targets.variable_ids
                ):
                    self._verified_generation = generation
                    self._verified_target_generation = target_generation
                    self._failure_category = None
                    self._failed_variable_id = None
                    self._status = "ready"
                else:
                    self._verified_generation = 0
                    self._verified_target_generation = 0
                    self._failure_category = failure_category
                    self._failed_variable_id = failed_variable_id
                    self._status = (
                        "unauthorized"
                        if not self._authorization.ready
                        else "unconfigured"
                        if not current_targets.variable_ids
                        else "failed"
                    )
            if self._future is not None:
                return self._status
            if completed and self._status != "ready":
                # Report one complete failed/stale attempt. A later short poll
                # may retry, but no callback spins or overlaps requests.
                return self._status
            if not self._authorization.ready:
                self._failure_category = None
                self._status = "unauthorized"
                return self._status
            target_snapshot = self._targets.snapshot()
            if not target_snapshot.variable_ids:
                self._failure_category = None
                self._failed_variable_id = None
                self._status = "unconfigured"
                return self._status
            if (
                self._status == "ready"
                and self._verified_generation == self._authorization.generation
                and self._verified_target_generation == target_snapshot.generation
            ):
                return self._status
            self._future_generation = self._authorization.generation
            self._future_target_generation = target_snapshot.generation
            self._future = self._worker.submit(
                self._read_targets,
                self._transport,
                tuple(sorted(target_snapshot.variable_ids)),
            )
            self._status = "pending"
            return self._status

    @staticmethod
    def _read_targets(
        transport: BoundedStateVariableTransport,
        variable_ids: tuple[int, ...],
    ) -> tuple[int, ...]:
        for variable_id in variable_ids:
            try:
                read_state_variable(transport, variable_id)
            except Exception as exc:
                error = IoxTransportError(
                    "configured IoX target validation failed",
                    category=getattr(exc, "category", "response-validation"),
                )
                error.variable_id = variable_id
                raise error from None
        return variable_ids

    def close(self) -> None:
        self.stop()
        self._worker.shutdown(wait=False, cancel_futures=True)
