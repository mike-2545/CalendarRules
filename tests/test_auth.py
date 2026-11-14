from __future__ import annotations

from pathlib import Path

import pytest

from eisy_google_calendar.auth import (
    REQUIRED_GOOGLE_SCOPES,
    CredentialsRevoked,
    CredentialsUnavailable,
    OAuthConfiguration,
    OAuthState,
    TokenRefreshError,
    UdiOAuthTokenProvider,
    discover_authorized_calendars,
)
from eisy_google_calendar.calendar_discovery import (
    AuthorizationRejected,
    CalendarDiscoveryError,
    GoogleApiError,
    list_calendars,
)


class StubProvider:
    def __init__(self, *, token="mock-access-token", error=None):
        self.token = token
        self.error = error
        self.calls = 0

    def get_access_token(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.token


class RefreshingProvider:
    def __init__(self):
        self.expired = True
        self.refresh_calls = 0

    def get_access_token(self):
        if self.expired:
            self.refresh_calls += 1
            self.expired = False
        return "refreshed-mock-token"


def configured():
    return OAuthConfiguration(REQUIRED_GOOGLE_SCOPES)


def test_authentication_core_has_no_info_debug_or_print_logging():
    package = Path(__file__).resolve().parents[1] / "src" / "eisy_google_calendar"
    source = "\n".join(
        (package / name).read_text()
        for name in ("auth.py", "calendar_discovery.py")
    )
    for logging_call in ("LOGGER", "logging.", "print("):
        assert logging_call not in source


@pytest.mark.parametrize(
    "configuration",
    [None, OAuthConfiguration(frozenset()), OAuthConfiguration.from_scopes("bad")],
)
def test_missing_oauth_configuration_does_not_request_a_token(configuration):
    provider = StubProvider()
    result = discover_authorized_calendars(
        configuration=configuration,
        token_provider=provider,
        fetch_page=lambda _token, _params: pytest.fail("must not call Google"),
    )
    assert result.state is OAuthState.MISSING_CONFIGURATION
    assert result.calendars == ()
    assert provider.calls == 0


def test_udi_provider_maps_missing_sdk_token_without_retaining_credentials():
    provider = UdiOAuthTokenProvider(
        lambda: (_ for _ in ()).throw(ValueError("SDK token missing"))
    )
    result = discover_authorized_calendars(
        configuration=configured(),
        token_provider=provider,
        fetch_page=lambda _token, _params: pytest.fail("must not call Google"),
    )
    assert result.state is OAuthState.AUTHENTICATION_REQUIRED
    assert "SDK token missing" not in repr(result)


def test_udi_provider_redacts_direct_sdk_exception():
    provider = UdiOAuthTokenProvider(
        lambda: (_ for _ in ()).throw(ValueError("access-token-in-sdk-error"))
    )
    with pytest.raises(CredentialsUnavailable) as caught:
        provider.get_access_token()
    assert str(caught.value) == "Google authentication is required"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_calendar_discovery_uses_refreshed_token_and_paginates():
    provider = RefreshingProvider()
    calls = []

    def fetch_page(token, params):
        calls.append((token, dict(params)))
        if "pageToken" not in params:
            return {
                "items": [
                    {
                        "id": "primary@example.invalid",
                        "summary": "Primary",
                        "primary": True,
                        "accessRole": "owner",
                        "timeZone": "America/New_York",
                    }
                ],
                "nextPageToken": "page-2",
            }
        return {
            "items": [
                {
                    "id": "shared@example.invalid",
                    "summary": "Shared",
                    "summaryOverride": "Workshop",
                    "accessRole": "reader",
                }
            ]
        }

    result = discover_authorized_calendars(
        configuration=configured(),
        token_provider=provider,
        fetch_page=fetch_page,
    )

    assert result.state is OAuthState.READY
    assert [calendar.name for calendar in result.calendars] == [
        "Primary",
        "Workshop",
    ]
    assert result.calendars[0].primary is True
    assert provider.refresh_calls == 1
    assert calls == [
        (
            "refreshed-mock-token",
            {"maxResults": 250, "showDeleted": False, "showHidden": False},
        ),
        (
            "refreshed-mock-token",
            {
                "maxResults": 250,
                "showDeleted": False,
                "showHidden": False,
                "pageToken": "page-2",
            },
        ),
    ]
    assert "refreshed-mock-token" not in repr(result)


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (CredentialsUnavailable("missing"), OAuthState.AUTHENTICATION_REQUIRED),
        (TokenRefreshError("refresh-token-value"), OAuthState.REFRESH_FAILED),
        (CredentialsRevoked("access-token-value"), OAuthState.REVOKED),
    ],
)
def test_token_failures_are_redacted_and_do_not_discover(error, state):
    result = discover_authorized_calendars(
        configuration=configured(),
        token_provider=StubProvider(error=error),
        fetch_page=lambda _token, _params: pytest.fail("must not call Google"),
    )
    assert result.state is state
    assert result.calendars == ()
    assert str(error) not in repr(result)


def test_revoked_google_grant_is_reported_separately():
    result = discover_authorized_calendars(
        configuration=configured(),
        token_provider=StubProvider(),
        fetch_page=lambda _token, _params: (_ for _ in ()).throw(
            AuthorizationRejected("response contained a credential")
        ),
    )
    assert result.state is OAuthState.REVOKED
    assert result.calendars == ()
    assert "credential" not in repr(result)


@pytest.mark.parametrize(
    "failure",
    [
        GoogleApiError("sensitive response body"),
        RuntimeError("mock-access-token"),
    ],
)
def test_google_api_errors_are_fail_closed_and_redacted(failure):
    result = discover_authorized_calendars(
        configuration=configured(),
        token_provider=StubProvider(),
        fetch_page=lambda _token, _params: (_ for _ in ()).throw(failure),
    )
    assert result.state is OAuthState.API_ERROR
    assert result.calendars == ()
    assert str(failure) not in repr(result)


@pytest.mark.parametrize(
    ("failure", "expected_type", "expected_message"),
    [
        (
            AuthorizationRejected("refresh-token-in-response"),
            AuthorizationRejected,
            "Google authorization was rejected",
        ),
        (
            GoogleApiError("access-token-in-response"),
            GoogleApiError,
            "Google Calendar list request failed",
        ),
        (
            RuntimeError("client-secret-in-response"),
            GoogleApiError,
            "Google Calendar list request failed",
        ),
    ],
)
def test_direct_transport_exceptions_are_normalized(
    failure, expected_type, expected_message
):
    with pytest.raises(expected_type) as caught:
        list_calendars(
            access_token="mock-access-token",
            fetch_page=lambda _token, _params: (_ for _ in ()).throw(failure),
        )
    assert str(caught.value) == expected_message
    assert str(failure) not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_malformed_or_repeated_calendar_pages_fail_safely():
    responses = iter(
        [
            {"items": [], "nextPageToken": "repeat"},
            {"items": [], "nextPageToken": "repeat"},
        ]
    )
    result = discover_authorized_calendars(
        configuration=configured(),
        token_provider=StubProvider(),
        fetch_page=lambda _token, _params: next(responses),
    )
    assert result.state is OAuthState.API_ERROR
    assert result.calendars == ()


def test_direct_discovery_requires_a_nonempty_token():
    with pytest.raises(CalendarDiscoveryError, match="token is unavailable"):
        list_calendars(
            access_token="",
            fetch_page=lambda _token, _params: {},
        )
