"""Pure, bounded activity views derived from one trusted calendar snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config_identity import configuration_fingerprint
from .evaluator import event_matches_rule, validate_event_time
from .models import CalendarConfig, CalendarSnapshot
from .preview import preview_snapshot
from .validation import ConfigurationError, validate_event, validate_rule


MAX_ACTIVE_ITEMS = 4
MAX_UPCOMING_ITEMS = 6
MAX_VARIABLE_ITEMS = 8
MAX_TITLE_CHARS = 80
MAX_RULE_NAME_CHARS = 60
MAX_ACTIVITY_NOTICE_CHARS = 2000
MAX_COMPACT_NOTICE_CHARS = 200
MAX_COMPACT_TITLE_CHARS = 48
MAX_COMPACT_RULE_CHARS = 32
MAX_COMPACT_ACTIVE_ITEMS = 3
UPCOMING_NOTICE_LIMITS = frozenset({5, 10})


@dataclass(frozen=True)
class ActivityItem:
    """One event/rule automation window, with no provider identifiers."""

    title: str
    rule_name: str
    target_variable_id: int
    event_start: datetime
    event_end: datetime
    automation_start: datetime
    automation_end: datetime
    active: bool
    desired_state: int
    next_variable_transition: Optional[datetime]
    next_variable_value: Optional[int]
    target_interval_start: Optional[datetime] = None
    target_interval_end: Optional[datetime] = None
    target_already_on_at_start: bool = False


@dataclass(frozen=True)
class VariableActivity:
    """Union-derived desired state and next transition for one target."""

    target_variable_id: int
    desired_state: int
    next_transition: Optional[datetime]
    next_value: Optional[int]


@dataclass(frozen=True)
class ActivityResult:
    """Trusted activity details or a fixed, privacy-safe unavailable state."""

    evaluated_at: datetime
    trusted: bool
    cached: bool
    active: tuple[ActivityItem, ...] = ()
    upcoming: tuple[ActivityItem, ...] = ()
    variables: tuple[VariableActivity, ...] = ()
    reason: Optional[str] = None
    configuration_fingerprint: Optional[str] = None


@dataclass(frozen=True)
class _Window:
    title: str
    rule_name: str
    target_variable_id: int
    event_start: datetime
    event_end: datetime
    start: datetime
    end: datetime


def _unavailable(now: datetime, *, cached: bool) -> ActivityResult:
    return ActivityResult(
        evaluated_at=now,
        trusted=False,
        cached=cached,
        reason="calendar activity is unavailable because the schedule is not trusted",
    )


def _merged_intervals(windows: list[_Window]) -> list[tuple[datetime, datetime]]:
    """Return the interval union; adjacent windows deliberately remain ON."""

    merged: list[tuple[datetime, datetime]] = []
    for window in sorted(windows, key=lambda item: (item.start, item.end)):
        if not merged or window.start > merged[-1][1]:
            merged.append((window.start, window.end))
            continue
        prior_start, prior_end = merged[-1]
        merged[-1] = (prior_start, max(prior_end, window.end))
    return merged


def _variable_activity(
    target: int,
    intervals: list[tuple[datetime, datetime]],
    now: datetime,
) -> VariableActivity:
    for start, end in intervals:
        if start <= now < end:
            return VariableActivity(target, 1, end, 0)
        if start > now:
            return VariableActivity(target, 0, start, 1)
    return VariableActivity(target, 0, None, None)


def calculate_activity(
    config: CalendarConfig,
    snapshot: CalendarSnapshot,
    *,
    now: datetime,
    max_snapshot_age: timedelta,
    using_cached_data: bool = False,
) -> ActivityResult:
    """Calculate active/upcoming windows without network or IoX side effects.

    The snapshot must carry the canonical identity of the supplied validated
    configuration.  This makes activity restoration obey the same cache
    compatibility boundary as the schedule engine.
    """

    now_utc = validate_event_time(now)
    if not isinstance(using_cached_data, bool):
        raise ConfigurationError("using_cached_data must be boolean")
    try:
        expected_fingerprint = configuration_fingerprint(config)
    except Exception:
        return _unavailable(now_utc, cached=using_cached_data)
    if snapshot.configuration_fingerprint != expected_fingerprint:
        return _unavailable(now_utc, cached=using_cached_data)

    preview = preview_snapshot(
        config,
        snapshot,
        now=now_utc,
        max_snapshot_age=max_snapshot_age,
        require_coverage=True,
    )
    if not preview.trusted:
        return _unavailable(now_utc, cached=using_cached_data)

    try:
        rules = tuple(validate_rule(rule) for rule in config.rules)
        windows: list[_Window] = []
        for raw_event in snapshot.events:
            event = validate_event(raw_event)
            for rule in rules:
                if rule.ignore_cancelled and event.cancelled:
                    continue
                if rule.ignore_all_day and event.all_day:
                    continue
                if not event_matches_rule(event, rule):
                    continue
                start = event.start + timedelta(minutes=rule.start_offset_minutes)
                end = event.end + timedelta(minutes=rule.end_offset_minutes)
                if end <= start:
                    return _unavailable(now_utc, cached=using_cached_data)
                windows.append(
                    _Window(
                        event.title,
                        rule.name,
                        rule.target_variable_id,
                        event.start,
                        event.end,
                        start,
                        end,
                    )
                )
    except Exception:
        return _unavailable(now_utc, cached=using_cached_data)

    by_target: dict[int, list[_Window]] = {
        target: [] for target in sorted(preview.desired_states)
    }
    for window in windows:
        by_target.setdefault(window.target_variable_id, []).append(window)
    intervals_by_target = {
        target: _merged_intervals(by_target[target]) for target in sorted(by_target)
    }
    variable_states = tuple(
        _variable_activity(target, intervals_by_target[target], now_utc)
        for target in sorted(by_target)
    )
    variable_lookup = {item.target_variable_id: item for item in variable_states}

    def item(window: _Window) -> ActivityItem:
        variable = variable_lookup[window.target_variable_id]
        containing_interval = next(
            (
                (start, end)
                for start, end in intervals_by_target[window.target_variable_id]
                if start <= window.start < end
            ),
            None,
        )
        if containing_interval is None:
            raise ConfigurationError("automation window has no target interval")
        interval_start, interval_end = containing_interval
        return ActivityItem(
            title=window.title,
            rule_name=window.rule_name,
            target_variable_id=window.target_variable_id,
            event_start=window.event_start,
            event_end=window.event_end,
            automation_start=window.start,
            automation_end=window.end,
            active=window.start <= now_utc < window.end,
            desired_state=variable.desired_state,
            next_variable_transition=variable.next_transition,
            next_variable_value=variable.next_value,
            target_interval_start=interval_start,
            target_interval_end=interval_end,
            target_already_on_at_start=window.start > interval_start,
        )

    order = lambda window: (
        window.start,
        window.end,
        window.target_variable_id,
        window.rule_name.casefold(),
        window.title.casefold(),
        window.rule_name,
        window.title,
    )
    active = tuple(
        item(window)
        for window in sorted(windows, key=order)
        if window.start <= now_utc < window.end
    )
    upcoming = tuple(
        item(window)
        for window in sorted(windows, key=order)
        if window.start > now_utc
    )
    return ActivityResult(
        evaluated_at=now_utc,
        trusted=True,
        cached=using_cached_data,
        active=active,
        upcoming=upcoming,
        variables=variable_states,
        configuration_fingerprint=expected_fingerprint,
    )


def _bounded_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _local_time(value: datetime, zone: ZoneInfo) -> str:
    local = value.astimezone(zone)
    hour = local.hour % 12 or 12
    return f"{local:%b} {local.day}, {local.year} {hour}:{local:%M} " \
        f"{'AM' if local.hour < 12 else 'PM'} {local.tzname() or 'UTC'}"


def _compact_time(value: datetime, zone: ZoneInfo) -> str:
    local = value.astimezone(zone)
    hour = local.hour % 12 or 12
    return (
        f"{hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'} "
        f"{local.tzname() or 'UTC'}"
    )


def _compact_date(value: datetime, zone: ZoneInfo) -> str:
    local = value.astimezone(zone)
    return f"{local:%a %b} {local.day}"


def _limit_notice(value: str) -> str:
    """Sanitize and bound one notice for eisy-ui's collapsed-line display."""

    return _bounded_text(value, MAX_COMPACT_NOTICE_CHARS)


