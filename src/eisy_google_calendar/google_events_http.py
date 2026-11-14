"""Bounded read-only Google Calendar events.list transport.

Reusable only through an explicit caller. The live controller does not import
or construct this adapter. No token, calendar ID, URL, title or API body is
logged or included in an error. The Google HTTP endpoint is fixed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .calendar_discovery import AuthorizationRejected, GoogleApiError


EVENTS_ENDPOINT = "https://www.googleapis.com/calendar/v3/calendars/"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 8
MAX_FETCH_WINDOW = timedelta(days=40)
MAX_PAGE_TOKEN_LENGTH = 4096
MAX_CALENDAR_ID_LENGTH = 1024


class _RejectRedirects(HTTPRedirectHandler):
    """Never forward an Authorization header to a redirected destination."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _default_opener(request: Request, *, timeout: int):
    return build_opener(_RejectRedirects()).open(request, timeout=timeout)


def _rfc3339(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise GoogleApiError("Google events request parameters are invalid")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except (ValueError, OverflowError):
        raise GoogleApiError("Google events request parameters are invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GoogleApiError("Google events request parameters are invalid")
    return parsed


def _safe_token(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 8192
        and all(33 <= ord(character) <= 126 for character in value)
    )


class GoogleEventsHttp:
    """Injected `fetch_page(calendar_id, params)` for `fetch_snapshot`.

    The provider is invoked for each page and does not retain access tokens.
    It can be backed by UD's supported OAuth helper in a later, separately
    approved read-only preview integration.
    """

    def __init__(
        self,
        *,
        get_access_token: Callable[[], str],
        opener: Callable[..., Any] | None = None,
        timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        if not callable(get_access_token) or (
            type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 30
        ) or (
            type(max_response_bytes) is not int
            or not 1024 <= max_response_bytes <= MAX_RESPONSE_BYTES
        ):
            raise ValueError("Google events transport configuration is invalid")
        self._token_provider = get_access_token
        self._open = opener if opener is not None else _default_opener
        self._timeout = timeout_seconds
        self._limit = max_response_bytes

    def __call__(self, calendar_id: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if (
            not isinstance(calendar_id, str)
            or not 0 < len(calendar_id) <= MAX_CALENDAR_ID_LENGTH
            or any(ord(character) < 32 or ord(character) == 127 for character in calendar_id)
        ):
            raise GoogleApiError("Google calendar selection is invalid")
        # Reject dot segments and malformed Unicode before URL construction.
        # Only the fixed Google API endpoint may ever receive the bearer token.
        try:
            calendar_id.encode("utf-8", errors="strict")
        except UnicodeError:
            raise GoogleApiError("Google calendar selection is invalid") from None
        if calendar_id in (".", ".."):
            raise GoogleApiError("Google calendar selection is invalid")
        if not isinstance(params, Mapping) or set(params) - {
            "singleEvents", "showDeleted", "timeMin", "timeMax", "pageToken"
        }:
            raise GoogleApiError("Google events request parameters are invalid")
        if params.get("singleEvents") is not True or params.get("showDeleted") is not False:
            raise GoogleApiError("Google events request parameters are invalid")
        minimum = _rfc3339(params.get("timeMin"))
        maximum = _rfc3339(params.get("timeMax"))
        if minimum >= maximum or maximum - minimum > MAX_FETCH_WINDOW:
            raise GoogleApiError("Google events request window is invalid")
        page_token = params.get("pageToken")
        if page_token is not None and (
            not isinstance(page_token, str)
            or not 0 < len(page_token) <= MAX_PAGE_TOKEN_LENGTH
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in page_token)
        ):
            raise GoogleApiError("Google events page token is invalid")

        try:
            access_token = self._token_provider()
        except Exception:
            raise GoogleApiError("Google authorization is unavailable") from None
        if not _safe_token(access_token):
            raise GoogleApiError("Google authorization is unavailable")

        query = {
            "singleEvents": "true",
            "showDeleted": "false",
            "maxResults": "250",
            "timeMin": params["timeMin"],
            "timeMax": params["timeMax"],
        }
        if page_token is not None:
            query["pageToken"] = page_token
        request = Request(
            EVENTS_ENDPOINT + quote(calendar_id, safe="") + "/events?" + urlencode(query),
            headers={"Authorization": "Bearer " + access_token, "Accept": "application/json"},
            method="GET",
        )
        try:
            with self._open(request, timeout=self._timeout) as response:
                raw = response.read(self._limit + 1)
        except HTTPError as error:
            if error.code == 401:
                raise AuthorizationRejected("Google authorization was rejected") from None
            raise GoogleApiError("Google events request failed") from None
        except Exception:
            raise GoogleApiError("Google events request failed") from None

        if not isinstance(raw, bytes) or len(raw) > self._limit:
            raise GoogleApiError("Google events response is invalid")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError):
            raise GoogleApiError("Google events response is invalid") from None
        if not isinstance(payload, dict):
            raise GoogleApiError("Google events response is invalid")
        return payload
