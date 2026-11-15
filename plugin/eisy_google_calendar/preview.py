"""Side-effect-free calendar processing previews.

This module deliberately composes the existing Google response adapter and
rule evaluator.  It never imports the IoX reconciliation transport and cannot
read or write an IoX variable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Mapping, Optional, Tuple

from .calendar_api import FetchPage, fetch_snapshot
from .evaluator import evaluate_snapshot, validate_event_time
from .models import CalendarConfig, CalendarSnapshot, SnapshotStatus
from .validation import ConfigurationError


@dataclass(frozen=True)
class PreviewResult:
    """Privacy-safe result of an offline calendar evaluation.

    Event titles and provider event IDs are intentionally absent.  Rule names
    come from local configuration; instance diagnostics are aggregate counts.
    """

    evaluated_at: datetime
    snapshot_status: SnapshotStatus
    trusted: bool
    desired_states: Mapping[int, int]
    matched_rules: Mapping[int, Tuple[str, ...]]
    active_match_counts: Mapping[int, int]
    ignored_reasons: Mapping[str, int]
    source_event_count: Optional[int]
    reason: Optional[str] = None


_STATUS_REASONS = {
    SnapshotStatus.FAILED: "calendar snapshot could not be completed",
    SnapshotStatus.UNAVAILABLE: "calendar snapshot is unavailable",
    SnapshotStatus.STALE: "calendar snapshot is stale",
    SnapshotStatus.OUT_OF_COVERAGE: "evaluation time is outside snapshot coverage",
}


def _validate_max_age(value: Optional[timedelta]) -> Optional[timedelta]:
    if value is None:
        return None
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ConfigurationError("max_snapshot_age must be a positive timedelta")
    return value


def _trust_checked_snapshot(
    snapshot: CalendarSnapshot,
    *,
    now_utc: datetime,
    max_snapshot_age: Optional[timedelta],
    require_coverage: bool,
) -> CalendarSnapshot:
    if snapshot.status is not SnapshotStatus.VALID:
        return snapshot
    if snapshot.fetched_at is None:
        return replace(
            snapshot,
            status=SnapshotStatus.UNAVAILABLE,
            error="snapshot completion time is unavailable",
        )
    try:
        fetched_at = validate_event_time(snapshot.fetched_at)
    except (ConfigurationError, TypeError, ValueError):
        return replace(
            snapshot,
            status=SnapshotStatus.FAILED,
            error="snapshot completion time is invalid",
        )
    if fetched_at > now_utc:
        return replace(
            snapshot,
            status=SnapshotStatus.FAILED,
            error="snapshot completion time is later than evaluation time",
        )
    if max_snapshot_age is not None and now_utc - fetched_at > max_snapshot_age:
        return replace(
            snapshot,
            status=SnapshotStatus.STALE,
            error="snapshot exceeded the configured maximum age",
        )
    if not require_coverage:
        return replace(snapshot, fetched_at=fetched_at)
    if snapshot.coverage_start is None or snapshot.coverage_end is None:
        return replace(
            snapshot,
            status=SnapshotStatus.UNAVAILABLE,
            error="snapshot coverage is unavailable",
        )
    try:
        coverage_start = validate_event_time(snapshot.coverage_start)
        coverage_end = validate_event_time(snapshot.coverage_end)
    except (ConfigurationError, TypeError, ValueError):
        return replace(
            snapshot,
            status=SnapshotStatus.FAILED,
            error="snapshot coverage is invalid",
        )
    if coverage_end < coverage_start:
        return replace(
            snapshot,
            status=SnapshotStatus.FAILED,
            error="snapshot coverage is invalid",
        )
    if now_utc < coverage_start or now_utc > coverage_end:
        return replace(
            snapshot,
            status=SnapshotStatus.OUT_OF_COVERAGE,
            error="evaluation time is outside snapshot coverage",
        )
    return replace(
        snapshot,
        fetched_at=fetched_at,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
    )


def preview_snapshot(
    config: CalendarConfig,
    snapshot: CalendarSnapshot,
    *,
    now: datetime,
    max_snapshot_age: Optional[timedelta] = None,
    require_coverage: bool = False,
) -> PreviewResult:
    """Evaluate a complete offline snapshot without any external side effects.

    A completion timestamp is required.  Age expiry is disabled by default and
    is applied only when ``max_snapshot_age`` is explicitly supplied.
    """

    now_utc = validate_event_time(now)
    maximum_age = _validate_max_age(max_snapshot_age)
    checked = _trust_checked_snapshot(
        snapshot,
        now_utc=now_utc,
        max_snapshot_age=maximum_age,
        require_coverage=require_coverage,
    )

    try:
        evaluation = evaluate_snapshot(checked, config.rules, now_utc)
    except Exception:
        # Provider exception text may contain request data or private calendar
        # details.  The preview boundary intentionally returns a fixed reason.
        return PreviewResult(
            evaluated_at=now_utc,
            snapshot_status=SnapshotStatus.FAILED,
            trusted=False,
            desired_states={},
            matched_rules={},
            active_match_counts={},
            ignored_reasons={},
            source_event_count=None,
            reason="calendar snapshot could not be evaluated safely",
        )

    if not evaluation.trusted:
        status = evaluation.status
        if status is SnapshotStatus.VALID:
            status = SnapshotStatus.FAILED
        return PreviewResult(
            evaluated_at=now_utc,
            snapshot_status=status,
            trusted=False,
            desired_states={},
            matched_rules={},
            active_match_counts={},
            ignored_reasons={},
            source_event_count=None,
            reason=(
                "adjusted event interval is invalid"
                if evaluation.invalid_interval_count > 0
                else _STATUS_REASONS.get(
                    status, "calendar snapshot could not be evaluated safely"
                )
            ),
        )

    return PreviewResult(
        evaluated_at=now_utc,
        snapshot_status=evaluation.status,
        trusted=True,
        desired_states=dict(evaluation.desired_states),
        matched_rules=dict(evaluation.matched_rules),
        active_match_counts={
            target_id: len(event_ids)
            for target_id, event_ids in evaluation.active_matches.items()
        },
        ignored_reasons=dict(evaluation.ignored_reasons),
        source_event_count=len(checked.events),
    )


def preview_google_responses(
    config: CalendarConfig,
    fetch_page: FetchPage,
    *,
    now: datetime,
    max_snapshot_age: Optional[timedelta] = None,
    coverage_horizon: timedelta = timedelta(0),
    require_coverage: bool = False,
) -> PreviewResult:
    """Preview synthetic Google ``events.list`` pages using the real adapter."""

    snapshot = fetch_snapshot(
        config,
        fetch_page=fetch_page,
        now=now,
        coverage_horizon=coverage_horizon,
    )
    return preview_snapshot(
        config,
        snapshot,
        now=now,
        max_snapshot_age=max_snapshot_age,
        require_coverage=require_coverage,
    )


def format_preview(result: PreviewResult) -> str:
    """Format a result without exposing calendar titles or provider IDs."""

    lines = [
        "CalendarRules offline preview",
        f"Evaluation time: {result.evaluated_at.isoformat()}",
        f"Snapshot: {result.snapshot_status.value} "
        f"({'trusted' if result.trusted else 'untrusted'})",
    ]
    if not result.trusted:
        lines.append("Desired states: withheld")
        if result.reason:
            lines.append(f"Reason: {result.reason}")
        return "\n".join(lines)

    lines.append(f"Source events: {result.source_event_count}")
    lines.append("Desired states:")
    for target_id in sorted(result.desired_states):
        rule_names = result.matched_rules.get(target_id, ())
        matched = ", ".join(rule_names) if rule_names else "none"
        count = result.active_match_counts.get(target_id, 0)
        lines.append(
            f"  variable {target_id}: {result.desired_states[target_id]} "
            f"| matched rules: {matched} | active matches: {count}"
        )
    if result.ignored_reasons:
        ignored = ", ".join(
            f"{reason}={count}"
            for reason, count in sorted(result.ignored_reasons.items())
        )
        lines.append(f"Ignored events: {ignored}")
    return "\n".join(lines)
