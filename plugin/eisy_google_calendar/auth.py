"""Offline-testable Google OAuth status and calendar discovery orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from .calendar_discovery import (
    AuthorizationRejected,
    CalendarDiscoveryError,
    CalendarListPage,
    CalendarSummary,
    GoogleApiError,
    list_calendars,
)


CALENDAR_EVENTS_READONLY = (
    "https://www.googleapis.com/auth/calendar.events.readonly"
)
CALENDAR_LIST_READONLY = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
)
REQUIRED_GOOGLE_SCOPES = frozenset(
    {CALENDAR_EVENTS_READONLY, CALENDAR_LIST_READONLY}
)


class CredentialsUnavailable(RuntimeError):
    """No saved Google grant is available yet."""


class CredentialsRevoked(RuntimeError):
    """The saved Google grant has been revoked or rejected."""


class TokenRefreshError(RuntimeError):
    """The access token could not be refreshed."""


class AccessTokenProvider(Protocol):
    def get_access_token(self) -> str:
        """Return a current access token, refreshing it when necessary."""


class OAuthState(str, Enum):
    MISSING_CONFIGURATION = "missing_configuration"
    AUTHENTICATION_REQUIRED = "authentication_required"
    READY = "ready"
    REFRESH_FAILED = "refresh_failed"
    REVOKED = "revoked"
    API_ERROR = "api_error"


@dataclass(frozen=True)
class OAuthConfiguration:
    """Non-secret OAuth settings needed to validate the publisher setup."""

    scopes: frozenset[str]

    @classmethod
    def from_scopes(cls, scopes: Any) -> "OAuthConfiguration | None":
        if not isinstance(scopes, (list, tuple, set, frozenset)):
            return None
        if not all(isinstance(scope, str) and scope for scope in scopes):
            return None
        return cls(frozenset(scopes))

    @property
    def supports_calendar_discovery(self) -> bool:
        return REQUIRED_GOOGLE_SCOPES.issubset(self.scopes)


@dataclass(frozen=True)
class AuthorizationResult:
    """A redacted status result; it never contains OAuth credentials."""

    state: OAuthState
    calendars: tuple[CalendarSummary, ...] = ()
    message: str = ""


class UdiOAuthTokenProvider:
    """Adapt ``udi_interface.OAuth.getAccessToken`` without retaining tokens."""

    def __init__(self, get_access_token: Callable[[], Any]) -> None:
        self._get_access_token = get_access_token

    def get_access_token(self) -> str:
        try:
            token = self._get_access_token()
        except ValueError:
            raise CredentialsUnavailable(
                "Google authentication is required"
            ) from None
        if not isinstance(token, str) or not token:
            raise CredentialsUnavailable("Google authentication is required")
        return token


def discover_authorized_calendars(
    *,
    configuration: OAuthConfiguration | None,
    token_provider: AccessTokenProvider,
    fetch_page: CalendarListPage,
) -> AuthorizationResult:
    """Resolve auth health and discover calendars without exposing credentials."""

    if configuration is None or not configuration.supports_calendar_discovery:
        return AuthorizationResult(
            OAuthState.MISSING_CONFIGURATION,
            message="Google OAuth publisher configuration is incomplete.",
        )
    try:
        access_token = token_provider.get_access_token()
    except CredentialsUnavailable:
        return AuthorizationResult(
            OAuthState.AUTHENTICATION_REQUIRED,
            message="Use Authenticate to connect Google Calendar.",
        )
    except CredentialsRevoked:
        return AuthorizationResult(
            OAuthState.REVOKED,
            message="Google authorization was revoked; authenticate again.",
        )
    except TokenRefreshError:
        return AuthorizationResult(
            OAuthState.REFRESH_FAILED,
            message="Google authorization could not be refreshed.",
        )
    except Exception:
        return AuthorizationResult(
            OAuthState.REFRESH_FAILED,
            message="Google authorization is temporarily unavailable.",
        )

    try:
        calendars = list_calendars(
            access_token=access_token,
            fetch_page=fetch_page,
        )
    except AuthorizationRejected:
        return AuthorizationResult(
            OAuthState.REVOKED,
            message="Google authorization was rejected; authenticate again.",
        )
    except (GoogleApiError, CalendarDiscoveryError):
        return AuthorizationResult(
            OAuthState.API_ERROR,
            message="Google Calendar discovery failed without changing schedules.",
        )
    return AuthorizationResult(OAuthState.READY, calendars=calendars)
