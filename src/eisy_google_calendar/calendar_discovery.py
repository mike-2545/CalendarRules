"""Read-only Google Calendar list discovery with an injected transport."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


CalendarListPage = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
MAX_DISCOVERY_PAGES = 40
MAX_DISCOVERY_CALENDARS = 10000


class CalendarDiscoveryError(RuntimeError):
    """Raised when a calendar list response cannot be safely consumed."""


class AuthorizationRejected(CalendarDiscoveryError):
    """Raised by a transport when Google rejects or revokes authorization."""


class GoogleApiError(CalendarDiscoveryError):
    """Raised by a transport for a non-authorization Google API failure."""


@dataclass(frozen=True)
class CalendarSummary:
    """The non-secret fields needed to let an owner select a calendar."""

    calendar_id: str
    name: str
    primary: bool = False
    access_role: str | None = None
    timezone: str | None = None


def _optional_string(value: Any, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise CalendarDiscoveryError(f"{path} must be a non-empty string")
    return value


def _calendar(value: Any, index: int) -> CalendarSummary:
    path = f"calendarList.items[{index}]"
    if not isinstance(value, dict):
        raise CalendarDiscoveryError(f"{path} must be an object")
    calendar_id = value.get("id")
    if not isinstance(calendar_id, str) or not calendar_id:
        raise CalendarDiscoveryError(f"{path}.id must be a non-empty string")
    summary_override = value.get("summaryOverride")
    summary = value.get("summary")
    if summary_override is not None and (
        not isinstance(summary_override, str) or not summary_override
    ):
        raise CalendarDiscoveryError(
            f"{path}.summaryOverride must be a non-empty string"
        )
    if summary is not None and (not isinstance(summary, str) or not summary):
        raise CalendarDiscoveryError(f"{path}.summary must be a non-empty string")
    primary = value.get("primary", False)
    if not isinstance(primary, bool):
        raise CalendarDiscoveryError(f"{path}.primary must be a boolean")
    return CalendarSummary(
        calendar_id=calendar_id,
        name=summary_override or summary or calendar_id,
        primary=primary,
        access_role=_optional_string(value.get("accessRole"), f"{path}.accessRole"),
        timezone=_optional_string(value.get("timeZone"), f"{path}.timeZone"),
    )


def list_calendars(
    *, access_token: str, fetch_page: CalendarListPage
) -> tuple[CalendarSummary, ...]:
    """Return all visible calendar-list entries using a bearer-token transport.

    The transport owns HTTP and must raise :class:`AuthorizationRejected` for
    rejected credentials or :class:`GoogleApiError` for other API failures.
    Tokens and response bodies are deliberately excluded from raised messages.
    """

    if not isinstance(access_token, str) or not access_token:
        raise CalendarDiscoveryError("Google access token is unavailable")

    calendars: list[CalendarSummary] = []
    seen_ids: set[str] = set()
    seen_page_tokens: set[str] = set()
    page_token: str | None = None
    page_count = 0

    while True:
        page_count += 1
        if page_count > MAX_DISCOVERY_PAGES:
            raise CalendarDiscoveryError("Google Calendar list exceeded the page limit")
        params: dict[str, Any] = {
            "maxResults": 250,
            "showDeleted": False,
            "showHidden": False,
        }
        if page_token is not None:
            params["pageToken"] = page_token
        try:
            response = fetch_page(access_token, params)
        except AuthorizationRejected:
            raise AuthorizationRejected(
                "Google authorization was rejected"
            ) from None
        except GoogleApiError:
            raise GoogleApiError("Google Calendar list request failed") from None
        except Exception:
            raise GoogleApiError("Google Calendar list request failed") from None

        if not isinstance(response, dict):
            raise CalendarDiscoveryError("calendarList response must be an object")
        items = response.get("items", [])
        if not isinstance(items, list):
            raise CalendarDiscoveryError("calendarList.items must be a list")
        for index, item in enumerate(items):
            calendar = _calendar(item, index)
            if calendar.calendar_id in seen_ids:
                raise CalendarDiscoveryError(
                    "calendarList response contains a duplicate calendar id"
                )
            seen_ids.add(calendar.calendar_id)
            calendars.append(calendar)
            if len(calendars) > MAX_DISCOVERY_CALENDARS:
                raise CalendarDiscoveryError("Google Calendar list exceeded the result limit")

        next_page_token = response.get("nextPageToken")
        if next_page_token is None:
            return tuple(calendars)
        if not isinstance(next_page_token, str) or not next_page_token:
            raise CalendarDiscoveryError(
                "calendarList.nextPageToken must be a non-empty string"
            )
        if next_page_token in seen_page_tokens:
            raise CalendarDiscoveryError(
                "calendarList response contains a repeated page token"
            )
        seen_page_tokens.add(next_page_token)
        page_token = next_page_token
