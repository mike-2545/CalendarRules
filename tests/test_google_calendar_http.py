"""Offline, no-network tests for the bounded read-only Google transport."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from eisy_google_calendar.auth import (
    OAuthConfiguration, OAuthState, REQUIRED_GOOGLE_SCOPES,
    discover_authorized_calendars,
)
from eisy_google_calendar.calendar_discovery import (
    AuthorizationRejected, GoogleApiError, list_calendars,
)
from eisy_google_calendar.google_calendar_http import (
    CALENDAR_LIST_URL, GoogleCalendarListHttp, MAX_RESPONSE_BYTES,
)


TOKEN = "PRIVATE_ACCESS_TOKEN"
PARAMS = {"maxResults": 250, "showDeleted": False, "showHidden": False}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size):
        return self.payload[:size]


class StubOpener:
    def __init__(self, pages):
        self.pages = list(pages)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        value = self.pages.pop(0)
        if isinstance(value, Exception):
            raise value
        return FakeResponse(value)


def encoded(value):
    return json.dumps(value).encode("utf-8")


def test_only_fixed_get_endpoint_and_bearer_header_used():
    opener = StubOpener([encoded({"items": []})])
    result = GoogleCalendarListHttp(opener=opener)(TOKEN, PARAMS)
    assert result == {"items": []}
    request, timeout = opener.requests[0]
    assert request.get_method() == "GET"
    assert timeout == 8
    parts = urlsplit(request.full_url)
    assert parts.scheme == "https"
    assert parts.netloc == "www.googleapis.com"
    assert parts.path == "/calendar/v3/users/me/calendarList"
    assert request.full_url.startswith(CALENDAR_LIST_URL + "?")
    assert parse_qs(parts.query) == {
        "maxResults": ["250"], "showDeleted": ["false"],
        "showHidden": ["false"],
    }
    assert TOKEN not in request.full_url
    assert request.get_header("Authorization") == "Bearer " + TOKEN
    assert request.data is None


def test_pagination_and_two_calendar_success_with_real_adapter():
    opener = StubOpener([
        encoded({
            "items": [
                {"id": "first@calendar.invalid", "summary": "First", "primary": True}
            ],
            "nextPageToken": "NEXT_PAGE_PRIVATE",
        }),
        encoded({"items": [{"id": "second@calendar.invalid", "summary": "Second"}]}),
    ])
    provider = GoogleCalendarListHttp(opener=opener)
    result = discover_authorized_calendars(
        configuration=OAuthConfiguration(REQUIRED_GOOGLE_SCOPES),
        token_provider=type("Provider", (), {"get_access_token": lambda self: TOKEN})(),
        fetch_page=provider,
    )
    assert result.state is OAuthState.READY
    assert len(result.calendars) == 2
    request, _ = opener.requests[1]
    assert parse_qs(urlsplit(request.full_url).query)["pageToken"] == ["NEXT_PAGE_PRIVATE"]
    assert TOKEN not in repr(result)
    assert "NEXT_PAGE_PRIVATE" not in repr(result)


@pytest.mark.parametrize("code,expected", [(401, AuthorizationRejected), (403, GoogleApiError), (429, GoogleApiError), (500, GoogleApiError)])
def test_http_failures_are_sanitized(code, expected):
    opener = StubOpener([HTTPError("https://www.googleapis.com/PRIVATE", code, "PRIVATE_TOKEN_BODY", {}, None)])
    with pytest.raises(expected) as exc:
        GoogleCalendarListHttp(opener=opener)(TOKEN, PARAMS)
    assert "PRIVATE" not in str(exc.value)
    assert exc.value.__suppress_context__ is True
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize("fault", [
    URLError("PRIVATE_REFRESH_TOKEN"),
    TimeoutError("PRIVATE_ACCESS_TOKEN"),
    RuntimeError("PRIVATE_GOOGLE_RESPONSE"),
])
def test_transport_exceptions_are_redacted(fault):
    with pytest.raises(GoogleApiError) as exc:
        GoogleCalendarListHttp(opener=StubOpener([fault]))(TOKEN, PARAMS)
    assert "PRIVATE" not in str(exc.value)


@pytest.mark.parametrize("payload", [
    b"PRIVATE_INVALID_JSON",
    b"[]",
    b"null",
    b"\xff\xfe",
    b"a" * (MAX_RESPONSE_BYTES + 1),
])
def test_malformed_or_oversized_http_response_fails_closed(payload):
    with pytest.raises(GoogleApiError) as exc:
        GoogleCalendarListHttp(opener=StubOpener([payload]))(TOKEN, PARAMS)
    assert "PRIVATE" not in str(exc.value)


@pytest.mark.parametrize("token", ["", "bad\r\nheader", "has space", "X" * 8193, 123, None])
def test_invalid_access_tokens_do_not_make_http_requests(token):
    opener = StubOpener([])
    with pytest.raises(GoogleApiError):
        GoogleCalendarListHttp(opener=opener)(token, PARAMS)
    assert not opener.requests


@pytest.mark.parametrize("params", [
    {}, {"maxResults": True, "showDeleted": False, "showHidden": False},
    {"maxResults": 251, "showDeleted": False, "showHidden": False},
    {"maxResults": 250, "showDeleted": "false", "showHidden": False},
    {**PARAMS, "syncToken": "something"},
    {**PARAMS, "pageToken": "\r\nPRIVATE"},
    {**PARAMS, "pageToken": ""},
    {**PARAMS, "pageToken": "T" * 4097},
])
def test_invalid_or_out_of_scope_query_arguments_are_rejected(params):
    opener = StubOpener([])
    with pytest.raises(GoogleApiError):
        GoogleCalendarListHttp(opener=opener)(TOKEN, params)
    assert not opener.requests


def test_response_reaches_discovery_only_through_read_only_transport():
    provider = GoogleCalendarListHttp(
        opener=StubOpener([encoded({"items": [{"id": "primary", "summary": "Primary"}]})])
    )
    results = list_calendars(access_token=TOKEN, fetch_page=provider)
    assert len(results) == 1
    assert results[0].name == "Primary"


def test_bad_transport_configuration_is_rejected():
    for kwargs in (
        {"timeout_seconds": 0}, {"timeout_seconds": 31},
        {"max_response_bytes": 100},
        {"max_response_bytes": MAX_RESPONSE_BYTES + 1},
        {"timeout_seconds": True},
    ):
        with pytest.raises(ValueError):
            GoogleCalendarListHttp(**kwargs)
