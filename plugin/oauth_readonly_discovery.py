"""Single-attempt, explicitly opted-in read-only OAuth CalendarList discovery.

This adapter is *not* used by the default CalendarRules runtime or the
existing token-blind test. Only an approved marker and source-pinned SDK
in the home/test eisy can construct it. No credentials or calendar details
are placed in notices/logs; only status and aggregate count are returned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from eisy_google_calendar.auth import (
    OAuthConfiguration, OAuthState, REQUIRED_GOOGLE_SCOPES,
    UdiOAuthTokenProvider, discover_authorized_calendars,
)
from eisy_google_calendar.calendar_discovery import CalendarListPage

from oauth_event_bridge import OAuthEventStatus, UdiOAuthEventBridge


@dataclass(frozen=True)
class DiscoverySummary:
    """Public result contains counts/status only, never private calendar data."""

    status: str
    calendar_count: int = 0


class ReadOnlyOAuthDiscoveryProbe:
    """One-shot test bridge around UD's authenticated OAuth helper."""

    def __init__(
        self,
        sdk_oauth: Any,
        *,
        safe_logging: Callable[[], bool],
        fetch_page: CalendarListPage,
    ) -> None:
        self._bridge = UdiOAuthEventBridge(sdk_oauth)
        self._sdk_oauth = sdk_oauth
        self._safe_logging = safe_logging
        self._fetch_page = fetch_page
        self._configuration: OAuthConfiguration | None = None
        self._attempted = False
        self.status = OAuthEventStatus.NOT_CONFIGURED

    def _logging_allowed(self) -> bool:
        try:
            return self._safe_logging() is True
        except Exception:
            return False

    def custom_namespace(self, key: Any, data: Any) -> bool:
        if key not in ("oauth", "oauthTokens"):
            return True
        if not self._logging_allowed():
            self.status = OAuthEventStatus.EVENT_ERROR
            return False
        if not isinstance(data, Mapping):
            self.status = OAuthEventStatus.EVENT_ERROR
            return False
        if key == "oauth":
            scope = data.get("scope")
            if isinstance(scope, str) and scope:
                candidate = OAuthConfiguration.from_scopes(scope.split())
                if candidate is not None and candidate.scopes == REQUIRED_GOOGLE_SCOPES:
                    self._configuration = candidate
                else:
                    self._configuration = None
            else:
                self._configuration = None
        if not self._bridge.custom_namespace(key, data):
            self.status = OAuthEventStatus.EVENT_ERROR
            return False
        self.status = self._bridge.status
        return True

    def authorization_event(self, data: Any) -> bool:
        if not self._logging_allowed():
            self.status = OAuthEventStatus.EVENT_ERROR
            return False
        if not self._bridge.authorization_event(data):
            self.status = OAuthEventStatus.EVENT_ERROR
            return False
        self.status = self._bridge.status
        return True

    def discover_once(self) -> DiscoverySummary:
        """Perform at most one GET-based discovery per process lifetime."""

        if self._attempted:
            return DiscoverySummary("already_attempted")
        self._attempted = True
        if not self._logging_allowed():
            return DiscoverySummary("blocked_logging")
        if self._configuration is None:
            return DiscoverySummary("missing_scopes")
        if self.status is not OAuthEventStatus.GRANT_RECEIVED:
            return DiscoverySummary("authentication_required")

        result = discover_authorized_calendars(
            configuration=self._configuration,
            token_provider=UdiOAuthTokenProvider(self._sdk_oauth.getAccessToken),
            fetch_page=self._fetch_page,
        )
        if result.state is OAuthState.READY:
            return DiscoverySummary("ready", len(result.calendars))
        return DiscoverySummary(result.state.value)
