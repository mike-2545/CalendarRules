"""Pure, offline calendar snapshot evaluation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Sequence, Set

from .models import (
    DEFAULT_TITLE_MATCH_TYPE,
    TITLE_MATCH_TYPES,
    CalendarEvent,
    CalendarSnapshot,
    EvaluationResult,
    Rule,
    SnapshotStatus,
)
from .validation import ConfigurationError, validate_event, validate_rule


def title_matches(
    event_title: str,
    pattern: str,
    *,
    match_type: str = DEFAULT_TITLE_MATCH_TYPE,
    case_sensitive: bool = False,
) -> bool:
    """Apply one supported title operation without exposing private titles."""

    if not isinstance(event_title, str):
        raise ConfigurationError("event title must be a string")
    if not isinstance(pattern, str) or not pattern:
        raise ConfigurationError("title pattern must be a non-empty string")
    if not isinstance(case_sensitive, bool):
        raise ConfigurationError("case_sensitive must be boolean")
    if match_type not in TITLE_MATCH_TYPES:
        raise ConfigurationError("title match type is invalid")
    if not case_sensitive:
        event_title = event_title.casefold()
        pattern = pattern.casefold()
    if match_type == "contains":
        return pattern in event_title
    if match_type == "exact":
        return pattern == event_title
    if match_type == "starts_with":
        return event_title.startswith(pattern)
    return event_title.endswith(pattern)


def event_matches_rule(event: CalendarEvent, rule: Rule) -> bool:
    """Match the configured event field without leaking its private value."""

    if rule.match_field == "title":
        value = event.title
    elif rule.match_field == "location":
        value = event.location
    else:
        raise ConfigurationError("match field is invalid")
    if not value:
        return False
    return title_matches(
        value,
        rule.title_contains,
        match_type=rule.title_match_type,
        case_sensitive=rule.case_sensitive,
    )


def rules_require_location_data(rules: Sequence[Rule]) -> bool:
    """Return whether complete provider location evidence is required."""

    return any(rule.match_field == "location" for rule in rules)


def evaluate_snapshot(
    snapshot: CalendarSnapshot,
    rules: Sequence[Rule],
    now: datetime,
) -> EvaluationResult:
    """Evaluate a valid snapshot or preserve failure/unavailability status.

    ``validate_event`` and ``now`` normalization ensure all interval arithmetic
    is performed in UTC. Offsets therefore represent elapsed minutes, not local
    wall-clock adjustments.
    """

    normalized_rules = tuple(validate_rule(rule) for rule in rules)
    now_utc = validate_event_time(now)
    target_ids = tuple(dict.fromkeys(rule.target_variable_id for rule in normalized_rules))

    if not isinstance(snapshot.status, SnapshotStatus):
        raise ConfigurationError("snapshot status is invalid")
    if type(snapshot.location_data_complete) is not bool:
        raise ConfigurationError("snapshot location completeness is invalid")

    if snapshot.status is not SnapshotStatus.VALID:
        return EvaluationResult(
            desired_states={},
            status=snapshot.status,
            trusted=False,
            diagnostics=(snapshot.error or snapshot.status.value,),
        )
    if rules_require_location_data(normalized_rules) and not snapshot.location_data_complete:
        return EvaluationResult(
            desired_states={},
            status=snapshot.status,
            trusted=False,
            diagnostics=("calendar snapshot lacks complete location data",),
        )

    desired: Dict[int, int] = {target_id: 0 for target_id in target_ids}
    active: Dict[int, List[str]] = {target_id: [] for target_id in target_ids}
    matched_rules: Dict[int, List[str]] = {target_id: [] for target_id in target_ids}
    diagnostics: List[str] = []
    ignored_event_ids: Set[str] = set()
    ignored_by_reason: Dict[str, Set[str]] = {
        "cancelled": set(),
        "all_day": set(),
        "unmatched": set(),
    }
    invalid_intervals = 0

    for raw_event in snapshot.events:
        event = validate_event(raw_event)
        event_ignored = False
        event_matched = False

        for rule in normalized_rules:
            if rule.ignore_cancelled and event.cancelled:
                event_ignored = True
                ignored_by_reason["cancelled"].add(event.event_id)
                continue
            if rule.ignore_all_day and event.all_day:
                event_ignored = True
                ignored_by_reason["all_day"].add(event.event_id)
                continue
            if not event_matches_rule(event, rule):
                continue
            event_matched = True

            adjusted_start = event.start + timedelta(minutes=rule.start_offset_minutes)
            adjusted_end = event.end + timedelta(minutes=rule.end_offset_minutes)
            if adjusted_end <= adjusted_start:
                invalid_intervals += 1
                diagnostics.append(
                    f"rule {rule.name!r} event {event.event_id!r}: "
                    "adjusted interval end is not after start"
                )
                continue

            if adjusted_start <= now_utc < adjusted_end:
                desired[rule.target_variable_id] = 1
                active[rule.target_variable_id].append(event.event_id)
                if rule.name not in matched_rules[rule.target_variable_id]:
                    matched_rules[rule.target_variable_id].append(rule.name)

        if event_ignored:
            ignored_event_ids.add(event.event_id)
        if not event_matched and not event_ignored:
            ignored_by_reason["unmatched"].add(event.event_id)

    if invalid_intervals:
        return EvaluationResult(
            desired_states={},
            status=snapshot.status,
            trusted=False,
            ignored_event_count=len(ignored_event_ids),
            invalid_interval_count=invalid_intervals,
            diagnostics=tuple(diagnostics),
            ignored_reasons={
                reason: len(event_ids)
                for reason, event_ids in ignored_by_reason.items()
                if event_ids
            },
        )

    return EvaluationResult(
        desired_states=desired,
        status=snapshot.status,
        trusted=True,
        active_matches={key: tuple(value) for key, value in active.items() if value},
        ignored_event_count=len(ignored_event_ids),
        invalid_interval_count=0,
        diagnostics=tuple(diagnostics),
        matched_rules={
            key: tuple(value) for key, value in matched_rules.items() if value
        },
        ignored_reasons={
            reason: len(event_ids)
            for reason, event_ids in ignored_by_reason.items()
            if event_ids
        },
    )


def validate_event_time(value: datetime) -> datetime:
    """Normalize the supplied evaluation time to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ConfigurationError("now must be timezone-aware")
    return value.astimezone(timezone.utc)
