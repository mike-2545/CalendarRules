"""Optional, offline-testable event adapter for UD-managed Google OAuth.

This module never instantiates the SDK OAuth helper, requests access tokens,
logs event payloads, or communicates with Google / IoX. The SDK instance must
be supplied explicitly by a later, separately approved integration.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping


class OAuthEventStatus(str, Enum):
    NOT_CONFIGURED = "not_configured"
    CONFIGURATION_RECEIVED = "configuration_received"
    GRANT_RECEIVED = "grant_received"
    EVENT_ERROR = "event_error"


class UdiOAuthEventBridge:
    """Forward only official CUSTOMNS and OAUTH events to an injected SDK.

    GRANT_RECEIVED is not authorized: token validity, scopes, and
    Google API reachability have *not* been checked. No secrets or raw
    exceptions are exposed in public status or returned values.
    """

    def __init__(self, sdk_oauth: Any) -> None:
        self._sdk_oauth = sdk_oauth
        self.status = OAuthEventStatus.NOT_CONFIGURED

    def custom_namespace(self, key: Any, data: Any) -> bool:
        """Accept only the OAuth namespaces without inspecting their content."""
        if key not in ("oauth", "oauthTokens"):
            return True
        if not isinstance(data, Mapping):
            self.status = OAuthEventStatus.EVENT_ERROR
            return False
        try:
            self._sdk_oauth.customNsHandler(key, data)
        except Exception:
            self.status = OAuthEventStatus.EVENT_ERROR
            return False
        if key == "oauthTokens":
            self.status = OAuthEventStatus.GRANT_RECEIVED
        elif self.status is not OAuthEventStatus.GRANT_RECEIVED:
            self.status = OAuthEventStatus.CONFIGURATION_RECEIVED
        return True

    def authorization_event(self, data: Any) -> bool:
        """Forward a new OAuth grant without retaining or printing its tokens."""
        if not isinstance(data, Mapping):
            self.status = OAuthEventStatus.EVENT_ERROR
            return False
        try:
            self._sdk_oauth.oauthHandler(data)
        except Exception:
            self.status = OAuthEventStatus.EVENT_ERROR
            return False
        self.status = OAuthEventStatus.GRANT_RECEIVED
        return True



class TokenBlindOAuthSink:
    """First-live-test event receiver: deliberately discard secret payloads.

    Universal Devices performs the browser redirect, code exchange, and token
    persistence. This probe confirms only that its plugin callbacks arrive.
    It intentionally does NOT use udi_interface.OAuth, access tokens, Google,
    or any IoX state variable. No references to event payloads are retained.
    """

    def customNsHandler(self, _key: Any, _data: Any) -> None:
        return None

    def oauthHandler(self, _data: Any) -> None:
        return None
