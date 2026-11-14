"""Clock-controlled Stage 2 acceptance tests; no IoX transport or live I/O."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from eisy_google_calendar.cached_schedule import CachedScheduleEngine, SchedulePolicy
from eisy_google_calendar.models import CalendarConfig, Rule, SnapshotStatus


UTC = timezone.utc
TARGET = 38


class FakeClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def set(self, value: datetime) -> None:
        assert value >= self.value
        self.value = value

    def advance(self, **change: int) -> None:
        self.value += timedelta(**change)


class MutableGoogle:
    def __init__(self, items=()):
        self.items = list(items)
        self.calls: list[tuple[str, dict]] = []
        self.failure: str | None = None

    def __call__(self, calendar_id, params):
        self.calls.append((calendar_id, dict(params)))
        if self.failure is not None:
            raise RuntimeError(self.failure)
        return {"items": list(self.items)}


def timed_event(
    event_id: str,
    title: str,
    start: str,
    end: str,
    *,
    status: str = "confirmed",
    recurring_event_id: str | None = None,
) -> dict:
    event = {
        "id": event_id,
        "summary": title,
        "status": status,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if recurring_event_id is not None:
        event["recurringEventId"] = recurring_event_id
    return event


def calendar_config(*rules: Rule, calendars=("synthetic",)) -> CalendarConfig:
    return CalendarConfig(
        tuple(calendars),
        "America/New_York",
        60,
        tuple(rules),
    )


def schedule(
    clock: FakeClock,
    provider,
    *rules: Rule,
    policy: SchedulePolicy | None = None,
    calendars=("synthetic",),
) -> CachedScheduleEngine:
    return CachedScheduleEngine(
        calendar_config(*rules, calendars=calendars),
        policy=policy or SchedulePolicy(refresh_hours=4),
        fetch_page=provider,
        clock=clock,
    )


def workshop_rule(**changes) -> Rule:
    values = {
        "name": "workshop",
        "title_contains": "Workshop",
        "target_variable_id": TARGET,
    }
    values.update(changes)
    return Rule(**values)


def test_adjusted_start_turns_on_and_adjusted_end_turns_off():
    clock = FakeClock(datetime(2025, 1, 15, 9, 44, tzinfo=UTC))
    google = MutableGoogle([
        timed_event(
            "private-event",
            "Workshop",
            "2025-01-15T10:00:00Z",
            "2025-01-15T11:00:00Z",
        )
    ])
    engine = schedule(
        clock,
        google,
        workshop_rule(start_offset_minutes=-15, end_offset_minutes=10),
    )

    assert engine.tick().preview.desired_states == {TARGET: 0}
    clock.set(datetime(2025, 1, 15, 9, 45, tzinfo=UTC))
    assert engine.tick().preview.desired_states == {TARGET: 1}
    clock.set(datetime(2025, 1, 15, 11, 9, tzinfo=UTC))
    assert engine.tick().preview.desired_states == {TARGET: 1}
    clock.set(datetime(2025, 1, 15, 11, 10, tzinfo=UTC))
    assert engine.tick().preview.desired_states == {TARGET: 0}
    assert len(google.calls) == 1


def test_overlapping_events_keep_state_on_until_final_event_expires():
    clock = FakeClock(datetime(2025, 1, 15, 9, 59, tzinfo=UTC))
    google = MutableGoogle([
        timed_event(
            "first",
            "Workshop",
            "2025-01-15T10:00:00Z",
            "2025-01-15T10:30:00Z",
        ),
        timed_event(
            "second",
            "Workshop",
            "2025-01-15T10:20:00Z",
            "2025-01-15T11:00:00Z",
        ),
    ])
    engine = schedule(clock, google, workshop_rule())
    assert engine.tick().preview.desired_states == {TARGET: 0}

    clock.set(datetime(2025, 1, 15, 10, 20, tzinfo=UTC))
    overlap = engine.tick().preview
    assert overlap.desired_states == {TARGET: 1}
    assert overlap.active_match_counts == {TARGET: 2}

    clock.set(datetime(2025, 1, 15, 10, 30, tzinfo=UTC))
    after_first = engine.tick().preview
    assert after_first.desired_states == {TARGET: 1}
    assert after_first.active_match_counts == {TARGET: 1}

    clock.set(datetime(2025, 1, 15, 11, 0, tzinfo=UTC))
    assert engine.tick().preview.desired_states == {TARGET: 0}
    assert len(google.calls) == 1


def test_multiple_matching_rules_for_one_variable_combine_with_logical_or():
    clock = FakeClock(datetime(2025, 1, 15, 9, 59, tzinfo=UTC))
    google = MutableGoogle([
        timed_event(
            "workshop-event",
            "Workshop",
            "2025-01-15T10:00:00Z",
            "2025-01-15T10:20:00Z",
        ),
        timed_event(
            "office-event",
            "Office hours",
            "2025-01-15T10:15:00Z",
            "2025-01-15T10:45:00Z",
        ),
    ])
    rules = (
        workshop_rule(),
        Rule("office", "Office", TARGET),
    )
    engine = schedule(clock, google, *rules)
    engine.tick()

    clock.set(datetime(2025, 1, 15, 10, 15, tzinfo=UTC))
    both = engine.tick().preview
    assert both.desired_states == {TARGET: 1}
    assert both.matched_rules[TARGET] == ("workshop", "office")

    clock.set(datetime(2025, 1, 15, 10, 20, tzinfo=UTC))
    office_only = engine.tick().preview
    assert office_only.desired_states == {TARGET: 1}
    assert office_only.matched_rules[TARGET] == ("office",)

    clock.set(datetime(2025, 1, 15, 10, 45, tzinfo=UTC))
    assert engine.tick().preview.desired_states == {TARGET: 0}
    assert len(google.calls) == 1


def test_recurring_instance_offsets_dst_cancelled_and_all_day_events():
    clock = FakeClock(datetime(2025, 3, 9, 5, 59, tzinfo=UTC))
    google = MutableGoogle([
        timed_event(
            "recurring-instance",
            "Workshop",
            "2025-03-09T01:30:00-05:00",
            "2025-03-09T03:30:00-04:00",
            recurring_event_id="private-series",
        ),
        timed_event(
            "cancelled",
            "Workshop",
            "2025-03-09T06:00:00Z",
            "2025-03-09T08:00:00Z",
            status="cancelled",
        ),
        {
            "id": "all-day",
            "summary": "Workshop",
            "status": "confirmed",
            "start": {"date": "2025-03-09"},
            "end": {"date": "2025-03-10"},
        },
    ])
    engine = schedule(
        clock,
        google,
        workshop_rule(start_offset_minutes=-30, end_offset_minutes=30),
    )
    assert engine.tick().preview.desired_states == {TARGET: 0}

    clock.set(datetime(2025, 3, 9, 6, 0, tzinfo=UTC))
    active = engine.tick().preview
    assert active.desired_states == {TARGET: 1}
    assert active.active_match_counts == {TARGET: 1}
    assert active.ignored_reasons == {"cancelled": 1, "all_day": 1}

    clock.set(datetime(2025, 3, 9, 7, 59, tzinfo=UTC))
    assert engine.tick().preview.desired_states == {TARGET: 1}
    clock.set(datetime(2025, 3, 9, 8, 0, tzinfo=UTC))
    assert engine.tick().preview.desired_states == {TARGET: 0}
    assert len(google.calls) == 1


@pytest.mark.parametrize(
    "event_start,event_end,on_at,off_at",
    [
        (
            "2025-03-09T01:30:00-05:00",
            "2025-03-09T03:30:00-04:00",
            datetime(2025, 3, 9, 6, 30, tzinfo=UTC),
            datetime(2025, 3, 9, 7, 30, tzinfo=UTC),
        ),
        (
            "2025-11-02T01:30:00-04:00",
            "2025-11-02T01:30:00-05:00",
            datetime(2025, 11, 2, 5, 30, tzinfo=UTC),
            datetime(2025, 11, 2, 6, 30, tzinfo=UTC),
        ),
    ],
)
def test_dst_boundaries_use_absolute_instants(
    event_start, event_end, on_at, off_at
):
    clock = FakeClock(on_at - timedelta(minutes=1))
    google = MutableGoogle([
        timed_event("dst-instance", "Workshop", event_start, event_end)
    ])
    engine = schedule(clock, google, workshop_rule())
    assert engine.tick().preview.desired_states == {TARGET: 0}
    clock.set(on_at)
    assert engine.tick().preview.desired_states == {TARGET: 1}
    clock.set(off_at - timedelta(minutes=1))
    assert engine.tick().preview.desired_states == {TARGET: 1}
    clock.set(off_at)
    assert engine.tick().preview.desired_states == {TARGET: 0}
    assert len(google.calls) == 1


def test_google_failure_keeps_fresh_on_state_then_stale_cache_withholds():
    clock = FakeClock(datetime(2025, 1, 15, 9, 0, tzinfo=UTC))
    google = MutableGoogle([
        timed_event(
            "long-event",
            "Workshop",
            "2025-01-15T08:00:00Z",
            "2025-01-15T20:00:00Z",
        )
    ])
    policy = SchedulePolicy(
        refresh_hours=1,
        max_snapshot_age=timedelta(hours=2),
        coverage_horizon=timedelta(hours=4),
    )
    engine = schedule(clock, google, workshop_rule(), policy=policy)
    assert engine.tick().preview.desired_states == {TARGET: 1}

    google.failure = "private Google outage"
    clock.set(datetime(2025, 1, 15, 10, 0, tzinfo=UTC))
    fresh_failure = engine.tick()
    assert fresh_failure.refresh_succeeded is False
    assert fresh_failure.preview.trusted is True
    assert fresh_failure.preview.desired_states == {TARGET: 1}

    clock.set(datetime(2025, 1, 15, 11, 1, tzinfo=UTC))
    stale = engine.tick().preview
    assert stale.snapshot_status is SnapshotStatus.STALE
    assert stale.trusted is False
    assert stale.desired_states == {}
    assert "private Google outage" not in (stale.reason or "")


@pytest.mark.parametrize("partial_first_calendar", [False, True])
def test_missing_complete_google_data_withholds_all_decisions(
    partial_first_calendar
):
    clock = FakeClock(datetime(2025, 1, 15, 9, 0, tzinfo=UTC))
    calls = []

    def incomplete_google(calendar_id, _params):
        calls.append(calendar_id)
        if partial_first_calendar and calendar_id == "first":
            return {
                "items": [
                    timed_event(
                        "partial",
                        "Workshop",
                        "2025-01-15T08:00:00Z",
                        "2025-01-15T20:00:00Z",
                    )
                ]
            }
        raise RuntimeError("private missing Google data")

    engine = schedule(
        clock,
        incomplete_google,
        workshop_rule(),
        calendars=("first", "second") if partial_first_calendar else ("first",),
    )
    result = engine.tick()
    assert result.refresh_succeeded is False
    assert result.preview.snapshot_status is SnapshotStatus.UNAVAILABLE
    assert result.preview.trusted is False
    assert result.preview.desired_states == {}
    assert engine.snapshot is None
    assert calls == (["first", "second"] if partial_first_calendar else ["first"])


def test_minute_cached_evaluation_does_not_make_extra_google_requests():
    clock = FakeClock(datetime(2025, 1, 15, 9, 0, tzinfo=UTC))
    google = MutableGoogle([
        timed_event(
            "minute-event",
            "Workshop",
            "2025-01-15T09:02:00Z",
            "2025-01-15T09:04:00Z",
        )
    ])
    engine = schedule(clock, google, workshop_rule())
    assert engine.tick().preview.desired_states == {TARGET: 0}

    clock.advance(seconds=30)
    assert engine.tick().preview is None
    clock.advance(seconds=30)
    assert engine.tick().preview.desired_states == {TARGET: 0}
    clock.advance(minutes=1)
    assert engine.tick().preview.desired_states == {TARGET: 1}
    clock.advance(minutes=1)
    assert engine.tick().preview.desired_states == {TARGET: 1}
    clock.advance(minutes=1)
    assert engine.tick().preview.desired_states == {TARGET: 0}
    assert len(google.calls) == 1
