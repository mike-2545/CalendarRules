"""Fully mocked tests for the home-eisy read-only OAuth discovery probe."""

from __future__ import annotations

from collections import deque
from pathlib import Path
import sys

import pytest

# The plugin-level integration module is deliberately outside the src core.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from eisy_google_calendar.auth import REQUIRED_GOOGLE_SCOPES
from eisy_google_calendar.calendar_discovery import (
    MAX_DISCOVERY_PAGES, AuthorizationRejected, GoogleApiError
)
from oauth_event_bridge import OAuthEventStatus
from oauth_readonly_discovery import ReadOnlyOAuthDiscoveryProbe


SCOPES = " ".join(sorted(REQUIRED_GOOGLE_SCOPES))


class FakeSdkOAuth:
    """Record only operation names; never retain mock credential inputs."""

    def __init__(self, *, token="FAKE_ACCESS_TOKEN", token_error=None):
        self.calls = []
        self.token = token
        self.token_error = token_error

    def customNsHandler(self, key, data):
        self.calls.append(("custom", key))

    def oauthHandler(self, token):
        self.calls.append(("grant",))

    def getAccessToken(self):
        self.calls.append(("access",))
        if self.token_error is not None:
            raise self.token_error
        return self.token


class FakeGoogle:
    def __init__(self, pages):
        self.pages = deque(pages)
        self.calls = []

    def __call__(self, token, params):
        self.calls.append((token, dict(params)))
        response = self.pages.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def configured(bridge):
    assert bridge.custom_namespace(
        "oauth",
        {
            "scope": SCOPES,
            "client_secret": "PRIVATE_CLIENT_SECRET",
            "client_id": "PRIVATE_CLIENT_ID",
        },
    )
    assert bridge.custom_namespace(
        "oauthTokens",
        {"refresh_token": "PRIVATE_REFRESH_TOKEN"},
    )


def test_good_config_and_restored_grant_yields_only_aggregate_calendar_count():
    sdk = FakeSdkOAuth()
    google = FakeGoogle([
        {"items": [{"id": "PRIVATE_CAL_ID", "summary": "PRIVATE_CAL_NAME"}]},
    ])
    bridge = ReadOnlyOAuthDiscoveryProbe(
        sdk, safe_logging=lambda: True, fetch_page=google
    )
    configured(bridge)
    assert bridge.status is OAuthEventStatus.GRANT_RECEIVED
    outcome = bridge.discover_once()
    assert outcome.status == "ready"
    assert outcome.calendar_count == 1
    assert "PRIVATE_" not in repr(outcome)
    assert sdk.calls == [
        ("custom", "oauth"), ("custom", "oauthTokens"), ("access",),
    ]
    assert len(google.calls) == 1
    assert bridge.discover_once().status == "already_attempted"
    assert len(google.calls) == 1


def test_auth_event_does_not_trigger_http_read():
    sdk = FakeSdkOAuth()
    google = FakeGoogle([{"items": []}])
    probe = ReadOnlyOAuthDiscoveryProbe(
        sdk, safe_logging=lambda: True, fetch_page=google
    )
    assert probe.custom_namespace("ignored", {"client_secret": "PRIVATE"})
    assert sdk.calls == []
    assert probe.authorization_event({
        "access_token": "PRIVATE", "refresh_token": "PRIVATE",
    })
    assert probe.status is OAuthEventStatus.GRANT_RECEIVED
    assert google.calls == []  # Explicit one-shot poll is required.


@pytest.mark.parametrize("scope", [
    "",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
    SCOPES + " https://www.googleapis.com/auth/calendar",
])
def test_missing_or_excessive_permissions_fail_closed(scope):
    sdk = FakeSdkOAuth()
    google = FakeGoogle([])
    probe = ReadOnlyOAuthDiscoveryProbe(
        sdk, safe_logging=lambda: True, fetch_page=google
    )
    probe.custom_namespace("oauth", {"scope": scope})
    probe.custom_namespace("oauthTokens", {"refresh_token": "PRIVATE"})
    result = probe.discover_once()
    assert result.status == "missing_scopes"
    assert ("access",) not in sdk.calls
    assert google.calls == []


