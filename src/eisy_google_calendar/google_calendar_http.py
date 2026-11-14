"""Bounded, read-only Google CalendarList HTTP transport.

Only the fixed Google calendarList.list GET endpoint is reachable. No Google
client credentials, bearer token, request URL or response body are logged.
All external exceptions are translated into fixed, privacy-safe types.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .calendar_discovery import AuthorizationRejected, GoogleApiError


CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
MAX_RESPONSE_BYTES = 1024 * 1024
HTTP_TIMEOUT_SECONDS = 8


class GoogleCalendarListHttp:
    """Callable fetch-page adapter for a single fixed Google GET endpoint."""

    def __init__(
        self,
        *,
        opener: Callable[..., Any] | None = None,
        timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        if (
            type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 30
            or type(max_response_bytes) is not int
            or not 1024 <= max_response_bytes <= MAX_RESPONSE_BYTES
        ):
            raise ValueError("Google Calendar HTTP limits are invalid")
        self._opener = opener if opener is not None else urlopen
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes

    def __call__(
        self, access_token: str, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        # Reject control characters in tokens before constructing an HTTP
        # header; the token never appears in a query string or exception.
        if (
            not isinstance(access_token, str) or not access_token
            or len(access_token) > 8192
            or any(ord(ch) < 33 or ord(ch) > 126 for ch in access_token)
        ):
            raise GoogleApiError("Google access token is unavailable")

        if not isinstance(params, Mapping) or set(params) - {
            "maxResults", "showDeleted", "showHidden", "pageToken"
        }:
            raise GoogleApiError("Google Calendar list parameters are invalid")
        if params.get("maxResults") != 250 or type(params.get("maxResults")) is not int:
            raise GoogleApiError("Google Calendar list parameters are invalid")
        if (
            params.get("showDeleted") is not False
            or params.get("showHidden") is not False
        ):
            raise GoogleApiError("Google Calendar list parameters are invalid")

        page_token = params.get("pageToken")
        if page_token is not None and (
            not isinstance(page_token, str)
            or not page_token
            or len(page_token) > 4096
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in page_token)
        ):
            raise GoogleApiError("Google Calendar page token is invalid")

        query: dict[str, str] = {
            "maxResults": "250", "showDeleted": "false", "showHidden": "false"
        }
        if page_token is not None:
            query["pageToken"] = page_token

        request = Request(
            CALENDAR_LIST_URL + "?" + urlencode(query),
            headers={
                "Authorization": "Bearer " + access_token,
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                raw = response.read(self._max_response_bytes + 1)
        except HTTPError as error:
            if error.code == 401:
                raise AuthorizationRejected("Google authorization was rejected") from None
            raise GoogleApiError("Google Calendar list request failed") from None
        except Exception:
            raise GoogleApiError("Google Calendar list request failed") from None

        if not isinstance(raw, bytes) or len(raw) > self._max_response_bytes:
            raise GoogleApiError("Google Calendar list response is invalid")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, TypeError):
            raise GoogleApiError("Google Calendar list response is invalid") from None
        if not isinstance(decoded, dict):
            raise GoogleApiError("Google Calendar list response is invalid")
        return decoded