def format_activity_notices(
    result: ActivityResult,
    *,
    timezone_name: str,
    show_event_titles: bool,
    upcoming_event_limit: int,
) -> dict[str, str]:
    """Render a stable compact notice set from one trusted activity result.

    Upcoming entries represent event/rule matches, rather than unique provider
    events. This preserves every target attribution. When the configured bound
    omits matches, the summary reports the exact omitted count.
    """

    if upcoming_event_limit not in UPCOMING_NOTICE_LIMITS:
        raise ConfigurationError("upcoming_event_limit must be 5 or 10")
    if not result.trusted:
        return {
            "calendar_activity": (
                "Calendar activity unavailable: no trusted, compatible schedule."
            )
        }
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        zone = ZoneInfo("UTC")

    source = "Trusted cache" if result.cached else "Trusted schedule"
    shown_upcoming = result.upcoming[:upcoming_event_limit]
    omitted_upcoming = len(result.upcoming) - len(shown_upcoming)
    upcoming_count = str(len(result.upcoming))
    if omitted_upcoming:
        upcoming_count += f" (showing {len(shown_upcoming)})"
    notices = {
        "calendar_activity": _limit_notice(
            f"Calendar activity: {source} | Active {len(result.active)} | "
            f"Upcoming {upcoming_count}"
        )
    }

    def compact_title(item: ActivityItem) -> str:
        if not show_event_titles:
            return "[title hidden]"
        return _bounded_text(item.title, MAX_COMPACT_TITLE_CHARS)

    active_parts: list[str] = []
    for current in result.active[:MAX_COMPACT_ACTIVE_ITEMS]:
        interval_end = current.target_interval_end or current.automation_end
        active_parts.append(
            f"{compact_title(current)} | Var {current.target_variable_id} desired ON "
            f"| Next OFF {_compact_time(interval_end, zone)}"
        )
    if not active_parts:
        active_message = "Active: None"
    else:
        active_message = ""
        for shown_count in range(len(active_parts), 0, -1):
            omitted_active = len(result.active) - shown_count
            candidate = "Active: " + " · ".join(active_parts[:shown_count])
            if omitted_active:
                candidate += f" · +{omitted_active} more"
            if len(candidate) <= MAX_COMPACT_NOTICE_CHARS:
                active_message = candidate
                break
        if not active_message:
            active_message = _limit_notice(active_parts[0])
    notices["calendar_activity_active"] = active_message

    for index, future in enumerate(shown_upcoming, start=1):
        interval_start = future.target_interval_start or future.automation_start
        interval_end = future.target_interval_end or future.automation_end
        prefix = (
            f"{_compact_date(future.automation_start, zone)} · "
            f"{compact_title(future)} | "
            f"Rule {_bounded_text(future.rule_name, MAX_COMPACT_RULE_CHARS)} | "
            f"Var {future.target_variable_id}"
        )
        if future.target_already_on_at_start:
            transition = (
                f"Already desired ON since {_compact_time(interval_start, zone)} | "
                f"OFF {_compact_time(interval_end, zone)}"
            )
        else:
            transition = (
                f"ON {_compact_time(interval_start, zone)} | "
                f"OFF {_compact_time(interval_end, zone)}"
            )
        notices[f"calendar_activity_upcoming_{index:02d}"] = _limit_notice(
            f"{prefix} | {transition}"
        )
    return notices


