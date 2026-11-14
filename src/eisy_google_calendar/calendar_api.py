"""Offline adapter for synthetic Google Calendar ``events.list`` responses."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config_identity import configuration_fingerprint
from .models import CalendarConfig, CalendarEvent, CalendarSnapshot, SnapshotStatus
from .validation import ConfigurationError, normalize_utc, validate_event


FETCH_BOUNDARY_BUFFER = timedelta(minutes=1)
MAX_SNAPSHOT_CALENDARS = 25
MAX_SNAPSHOT_PAGES_PER_CALENDAR = 40
MAX_SNAPSHOT_RAW_EVENTS = 10000
FetchPage = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]



def _utc_now(value: datetime) -> datetime:
    try:
        return normalize_utc(value, "now")
    except ConfigurationError as exc:
        raise ConfigurationError(str(exc)) from None


def calculate_fetch_window(
    *,
    now: datetime,
    config: CalendarConfig,
    coverage_horizon: timedelta = timedelta(0),
) -> tuple[datetime, datetime]:
    """Return the UTC request bounds required by all rule offsets.

    Google ``timeMin`` is an exclusive lower bound on event end, while
    ``timeMax`` is an exclusive upper bound on event start.  Positive end
    offsets therefore extend the lower bound into the past; negative start
    offsets extend the upper bound into the future.
    """

    now_utc = _utc_now(now)
    if not isinstance(coverage_horizon, timedelta) or coverage_horizon < timedelta(0):
        raise ConfigurationError("coverage_horizon must be a non-negative timedelta")
    if not config.rules:
        raise ConfigurationError("config.rules must not be empty")
    maximum_end_offset = max(rule.end_offset_minutes for rule in config.rules)
    minimum_start_offset = min(rule.start_offset_minutes for rule in config.rules)
    past_extension = max(0, maximum_end_offset)
    future_extension = max(0, -minimum_start_offset)
    return (
        now_utc - timedelta(minutes=past_extension) - FETCH_BOUNDARY_BUFFER,
        now_utc
        + coverage_horizon
        + timedelta(minutes=future_extension)
        + FETCH_BOUNDARY_BUFFER,
    )


def _rfc3339(value: Any, path: str) -> datetime:
    if not isinstance(value, str):
        raise ConfigurationError(f"{path} must be an RFC3339 string")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise ConfigurationError(f"{path} must be a valid RFC3339 timestamp") from None
    return normalize_utc(parsed, path)


def _date_value(value: Any, path: str) -> date:
    if not isinstance(value, str):
        raise ConfigurationError(f"{path} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ConfigurationError(f"{path} must be a valid ISO date") from None


def _interval(
    raw_event: Mapping[str, Any], *, timezone_name: str, event_path: str
) -> tuple[datetime, datetime, bool]:
    start = raw_event.get("start")
    end = raw_event.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        raise ConfigurationError(f"{event_path} must contain start and end objects")

    has_datetime = "dateTime" in start or "dateTime" in end
    has_date = "date" in start or "date" in end
    if has_datetime and has_date:
        raise ConfigurationError(f"{event_path} mixes dateTime and date values")
    if has_datetime:
        if "dateTime" not in start or "dateTime" not in end:
            raise ConfigurationError(f"{event_path} timed interval is incomplete")
        return (
            _rfc3339(start["dateTime"], f"{event_path}.start.dateTime"),
            _rfc3339(end["dateTime"], f"{event_path}.end.dateTime"),
            False,
        )
    if has_date:
        if "date" not in start or "date" not in end:
            raise ConfigurationError(f"{event_path} all-day interval is incomplete")
        try:
            zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigurationError("config.timezone must be a valid IANA timezone name") from None
        start_date = _date_value(start["date"], f"{event_path}.start.date")
        end_date = _date_value(end["date"], f"{event_path}.end.date")
        return (
            datetime.combine(start_date, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc),
            datetime.combine(end_date, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc),
            True,
        )
    raise ConfigurationError(f"{event_path} has no dateTime or date interval")


def event_from_api(
    calendar_id: str, raw_event: Mapping[str, Any], *, timezone_name: str
) -> CalendarEvent | None:
    """Convert one API event; skip timestamp-less cancelled exceptions."""

    if not isinstance(raw_event, dict):
        raise ConfigurationError("event must be an object")
    raw_id = raw_event.get("id")
    if not isinstance(raw_id, str) or not raw_id:
        raise ConfigurationError("event.id must be a non-empty string")
    event_path = f"calendar {calendar_id} event {raw_id}"
    cancelled = raw_event.get("status") == "cancelled"
    if cancelled and ("start" not in raw_event or "end" not in raw_event):
        return None
    start, end, all_day = _interval(raw_event, timezone_name=timezone_name, event_path=event_path)
    title = raw_event.get("summary", "")
    if not isinstance(title, str):
        if cancelled:
            title = ""
        else:
            raise ConfigurationError(f"{event_path}.summary must be a string")
    location = raw_event.get("location", "")
    if not isinstance(location, str):
        raise ConfigurationError(f"{event_path}.location must be a string")
    event = CalendarEvent(
        event_id=f"{calendar_id}:{raw_id}",
        title=title,
        start=start,
        end=end,
        all_day=all_day,
        cancelled=cancelled,
        location=location,
    )
    try:
        return validate_event(event)
    except ConfigurationError as exc:
        raise ConfigurationError(f"{event_path}: {exc}") from None


def _same_event(left: CalendarEvent, right: CalendarEvent) -> bool:
    return left == right


def _page_response(value: Any, calendar_id: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"calendar {calendar_id}: page response must be an object")
    items = value.get("items", [])
    if not isinstance(items, list):
        raise ConfigurationError(f"calendar {calendar_id}: items must be a list")
    token = value.get("nextPageToken")
    if token is not None and (not isinstance(token, str) or not token):
        raise ConfigurationError(f"calendar {calendar_id}: nextPageToken must be a non-empty string")
    return value


def fetch_snapshot(
    config: CalendarConfig,
    *,
    fetch_page: FetchPage,
    now: datetime,
    coverage_horizon: timedelta = timedelta(0),
) -> CalendarSnapshot:
    """Fetch a complete, non-incremental snapshot using an injected fetcher."""

    try:
        now_utc = _utc_now(now)
        time_min, time_max = calculate_fetch_window(
            now=now_utc,
            config=config,
            coverage_horizon=coverage_horizon,
        )
        if not 1 <= len(config.calendar_ids) <= MAX_SNAPSHOT_CALENDARS:
            raise ConfigurationError("selected calendar count exceeds the safe limit")
        events: dict[str, CalendarEvent] = {}
        total_raw_events = 0
        for calendar_id in config.calendar_ids:
            page_token: str | None = None
            seen_tokens: set[str] = set()
            page_count = 0
            while True:
                page_count += 1
                if page_count > MAX_SNAPSHOT_PAGES_PER_CALENDAR:
                    raise ConfigurationError("calendar pagination exceeded the safe limit")
                params: dict[str, Any] = {
                    "singleEvents": True,
                    "showDeleted": False,
                    "timeMin": time_min.isoformat().replace("+00:00", "Z"),
                    "timeMax": time_max.isoformat().replace("+00:00", "Z"),
                }
                if page_token is not None:
                    params["pageToken"] = page_token
                response = _page_response(fetch_page(calendar_id, params), calendar_id)
                page_items = response.get("items", [])
                total_raw_events += len(page_items)
                if total_raw_events > MAX_SNAPSHOT_RAW_EVENTS:
                    raise ConfigurationError("calendar event count exceeded the safe limit")
                for index, raw_event in enumerate(page_items):
                    try:
                        converted = event_from_api(
                            calendar_id,
                            raw_event,
                            timezone_name=config.timezone,
                        )
                    except ConfigurationError as exc:
                        raise ConfigurationError(
                            f"calendar {calendar_id} item {index}: {exc}"
                        ) from None
                    if converted is None:
                        continue
                    prior = events.get(converted.event_id)
                    if prior is not None and not _same_event(prior, converted):
                        raise ConfigurationError(
                            f"calendar {calendar_id} event {converted.event_id.split(':', 1)[1]}: "
                            "conflicting duplicate event"
                        )
                    events[converted.event_id] = converted
                next_token = response.get("nextPageToken")
                if next_token is None:
                    break
                if next_token in seen_tokens:
                    raise ConfigurationError(
                        f"calendar {calendar_id}: repeated page token {next_token!r}"
                    )
                seen_tokens.add(next_token)
                page_token = next_token
        return CalendarSnapshot(
            status=SnapshotStatus.VALID,
            events=tuple(events.values()),
            fetched_at=now_utc,
            coverage_start=now_utc,
            coverage_end=now_utc + coverage_horizon,
            configuration_fingerprint=configuration_fingerprint(config),
            location_data_complete=True,
        )
    except Exception:
        # Google transport and parser exceptions can include authorization
        # headers, calendar IDs, event identifiers, or private event content.
        # Never persist an exception string in an observable snapshot.
        message = "calendar snapshot could not be completed"
        try:
            fetched_at = _utc_now(now)
        except ConfigurationError:
            fetched_at = None
        return CalendarSnapshot(
            status=SnapshotStatus.FAILED,
            events=(),
            fetched_at=fetched_at,
            error=message,
        )