def test_log_level_cannot_be_debug_before_forwarding_or_reading():
    sdk = FakeSdkOAuth()
    google = FakeGoogle([])
    active = [False]
    probe = ReadOnlyOAuthDiscoveryProbe(
        sdk, safe_logging=lambda: active[0], fetch_page=google
    )
    assert probe.custom_namespace("oauth", {"scope": SCOPES}) is False
    assert probe.custom_namespace("oauthTokens", {"refresh_token": "PRIVATE"}) is False
    assert probe.authorization_event({"access_token": "PRIVATE"}) is False
    assert sdk.calls == []
    assert probe.discover_once().status == "blocked_logging"
    assert google.calls == []


def test_log_level_changed_after_sdk_receives_data_blocks_google_access():
    sdk = FakeSdkOAuth()
    google = FakeGoogle([])
    active = [True]
    probe = ReadOnlyOAuthDiscoveryProbe(
        sdk, safe_logging=lambda: active[0], fetch_page=google
    )
    configured(probe)
    active[0] = False
    result = probe.discover_once()
    assert result.status == "blocked_logging"
    assert ("access",) not in sdk.calls


def test_missing_grant_disallows_token_access_and_google_call():
    sdk = FakeSdkOAuth()
    google = FakeGoogle([])
    probe = ReadOnlyOAuthDiscoveryProbe(
        sdk, safe_logging=lambda: True, fetch_page=google
    )
    probe.custom_namespace("oauth", {"scope": SCOPES})
    assert probe.discover_once().status == "authentication_required"
    assert ("access",) not in sdk.calls
    assert google.calls == []


def test_missing_saved_access_token_returns_authentication_required():
    sdk = FakeSdkOAuth(token_error=ValueError("PRIVATE_SDK_FAILURE"))
    google = FakeGoogle([])
    probe = ReadOnlyOAuthDiscoveryProbe(
        sdk, safe_logging=lambda: True, fetch_page=google
    )
    configured(probe)
    result = probe.discover_once()
    assert result.status == "authentication_required"
    assert "PRIVATE" not in repr(result)
    assert not google.calls


@pytest.mark.parametrize("failure,expected", [
    (AuthorizationRejected("PRIVATE_TOKEN"), "revoked"),
    (GoogleApiError("PRIVATE_API_BODY"), "api_error"),
    (RuntimeError("PRIVATE_TRANSPORT_FAILURE"), "api_error"),
])
def test_google_failures_are_sanitized_and_not_retried(failure, expected):
    sdk = FakeSdkOAuth()
    google = FakeGoogle([failure])
    probe = ReadOnlyOAuthDiscoveryProbe(
        sdk, safe_logging=lambda: True, fetch_page=google
    )
    configured(probe)
    result = probe.discover_once()
    assert result.status == expected
    assert "PRIVATE" not in repr(result)
    assert probe.discover_once().status == "already_attempted"
    assert len(google.calls) == 1


def test_invalid_namespace_payload_does_not_reach_sdk():
    sdk = FakeSdkOAuth()
    probe = ReadOnlyOAuthDiscoveryProbe(
        sdk, safe_logging=lambda: True, fetch_page=FakeGoogle([])
    )
    assert probe.custom_namespace("oauth", "PRIVATE_NOT_MAPPING") is False
    assert probe.authorization_event("PRIVATE_NOT_MAPPING") is False
    assert not sdk.calls


def test_unbounded_unique_pagination_tokens_fail_closed_without_results():
    sdk = FakeSdkOAuth()
    fake = FakeGoogle([
        {
            "items": [{"id": f"PRIVATE_CAL_{i}", "summary": "Private"}],
            "nextPageToken": f"PRIVATE_NEXT_{i}",
        }
        for i in range(MAX_DISCOVERY_PAGES + 1)
    ])
    probe = ReadOnlyOAuthDiscoveryProbe(
        sdk, safe_logging=lambda: True, fetch_page=fake
    )
    configured(probe)
    result = probe.discover_once()
    assert result.status == "api_error"
    assert result.calendar_count == 0
    assert "PRIVATE" not in repr(result)
    assert len(fake.calls) == MAX_DISCOVERY_PAGES


def test_exceptions_in_logger_probe_fail_closed():
    sdk = FakeSdkOAuth()
    probe = ReadOnlyOAuthDiscoveryProbe(
        sdk, safe_logging=lambda: (_ for _ in ()).throw(
            RuntimeError("PRIVATE_LOG_EXCEPTION")
        ), fetch_page=FakeGoogle([]),
    )
    assert probe.custom_namespace("oauth", {"scope": SCOPES}) is False
    assert probe.discover_once().status == "blocked_logging"
    assert not sdk.calls