def format_activity_notice(
    result: ActivityResult,
    *,
    timezone_name: str,
    show_event_titles: bool,
) -> str:
    """Render one bounded owner notice; titles are opt-in and never logged."""

    if not result.trusted:
        return "Calendar activity unavailable: no trusted, compatible schedule."
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        zone = ZoneInfo("UTC")
    source = "trusted cache" if result.cached else "trusted schedule"
    lines = [f"Calendar activity ({source})."]
    if not show_event_titles:
        lines.append("Event-title display is disabled in CalendarRules settings.")

    lines.append("Desired variables:")
    variables = result.variables[:MAX_VARIABLE_ITEMS]
    for variable in variables:
        desired = "ON" if variable.desired_state == 1 else "OFF"
        if variable.next_transition is None:
            transition_text = "no transition in snapshot"
        else:
            next_value = "ON" if variable.next_value == 1 else "OFF"
            transition_text = (
                f"next {next_value} {_local_time(variable.next_transition, zone)}"
            )
        lines.append(
            f"- variable {variable.target_variable_id} desired {desired} "
            f"| {transition_text}"
        )
    if len(result.variables) > len(variables):
        lines.append(
            f"- {len(result.variables) - len(variables)} more variable(s) omitted"
        )

    def title(item: ActivityItem) -> str:
        if not show_event_titles:
            return "[title hidden]"
        return _bounded_text(item.title, MAX_TITLE_CHARS)

    def transition(item: ActivityItem) -> str:
        if item.next_variable_transition is None:
            return "no transition in snapshot"
        value = "ON" if item.next_variable_value == 1 else "OFF"
        return f"next {value} {_local_time(item.next_variable_transition, zone)}"

    active = result.active[:MAX_ACTIVE_ITEMS]
    lines.append("Active:")
    if not active:
        lines.append("- none")
    for current in active:
        lines.append(
            f"- {title(current)} | rule {_bounded_text(current.rule_name, MAX_RULE_NAME_CHARS)} "
            f"| event {_local_time(current.event_start, zone)}–{_local_time(current.event_end, zone)} "
            f"| automation {_local_time(current.automation_start, zone)}–{_local_time(current.automation_end, zone)} "
            f"| variable {current.target_variable_id} desired ON | {transition(current)}"
        )
    if len(result.active) > len(active):
        lines.append(f"- {len(result.active) - len(active)} more active match(es) omitted")

    upcoming = result.upcoming[:MAX_UPCOMING_ITEMS]
    lines.append("Upcoming:")
    if not upcoming:
        lines.append("- none")
    for future in upcoming:
        if future.target_interval_start is None or future.target_interval_end is None:
            expected = "target interval unavailable"
        elif future.target_already_on_at_start:
            expected = (
                "target already desired ON at event start; interval ends "
                f"{_local_time(future.target_interval_end, zone)}"
            )
        else:
            expected = (
                f"expected ON {_local_time(future.target_interval_start, zone)}; "
                f"interval ends {_local_time(future.target_interval_end, zone)}"
            )
        lines.append(
            f"- {title(future)} | rule {_bounded_text(future.rule_name, MAX_RULE_NAME_CHARS)} "
            f"| event {_local_time(future.event_start, zone)}–{_local_time(future.event_end, zone)} "
            f"| automation {_local_time(future.automation_start, zone)}–{_local_time(future.automation_end, zone)} "
            f"| variable {future.target_variable_id} | {expected}"
        )
    if len(result.upcoming) > len(upcoming):
        lines.append(f"- {len(result.upcoming) - len(upcoming)} more upcoming match(es) omitted")
    rendered = "\n".join(lines)
    if len(rendered) <= MAX_ACTIVITY_NOTICE_CHARS:
        return rendered
    suffix = "\n- additional activity omitted to fit the display limit"
    return rendered[: MAX_ACTIVITY_NOTICE_CHARS - len(suffix)].rstrip() + suffix
