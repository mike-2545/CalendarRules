"""Offline-only tests for strict Google events.list HTTP transport."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from eisy_google_calendar.calendar_api import (
    MAX_SNAPSHOT_CALENDARS, MAX_SNAPSHOT_PAGES_PER_CALENDAR,
    MAX_SNAPSHOT_RAW_EVENTS, fetch_snapshot,
)
from eisy_google_calendar.calendar_discovery import AuthorizationRejected, GoogleApiError
from eisy_google_calendar.google_events_http import (
    EVENTS_ENDPOINT, GoogleEventsHttp, MAX_RESPONSE_BYTES,
    _RejectRedirects,
)
from eisy_google_calendar.models import CalendarConfig, Rule, SnapshotStatus
from eisy_google_calendar.preview import preview_snapshot


TOKEN = "FAKE_ACCESS_TOKEN"
NOW = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)
PARAMS = {
    "singleEvents": True,
    "showDeleted": False,
    "timeMin": "2025-01-01T10:00:00Z",
    "timeMax": "2025-01-02T12:00:00Z",
}


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size):
        self.read_sizes.append(size)
        return self.body[:size]


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def body(value):
    return json.dumps(value).encode("utf-8")


def transport(opener, token=lambda: TOKEN):
    return GoogleEventsHttp(get_access_token=token, opener=opener)


def test_fixed_get_path_and_escaped_calendar_id_with_no_query_token():
    response = FakeResponse(body({"items": []}))
    opener = FakeOpener([response])
    data = transport(opener)("team/one@example.invalid", PARAMS)
    assert data == {"items": []}
    request, timeout = opener.calls[0]
    assert timeout == 8
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.get_header("Authorization") == "Bearer " + TOKEN
    assert request.get_header("Accept") == "application/json"
    url = urlsplit(request.full_url)
    assert request.full_url.startswith(EVENTS_ENDPOINT)
    assert url.scheme == "https"
    assert url.netloc == "www.googleapis.com"
    assert url.path == "/calendar/v3/calendars/team%2Fone%40example.invalid/events"
    assert parse_qs(url.query) == {
        "singleEvents": ["true"],
        "showDeleted": ["false"],
        "maxResults": ["250"],
        "timeMin": [PARAMS["timeMin"]],
        "timeMax": [PARAMS["timeMax"]],
    }
    assert TOKEN not in request.full_url
    assert response.read_sizes == [MAX_RESPONSE_BYTES + 1]


def test_event_pagination_token_url_encoded_without_credentials():
    opener = FakeOpener([FakeResponse(body({"items": []}))])
    args = {**PARAMS, "pageToken": "secret/?&=+"}
    transport(opener)("primary", args)
    parsed = urlsplit(opener.calls[0][0].full_url)
    assert parsed.path.endswith("/primary/events")
    assert parse_qs(parsed.query)["pageToken"] == ["secret/?&=+"]
    assert TOKEN not in parsed.query


def test_http_redirects_cannot_forward_authorization_header():
    handler = _RejectRedirects()
    assert handler.redirect_request(None, None, 302, "", {}, "https://evil.invalid") is None


@pytest.mark.parametrize("status,expected", [
    (401, AuthorizationRejected),
    (403, GoogleApiError),
    (429, GoogleApiError),
    (500, GoogleApiError),
    (302, GoogleApiError),
])
def test_http_errors_are_sanitized(status, expected):
    error = HTTPError(
        "https://www.googleapis.com/PRIVATE_CALENDAR",
        status, "PRIVATE_HTTP_BODY", {}, None,
    )
    with pytest.raises(expected) as caught:
        transport(FakeOpener([error]))("primary", PARAMS)
    assert "PRIVATE" not in str(caught.value)
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize("exc", [
    TimeoutError("PRIVATE_REFRESH_TOKEN"),
    URLError("PRIVATE_URL"),
    RuntimeError("PRIVATE_CLIENT_SECRET"),
])
def test_transport_errors_have_fixed_safe_messages(exc):
    with pytest.raises(GoogleApiError) as caught:
        transport(FakeOpener([exc]))("primary", PARAMS)
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("value", [None, "", "a b", "bad\r\nheader", "X" * 8193, 42])
def test_invalid_access_token_blocks_request(value):
    opener = FakeOpener([])
    with pytest.raises(GoogleApiError):
        transport(opener, token=lambda: value)("primary", PARAMS)
    assert not opener.calls


def test_sdk_exception_does_not_escape_and_no_http_request():
    opener = FakeOpener([])
    def invalid_provider():
        raise RuntimeError("PRIVATE_REFRESH_TOKEN")
    with pytest.raises(GoogleApiError) as caught:
        transport(opener, token=invalid_provider)("primary", PARAMS)
    assert "PRIVATE" not in str(caught.value)
    assert not opener.calls


@pytest.mark.parametrize("calendar_id", ["", "\r\n", "bad\0id", "x"*1025, ".", "..", chr(0xD800), None])
def test_invalid_calendar_id_never_requests_google(calendar_id):
    opener = FakeOpener([])
    with pytest.raises(GoogleApiError):
        transport(opener)(calendar_id, PARAMS)
    assert not opener.calls


@pytest.mark.parametrize("params", [
    {},
    {**PARAMS, "singleEvents": "true"},
    {**PARAMS, "showDeleted": True},
    {**PARAMS, "syncToken": "PRIVATE"},
    {**PARAMS, "timeMin": "not-a-time"},
    {**PARAMS, "timeMin": "2025-01-01T10:00:00"},
    {**PARAMS, "timeMin": "2025-01-04T10:00:00Z"},
    {**PARAMS, "timeMax": "2025-05-01T10:00:00Z"},
    {**PARAMS, "pageToken": ""},
    {**PARAMS, "pageToken": "x"*4097},
    {**PARAMS, "pageToken": "bad\r\nrequest"},
])
def test_unapproved_or_invalid_query_parameters_block_network(params):
    opener = FakeOpener([])
    with pytest.raises(GoogleApiError):
        transport(opener)("primary", params)
    assert not opener.calls


@pytest.mark.parametrize("value", [
    b"INVALID_RESPONSE_PRIVATE",
    b"[]",
    b"null",
    b"\xff\xfe",
    b"*" * (MAX_RESPONSE_BYTES + 1),
])
def test_malformed_or_oversized_response_fails_closed(value):
    with pytest.raises(GoogleApiError) as caught:
        transport(FakeOpener([FakeResponse(value)]))("primary", PARAMS)
    assert "PRIVATE" not in str(caught.value)


def config(*calendar_ids):
    return CalendarConfig(
        tuple(calendar_ids), "America/New_York", 60,
        (Rule("workshop", "Workshop", 101),),
    )


def event(identifier, summary="Workshop", start="2025-01-01T12:00:00Z",
          end="2025-01-01T13:00:00Z", status="confirmed"):
    return {
        "id": identifier,
        "summary": summary,
        "status": status,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }


def test_full_offline_google_pages_to_trusted_read_only_preview():
    opener = FakeOpener([
        FakeResponse(body({
            "items": [
                event("evt-1"),
                event("evt-cancelled", status="cancelled"),
            ],
            "nextPageToken": "more",
        })),
        FakeResponse(body({
            "items": [event("evt-2", summary="Unrelated")]
        })),
    ])
    captured = []
    def provider():
        captured.append(True)
        return TOKEN
    snapshot = fetch_snapshot(
        config("team@example.invalid"),
        fetch_page=transport(opener, provider),
        now=NOW,
        coverage_horizon=timedelta(hours=24),
    )
    assert snapshot.status is SnapshotStatus.VALID
    assert len(snapshot.events) == 3
    assert len(captured) == 2  # Tokens are obtained only per page.
    preview = preview_snapshot(
        config("team@example.invalid"), snapshot,
        now=NOW, require_coverage=True, max_snapshot_age=timedelta(hours=24),
    )
    assert preview.trusted is True
    assert preview.desired_states == {101: 1}
    assert preview.ignored_reasons["cancelled"] == 1
    assert len(opener.calls) == 2


def test_second_calendar_http_failure_discards_first_calendar_events():
    opener = FakeOpener([
        FakeResponse(body({"items": [event("evt-1")]})),
        HTTPError("https://example.invalid/PRIVATE_ID", 403, "private", {}, None),
    ])
    snapshot = fetch_snapshot(
        config("one", "two"), fetch_page=transport(opener), now=NOW,
    )
    assert snapshot.status is SnapshotStatus.FAILED
    assert snapshot.events == ()
    assert "PRIVATE" not in repr(snapshot)


def test_paginated_snapshot_never_fetches_more_than_bound():
    calls = []
    def pages(calendar_id, params):
        calls.append(params)
        return {"items": [], "nextPageToken": f"unique-{len(calls)}"}
    snapshot = fetch_snapshot(config("primary"), fetch_page=pages, now=NOW)
    assert snapshot.status is SnapshotStatus.FAILED
    assert snapshot.events == ()
    assert len(calls) == MAX_SNAPSHOT_PAGES_PER_CALENDAR


def test_calendar_count_limit_blocks_all_network_calls():
    calls = []
    def pages(calendar_id, params):
        calls.append(calendar_id)
        return {"items": []}
    snapshot = fetch_snapshot(
        config(*[f"calendar-{i}" for i in range(MAX_SNAPSHOT_CALENDARS + 1)]),
        fetch_page=pages, now=NOW,
    )
    assert snapshot.status is SnapshotStatus.FAILED
    assert snapshot.events == ()
    assert not calls


def test_raw_event_limit_aborts_without_partial_snapshot():
    fake = [
        event(f"evt-{i}") for i in range(MAX_SNAPSHOT_RAW_EVENTS + 1)
    ]
    snapshot = fetch_snapshot(
        config("primary"), fetch_page=lambda _, __: {"items": fake}, now=NOW,
    )
    assert snapshot.status is SnapshotStatus.FAILED
    assert snapshot.events == ()
    assert snapshot.error == "calendar snapshot could not be completed"


def test_only_one_fixed_google_endpoint_is_supported():
    import inspect
    from eisy_google_calendar import google_events_http
    text = inspect.getsource(google_events_http)
    assert "calendar/v3/calendars/" in text
    assert "write_state_variable" not in text
    assert "events.insert" not in text
    assert "events.update" not in text
    assert "POST" not in text
