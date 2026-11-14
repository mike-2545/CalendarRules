"""Explicit eisy-ui SDK loopback-only HTTPS option for read-only IoX probes.

The self-signed eisy-ui HTTPS certificate is not exposed to store plugins by
any supported authenticated UDI API. Per Benoit's guidance, this option pins
both IPv4 destination and TCP peer to 127.0.0.1:443 and uses only owner-granted
SDK ISY credentials. It DOES NOT verify server certificate identity. A local
process able to replace the eisy-ui listener could impersonate it.

This is intentionally separate from the existing certificate-verified home
transport. It must never be silently used as a fallback after TLS failure.
"""

from __future__ import annotations

from http.client import HTTPSConnection
import socket
import ssl

from iox_transport import IoxAuthorization, IoxTransportError


NATIVE_LOOPBACK_IP = "127.0.0.1"
NATIVE_LOOPBACK_PORT = 443
NATIVE_LOOPBACK_PEER = (NATIVE_LOOPBACK_IP, NATIVE_LOOPBACK_PORT)


def require_exact_native_endpoint(authorization: IoxAuthorization) -> None:
    """Validate the SDK-supplied endpoint *before* connecting or using secrets."""

    if (
        not isinstance(authorization, IoxAuthorization)
        or authorization.host != NATIVE_LOOPBACK_IP
        or type(authorization.port) is not int
        or authorization.port != NATIVE_LOOPBACK_PORT
        or authorization.use_https is not True
    ):
        raise IoxTransportError(
            "IoX native SDK endpoint is unavailable", category="authorization"
        )


class NativeLoopbackHTTPSConnection(HTTPSConnection):
    """No DNS, proxy, tunnel, or certificate-identity fallback.

    HTTPS encryption is retained; the peer is identified by its exact local
    IPv4 socket address and port, NOT by an authenticated certificate.
    """

    def __init__(self, authorization: IoxAuthorization, timeout: float):
        require_exact_native_endpoint(authorization)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # Deliberate local-only tradeoff.
        super().__init__(
            NATIVE_LOOPBACK_IP,
            NATIVE_LOOPBACK_PORT,
            timeout=timeout,
            context=context,
        )

    def connect(self) -> None:
        if self.host != NATIVE_LOOPBACK_IP or self.port != NATIVE_LOOPBACK_PORT:
            raise IoxTransportError(
                "IoX native endpoint changed", category="authorization"
            )
        if self._tunnel_host is not None:
            raise IoxTransportError(
                "IoX native tunneling is not allowed", category="authorization"
            )

        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            raw.settimeout(self.timeout)
            raw.connect(NATIVE_LOOPBACK_PEER)
            if raw.getpeername() != NATIVE_LOOPBACK_PEER:
                raise IoxTransportError(
                    "IoX local TCP peer is not allowed", category="authorization"
                )
            self.sock = self._context.wrap_socket(
                raw, server_hostname=NATIVE_LOOPBACK_IP
            )
            if self.sock.getpeername() != NATIVE_LOOPBACK_PEER:
                raise IoxTransportError(
                    "IoX local HTTPS peer is not allowed", category="authorization"
                )
        except BaseException:
            raw.close()
            self.close()
            raise


def native_loopback_connection(
    authorization: IoxAuthorization, timeout: float
) -> HTTPSConnection:
    return NativeLoopbackHTTPSConnection(authorization, timeout)
