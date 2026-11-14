from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from eisy_google_calendar.cached_schedule import (
    ALLOWED_REFRESH_HOURS,
    DEFAULT_REFRESH_HOURS,
    CachedScheduleEngine,
    SchedulePolicy,
)
from eisy_google_calendar.models import CalendarConfig, Rule, SnapshotStatus
from eisy_google_calendar.snapshot_store import FileSnapshotStorage
from eisy_google_calendar.validation import ConfigurationError


UTC = timezone.utc
NINE = datetime(2025, 1, 1, 9, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


def raw_event(
    event_id="event",
    summary="Workshop",
    start="2025-01-01T10:30:00Z",
    end="2025-01-01T11:30:00Z",
    status="confirmed",
    **extra,
):
    return {
        "id": event_id,
        "summary": summary,
        "status": status,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        **extra,
    }


def config(*rules, calendars=("synthetic",)):
    return CalendarConfig(
        calendars,
        "America/New_York",
        60,
        tuple(rules or (Rule("workshop-rule", "Workshop", 101),)),
    )


class FakeClock:
    def __init__(self, value=NINE):
        self.value = value

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


class FakeProvider:
    def __init__(self, items=()):
        self.items = list(items)
        self.calls = []
        self.failure = None

    def __call__(self, calendar_id, params):
        self.calls.append((calendar_id, dict(params)))
        if self.failure is not None:
            raise RuntimeError(self.failure)
        return {"items": list(self.items)}


class MemoryStorage:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot
        self.saves = 0

    def load(self):
        return self.snapshot

    def save(self, snapshot):
        self.snapshot = snapshot
        self.saves += 1


def engine(clock, provider, *, policy=None, storage=None, rules=(), calendars=("synthetic",)):
    return CachedScheduleEngine(
        config(*rules, calendars=calendars),
        policy=policy or SchedulePolicy(),
        fetch_page=provider,
        clock=clock,
        storage=storage,
    )


@pytest.mark.parametrize(
    "replacement",
    [
        raw_event(start="2025-01-01T08:00:00Z", end="2025-01-01T08:30:00Z"),
        raw_event(
            start="2025-01-01T08:00:00Z",
            end="2025-01-01T12:00:00Z",
            status="cancelled",
        ),
    ],
    ids=("shortened", "cancelled"),
)
def test_refresh_now_replaces_changed_event_cache_and_recalculates_off(replacement):
    clock = FakeClock(datetime(2025, 1, 1, 9, tzinfo=UTC))
    provider = FakeProvider([
        raw_event(start="2025-01-01T08:00:00Z", end="2025-01-01T12:00:00Z")
    ])
    storage = MemoryStorage()
    schedule = engine(
        clock,
        provider,
        policy=SchedulePolicy(refresh_hours=4),
        storage=storage,
    )

    initial = schedule.tick()
    assert initial.preview.desired_states == {101: 1}
    assert storage.saves == 1

    provider.items = [replacement]
    assert schedule.request_refresh() is True
    refreshed = schedule.tick()

    assert refreshed.refresh_attempted is True
    assert refreshed.refresh_succeeded is True
    assert refreshed.preview.desired_states == {101: 0}
    assert storage.saves == 2
    assert storage.snapshot == schedule.snapshot
    assert refreshed.next_refresh_at == clock.value + timedelta(hours=4)


def test_failed_refresh_now_retains_cache_and_local_transitions():
    clock = FakeClock(datetime(2025, 1, 1, 9, tzinfo=UTC))
    provider = FakeProvider([
        raw_event(start="2025-01-01T08:00:00Z", end="2025-01-01T10:00:00Z")
    ])
    storage = MemoryStorage()
    schedule = engine(clock, provider, storage=storage)
    initial = schedule.tick()
    trusted = schedule.snapshot
    assert initial.preview.desired_states == {101: 1}

    provider.failure = "PRIVATE GOOGLE FAILURE"
    assert schedule.request_refresh() is True
    failed = schedule.tick()
    assert failed.refresh_succeeded is False
    assert failed.preview.trusted is True
    assert failed.preview.desired_states == {101: 1}
    assert schedule.snapshot is trusted
    assert storage.saves == 1

    clock.set(datetime(2025, 1, 1, 10, 1, tzinfo=UTC))
    expired = schedule.tick()
    assert expired.preview.trusted is True
    assert expired.preview.desired_states == {101: 0}
    assert schedule.snapshot is trusted
    assert storage.saves == 1
    assert "PRIVATE" not in (expired.warning or "")


def test_refresh_choices_are_one_two_or_four_hours_with_two_default():
    assert DEFAULT_REFRESH_HOURS == 2
    assert ALLOWED_REFRESH_HOURS == (1, 2, 4)
    assert SchedulePolicy().refresh_interval == timedelta(hours=2)
    for hours in ALLOWED_REFRESH_HOURS:
        assert SchedulePolicy(refresh_hours=hours).refresh_interval == timedelta(
            hours=hours
        )
    for invalid in (0, 3, 8, True):
        with pytest.raises(ConfigurationError, match="1, 2, or 4"):
            SchedulePolicy(refresh_hours=invalid)


def test_policy_requires_minute_evaluation_and_coverage_through_refresh():
    with pytest.raises(ConfigurationError, match="one minute"):
        SchedulePolicy(evaluation_interval=timedelta(seconds=30))
    with pytest.raises(ConfigurationError, match="one refresh interval"):
        SchedulePolicy(refresh_hours=4, coverage_horizon=timedelta(hours=2))
    with pytest.raises(ConfigurationError, match="must not exceed 32 days"):
        SchedulePolicy(coverage_horizon=timedelta(days=32, minutes=1))


def test_30_day_age_and_32_day_coverage_defaults_are_explicit():
    default = SchedulePolicy()
    custom = SchedulePolicy(
        max_snapshot_age=timedelta(hours=8),
        coverage_horizon=timedelta(hours=12),
    )
    assert default.max_snapshot_age == timedelta(days=30)
    assert default.coverage_horizon == timedelta(days=32)
    assert custom.max_snapshot_age == timedelta(hours=8)
    assert custom.coverage_horizon == timedelta(hours=12)

    evaluator_source = (
        ROOT / "src" / "eisy_google_calendar" / "evaluator.py"
    ).read_text()
    assert "SchedulePolicy" not in evaluator_source
    assert "max_snapshot_age" not in evaluator_source
    assert "coverage_horizon" not in evaluator_source


def test_planner_has_no_live_google_iox_or_background_timer_dependencies():
    source = "\n".join(
        (
            ROOT / "src" / "eisy_google_calendar" / name
        ).read_text()
        for name in ("cached_schedule.py", "snapshot_store.py")
    )
    for forbidden in (
        "googleapiclient",
        "requests.",
        "udi_interface",
        "iox_variables",
        "reconciler",
        "threading",
        "asyncio",
        "sleep(",
    ):
        assert forbidden not in source


def test_future_event_fetched_at_nine_drives_minute_local_evaluation():
    clock = FakeClock()
    provider = FakeProvider([raw_event()])
    schedule = engine(clock, provider)

    first = schedule.tick()
    assert first.refresh_succeeded is True
    assert first.preview.desired_states == {101: 0}
    assert provider.calls[0][1]["timeMax"] == "2025-02-02T09:01:00Z"

    clock.set(datetime(2025, 1, 1, 10, 29, tzinfo=UTC))
    before = schedule.tick()
    clock.advance(minutes=1)
    active = schedule.tick()

    assert before.refresh_attempted is False
    assert before.preview.desired_states == {101: 0}
    assert active.refresh_attempted is False
    assert active.preview.desired_states == {101: 1}
    assert len(provider.calls) == 1


@pytest.mark.parametrize("minutes", [30, 120, 240, 1440])
def test_single_fetch_supports_events_through_declared_horizon(minutes):
    clock = FakeClock()
    event_start = NINE + timedelta(minutes=minutes)
    provider = FakeProvider(
        [
            raw_event(
                start=event_start.isoformat().replace("+00:00", "Z"),
                end=(event_start + timedelta(hours=1))
                .isoformat()
                .replace("+00:00", "Z"),
            )
        ]
    )
    schedule = engine(clock, provider)
    assert schedule.tick().preview.desired_states == {101: 0}
    provider.failure = "offline after complete fetch"

    clock.set(event_start)
    result = schedule.tick()
    assert result.preview.trusted is True
    assert result.preview.desired_states == {101: 1}
    assert schedule.snapshot.fetched_at == NINE


def test_local_evaluation_runs_once_per_minute_without_google_refresh():
    clock = FakeClock()
    provider = FakeProvider([])
    schedule = engine(clock, provider)
    assert schedule.tick().preview is not None

    clock.advance(seconds=30)
    half_minute = schedule.tick()
    clock.advance(seconds=30)
    next_minute = schedule.tick()

    assert half_minute.preview is None
    assert next_minute.preview is not None
    assert len(provider.calls) == 1


@pytest.mark.parametrize("refresh_hours", ALLOWED_REFRESH_HOURS)
def test_selected_refresh_interval_controls_next_google_attempt(refresh_hours):
    clock = FakeClock()
    provider = FakeProvider([])
    schedule = engine(
        clock, provider, policy=SchedulePolicy(refresh_hours=refresh_hours)
    )
    first = schedule.tick()
    assert first.next_refresh_at == NINE + timedelta(hours=refresh_hours)
    clock.set(first.next_refresh_at - timedelta(seconds=1))
    assert schedule.tick().refresh_attempted is False
    clock.advance(seconds=1)
    assert schedule.tick().refresh_succeeded is True
    assert len(provider.calls) == 2


def test_changed_refresh_policy_is_immediate_but_duplicate_callback_is_noop():
    clock = FakeClock()
    provider = FakeProvider([])
    schedule = engine(clock, provider, policy=SchedulePolicy(refresh_hours=4))
    assert schedule.tick().refresh_succeeded is True
    assert len(provider.calls) == 1

    clock.advance(minutes=1)
    assert schedule.reconfigure(config(), SchedulePolicy(refresh_hours=4)) is False
    duplicate = schedule.tick()
    assert duplicate.refresh_attempted is False
    assert len(provider.calls) == 1

    assert schedule.reconfigure(config(), SchedulePolicy(refresh_hours=1)) is True
    changed = schedule.tick()
    assert changed.refresh_attempted is True
    assert changed.refresh_succeeded is True
    assert len(provider.calls) == 2


def test_negative_start_and_positive_end_offsets_are_available_from_cache():
    clock = FakeClock()
    provider = FakeProvider(
        [
            raw_event(
                "early",
                start="2025-01-01T11:00:00Z",
                end="2025-01-01T11:30:00Z",
            ),
            raw_event(
                "late",
                start="2025-01-01T08:00:00Z",
                end="2025-01-01T08:30:00Z",
            ),
        ]
    )
    rules = (
        Rule("early", "Workshop", 101, start_offset_minutes=-120),
        Rule("late", "Workshop", 202, end_offset_minutes=60),
    )
    schedule = engine(clock, provider, rules=rules)
    result = schedule.tick()

    assert result.preview.desired_states == {101: 1, 202: 1}
    params = provider.calls[0][1]
    assert params["timeMin"] == "2025-01-01T07:59:00Z"
    assert params["timeMax"] == "2025-02-02T11:01:00Z"


def test_overlapping_recurring_and_cancelled_events_use_existing_evaluator():
    clock = FakeClock(datetime(2025, 3, 9, 7, 15, tzinfo=UTC))
    provider = FakeProvider(
        [
            raw_event(
                "recurring",
                start="2025-03-09T01:30:00-05:00",
                end="2025-03-09T04:00:00-04:00",
                recurringEventId="series",
            ),
            raw_event(
                "overlap",
                start="2025-03-09T03:00:00-04:00",
                end="2025-03-09T04:30:00-04:00",
            ),
            raw_event(
                "cancelled",
                start="2025-03-09T06:30:00Z",
                end="2025-03-09T08:00:00Z",
                status="cancelled",
            ),
        ]
    )
    result = engine(clock, provider).tick().preview
    assert result.desired_states == {101: 1}
    assert result.active_match_counts == {101: 2}
    assert result.ignored_reasons == {"cancelled": 1}


def test_failed_refresh_retains_snapshot_and_uses_bounded_retry_backoff():
    clock = FakeClock()
    provider = FakeProvider(
        [
            raw_event(
                start="2025-01-01T09:00:00Z",
                end="2025-01-01T10:00:00Z",
            )
        ]
    )
    storage = MemoryStorage()
    schedule = engine(clock, provider, storage=storage)
    assert schedule.tick().refresh_succeeded is True
    original = schedule.snapshot

    provider.failure = "private provider failure body"
    clock.advance(hours=2)
    failed = schedule.tick()
    assert failed.refresh_succeeded is False
    assert failed.next_refresh_at == clock.value + timedelta(minutes=15)
    assert schedule.snapshot is original
    assert storage.saves == 1
    assert failed.preview.trusted is True
    assert failed.preview.desired_states == {101: 0}
    assert "private provider failure body" not in failed.warning

    clock.advance(minutes=15)
    second = schedule.tick()
    assert second.next_refresh_at == clock.value + timedelta(minutes=30)
    clock.advance(minutes=30)
    third = schedule.tick()
    assert third.next_refresh_at == clock.value + timedelta(hours=1)
    clock.advance(hours=1)
    fourth = schedule.tick()
    assert fourth.next_refresh_at == clock.value + timedelta(hours=1)


def test_successful_retry_clears_failure_warning_and_replaces_snapshot():
    clock = FakeClock()
    provider = FakeProvider([])
    schedule = engine(clock, provider)
    schedule.tick()
    provider.failure = "temporary private failure"
    clock.advance(hours=2)
    assert schedule.tick().warning is not None
    provider.failure = None
    provider.items = [raw_event(start="2025-01-01T11:00:00Z")]
    clock.advance(minutes=15)
    recovered = schedule.tick()
    assert recovered.refresh_succeeded is True
    assert recovered.warning is None
    assert len(schedule.snapshot.events) == 1


def test_partial_multi_calendar_failure_does_not_replace_last_complete_snapshot():
    clock = FakeClock()
    responses = {
        "one": [raw_event("one")],
        "two": [raw_event("two")],
    }
    fail_second = False

    def provider(calendar_id, params):
        if fail_second and calendar_id == "two":
            raise RuntimeError("second private calendar unavailable")
        return {"items": responses[calendar_id]}

    schedule = engine(clock, provider, calendars=("one", "two"))
    assert schedule.tick().refresh_succeeded is True
    original = schedule.snapshot
    fail_second = True
    clock.advance(hours=2)
    failed = schedule.tick()
    assert failed.refresh_succeeded is False
    assert schedule.snapshot is original
    assert len(schedule.snapshot.events) == 2


def test_partial_page_failure_does_not_replace_last_complete_snapshot():
    clock = FakeClock()
    partial = False

    def provider(calendar_id, params):
        if not partial:
            return {"items": [raw_event("complete")]}
        if "pageToken" not in params:
            return {
                "items": [raw_event("partial")],
                "nextPageToken": "second-page",
            }
        raise RuntimeError("private second page failure")

    schedule = engine(clock, provider)
    assert schedule.tick().refresh_succeeded is True
    original = schedule.snapshot
    partial = True
    clock.advance(hours=2)
    failed = schedule.tick()
    assert failed.refresh_succeeded is False
    assert schedule.snapshot is original
    assert [event.event_id for event in schedule.snapshot.events] == [
        "synthetic:complete"
    ]


def test_complete_edits_cancellations_and_deletes_replace_cache_at_refresh():
    clock = FakeClock()
    provider = FakeProvider([raw_event(start="2025-01-01T09:00:00Z")])
    schedule = engine(clock, provider, policy=SchedulePolicy(refresh_hours=1))
    assert schedule.tick().preview.desired_states == {101: 1}

    provider.items = [
        raw_event(
            status="cancelled",
            start="2025-01-01T09:00:00Z",
            end="2025-01-01T12:00:00Z",
        )
    ]
    clock.advance(hours=1)
    cancelled = schedule.tick()
    assert cancelled.refresh_succeeded is True
    assert cancelled.preview.desired_states == {101: 0}

    provider.items = [
        raw_event(
            start="2025-01-02T08:00:00Z",
            end="2025-01-02T09:00:00Z",
        )
    ]
    clock.advance(hours=1)
    moved = schedule.tick()
    assert moved.preview.desired_states == {101: 0}

    provider.items = []
    clock.advance(hours=1)
    deleted = schedule.tick()
    assert deleted.preview.trusted is True
    assert deleted.preview.desired_states == {101: 0}
    assert schedule.snapshot.events == ()


def test_restart_loads_verified_snapshot_and_survives_provider_outage(tmp_path):
    path = tmp_path / "calendar-cache" / "snapshot.json"
    storage = FileSnapshotStorage(path)
    first_clock = FakeClock()
    first_provider = FakeProvider(
        [raw_event(start="2025-01-01T09:30:00Z", end="2025-01-01T11:30:00Z")]
    )
    first = engine(first_clock, first_provider, storage=storage)
    assert first.tick().preview.desired_states == {101: 0}

    restart_clock = FakeClock(datetime(2025, 1, 1, 10, tzinfo=UTC))
    offline = FakeProvider()
    offline.failure = "private outage details"
    restarted = engine(restart_clock, offline, storage=storage)
    result = restarted.tick()

    assert result.refresh_succeeded is False
    assert result.preview.trusted is True
    assert result.preview.desired_states == {101: 1}
    assert restarted.snapshot.events[0].event_id.startswith("cached:")

    after_clock = FakeClock(datetime(2025, 1, 1, 12, tzinfo=UTC))
    after_restart = engine(after_clock, offline, storage=storage).tick()
    assert after_restart.refresh_succeeded is False
    assert after_restart.preview.trusted is True
    assert after_restart.preview.desired_states == {101: 0}


def test_calendar_change_rejects_persisted_empty_schedule_during_outage():
    storage = MemoryStorage()
    original = engine(
        FakeClock(),
        FakeProvider([]),
        storage=storage,
        calendars=("private-original",),
    )
    assert original.tick().preview.desired_states == {101: 0}
    assert storage.snapshot.events == ()

    offline = FakeProvider()
    offline.failure = "private changed-calendar failure"
    restarted = engine(
        FakeClock(),
        offline,
        storage=storage,
        calendars=("private-replacement",),
    )
    result = restarted.tick()

    assert result.refresh_succeeded is False
    assert result.preview.trusted is False
    assert result.preview.desired_states == {}
    assert result.preview.snapshot_status is SnapshotStatus.UNAVAILABLE
    assert restarted.snapshot is None
    assert "private-original" not in result.warning
    assert "private-replacement" not in result.warning


@pytest.mark.parametrize(
    "changed_rule",
    [
        Rule("changed-name", "Workshop", 101),
        Rule("workshop-rule", "Different matcher", 101),
        Rule("workshop-rule", "Workshop", 202),
        Rule("workshop-rule", "Workshop", 101, start_offset_minutes=-30),
        Rule("workshop-rule", "Workshop", 101, end_offset_minutes=45),
        Rule("workshop-rule", "Workshop", 101, case_sensitive=True),
        Rule("workshop-rule", "Workshop", 101, title_match_type="exact"),
        Rule("workshop-rule", "Workshop", 101, ignore_all_day=False),
        Rule("workshop-rule", "Workshop", 101, ignore_cancelled=False),
    ],
)
def test_rule_or_offset_change_rejects_persisted_schedule(changed_rule):
    storage = MemoryStorage()
    original_rule = Rule("workshop-rule", "Workshop", 101)
    assert engine(
        FakeClock(), FakeProvider([]), storage=storage, rules=(original_rule,)
    ).tick().preview.desired_states == {101: 0}

    offline = FakeProvider()
    offline.failure = "private rule-change failure"
    result = engine(
        FakeClock(), offline, storage=storage, rules=(changed_rule,)
    ).tick()

    assert result.preview.trusted is False
    assert result.preview.desired_states == {}
    assert result.preview.snapshot_status is SnapshotStatus.UNAVAILABLE


def test_matching_configuration_restores_valid_empty_schedule():
    storage = MemoryStorage()
    initial = engine(FakeClock(), FakeProvider([]), storage=storage)
    assert initial.tick().preview.desired_states == {101: 0}

    offline = FakeProvider()
    offline.failure = "private restart outage"
    result = engine(FakeClock(), offline, storage=storage).tick()

    assert result.refresh_succeeded is False
    assert result.preview.trusted is True
    assert result.preview.desired_states == {101: 0}
    assert result.preview.snapshot_status is SnapshotStatus.VALID


def test_explicit_default_contains_restores_legacy_cached_schedule():
    storage = MemoryStorage()
    legacy = Rule("workshop-rule", "Workshop", 101)
    initial = engine(
        FakeClock(), FakeProvider([]), storage=storage, rules=(legacy,)
    )
    assert initial.tick().preview.desired_states == {101: 0}

    offline = FakeProvider()
    offline.failure = "private restart outage"
    explicit = Rule(
        "workshop-rule", "Workshop", 101, title_match_type="contains"
    )
    restored = engine(
        FakeClock(), offline, storage=storage, rules=(explicit,)
    ).tick()

    assert restored.refresh_succeeded is False
    assert restored.preview.trusted is True
    assert restored.preview.desired_states == {101: 0}


def test_configuration_mismatch_allows_states_after_complete_new_snapshot():
    storage = MemoryStorage()
    assert engine(
        FakeClock(),
        FakeProvider([]),
        storage=storage,
        calendars=("old-calendar",),
    ).tick().preview.desired_states == {101: 0}

    refreshed = engine(
        FakeClock(),
        FakeProvider([raw_event(start="2025-01-01T09:00:00Z")]),
        storage=storage,
        calendars=("new-calendar",),
    ).tick()

    assert refreshed.refresh_succeeded is True
    assert refreshed.preview.trusted is True
    assert refreshed.preview.desired_states == {101: 1}
    assert storage.snapshot.configuration_fingerprint is not None


@pytest.mark.parametrize(
    "changed",
    [
        config(calendars=("replacement-calendar",)),
        config(Rule("changed-rule", "Different", 101)),
        config(
            Rule(
                "workshop-rule",
                "Workshop",
                101,
                start_offset_minutes=-30,
                end_offset_minutes=15,
            )
        ),
    ],
)
def test_in_memory_snapshot_is_invalidated_when_configuration_changes(changed):
    clock = FakeClock()
    provider = FakeProvider(
        [raw_event(start="2025-01-01T09:00:00Z", end="2025-01-01T10:00:00Z")]
    )
    schedule = engine(
        clock,
        provider,
        storage=MemoryStorage(),
        calendars=("original-calendar",),
    )
    assert schedule.tick().preview.desired_states == {101: 1}
    assert schedule.snapshot is not None

    assert schedule.reconfigure(changed, SchedulePolicy()) is True
    assert schedule.snapshot is None
    provider.failure = "private configuration refresh failure"
    result = schedule.tick()

    assert result.refresh_attempted is True
    assert result.refresh_succeeded is False
    assert result.preview.trusted is False
    assert result.preview.desired_states == {}
    assert result.preview.snapshot_status is SnapshotStatus.UNAVAILABLE


def test_invalid_partial_configuration_suspends_refresh_and_decisions():
    clock = FakeClock()
    provider = FakeProvider(
        [raw_event(start="2025-01-01T09:00:00Z", end="2025-01-01T10:00:00Z")]
    )
    schedule = engine(clock, provider)
    assert schedule.tick().preview.desired_states == {101: 1}
    calls_before_suspend = len(provider.calls)

    schedule.invalidate_configuration()
    clock.advance(minutes=1)
    result = schedule.tick()

    assert schedule.configuration_active is False
    assert schedule.snapshot is None
    assert result.refresh_attempted is False
    assert result.refresh_succeeded is None
    assert result.preview.trusted is False
    assert result.preview.desired_states == {}
    assert len(provider.calls) == calls_before_suspend


def test_reapplying_configuration_after_suspension_requires_complete_refresh():
    clock = FakeClock()
    provider = FakeProvider([])
    schedule = engine(clock, provider)
    assert schedule.tick().preview.desired_states == {101: 0}

    schedule.invalidate_configuration()
    provider.failure = "private provider outage"
    assert schedule.reconfigure(config(), SchedulePolicy()) is True
    failed = schedule.tick()
    assert failed.preview.trusted is False
    assert failed.preview.desired_states == {}

    provider.failure = None
    clock.set(failed.next_refresh_at)
    recovered = schedule.tick()
    assert recovered.refresh_succeeded is True
    assert recovered.preview.trusted is True
    assert recovered.preview.desired_states == {101: 0}


@pytest.mark.parametrize(
    "policy,elapsed,expected_status",
    [
        (
            SchedulePolicy(
                refresh_hours=1,
                max_snapshot_age=timedelta(hours=2),
                coverage_horizon=timedelta(hours=4),
            ),
            timedelta(hours=2, minutes=1),
            SnapshotStatus.STALE,
        ),
        (
            SchedulePolicy(
                refresh_hours=4,
                max_snapshot_age=timedelta(hours=8),
                coverage_horizon=timedelta(hours=4),
            ),
            timedelta(hours=4, minutes=1),
            SnapshotStatus.OUT_OF_COVERAGE,
        ),
    ],
)
def test_restart_enforces_freshness_and_coverage_independently(
    policy, elapsed, expected_status
):
    storage = MemoryStorage()
    initial = engine(FakeClock(), FakeProvider([]), policy=policy, storage=storage)
    assert initial.tick().refresh_succeeded is True

    offline = FakeProvider([])
    offline.failure = "private provider failure"
    restarted = engine(
        FakeClock(NINE + elapsed),
        offline,
        policy=policy,
        storage=storage,
    )
    result = restarted.tick()
    assert result.refresh_succeeded is False
    assert result.preview.snapshot_status is expected_status
    assert result.preview.trusted is False
    assert result.preview.desired_states == {}


def test_corrupt_restart_cache_and_failed_provider_withhold_all_states(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text("private corrupt calendar data")
    provider = FakeProvider()
    provider.failure = "private fetch response"
    schedule = engine(FakeClock(), provider, storage=FileSnapshotStorage(path))
    result = schedule.tick()

    assert result.preview.trusted is False
    assert result.preview.desired_states == {}
    assert result.preview.snapshot_status is SnapshotStatus.UNAVAILABLE
    assert "private" not in result.warning


def test_freshness_boundary_is_trusted_but_one_minute_later_is_stale():
    clock = FakeClock()
    provider = FakeProvider([])
    policy = SchedulePolicy(
        refresh_hours=1,
        max_snapshot_age=timedelta(hours=2),
        coverage_horizon=timedelta(hours=4),
    )
    schedule = engine(clock, provider, policy=policy)
    schedule.tick()
    provider.failure = "offline"

    clock.advance(hours=2)
    boundary = schedule.tick()
    assert boundary.preview.trusted is True
    assert boundary.preview.desired_states == {101: 0}

    clock.advance(minutes=1)
    stale = schedule.tick()
    assert stale.preview.snapshot_status is SnapshotStatus.STALE
    assert stale.preview.trusted is False
    assert stale.preview.desired_states == {}


def test_coverage_boundary_is_trusted_but_one_minute_later_is_untrusted():
    clock = FakeClock()
    provider = FakeProvider([])
    policy = SchedulePolicy(
        refresh_hours=4,
        max_snapshot_age=timedelta(hours=8),
        coverage_horizon=timedelta(hours=4),
    )
    schedule = engine(clock, provider, policy=policy)
    schedule.tick()
    provider.failure = "offline"

    clock.advance(hours=4)
    boundary = schedule.tick()
    assert boundary.preview.trusted is True
    assert boundary.preview.desired_states == {101: 0}

    clock.advance(minutes=1)
    beyond = schedule.tick()
    assert beyond.preview.snapshot_status is SnapshotStatus.OUT_OF_COVERAGE
    assert beyond.preview.trusted is False
    assert beyond.preview.desired_states == {}


@pytest.mark.parametrize(
    "evaluation_time,start,end",
    [
        (
            datetime(2025, 3, 9, 7, 15, tzinfo=UTC),
            "2025-03-09T01:30:00-05:00",
            "2025-03-09T04:00:00-04:00",
        ),
        (
            datetime(2025, 11, 2, 5, 45, tzinfo=UTC),
            "2025-11-02T01:30:00-04:00",
            "2025-11-02T01:30:00-05:00",
        ),
    ],
)
def test_cached_evaluation_uses_absolute_time_across_dst_changes(
    evaluation_time, start, end
):
    clock = FakeClock(evaluation_time)
    provider = FakeProvider([raw_event(start=start, end=end)])
    result = engine(clock, provider).tick().preview
    assert result.trusted is True
    assert result.desired_states == {101: 1}


def test_clock_correction_rebases_once_and_repeated_tick_does_no_duplicate_work():
    clock = FakeClock()
    provider = FakeProvider([])
    schedule = engine(clock, provider)
    schedule.tick()
    repeated = schedule.tick()
    assert repeated.refresh_attempted is False
    assert repeated.preview is None

    clock.advance(minutes=10)
    schedule.tick()
    clock.advance(minutes=-20)
    corrected = schedule.tick()
    assert corrected.refresh_attempted is True
    assert corrected.preview is not None
    assert "clock moved backwards" in corrected.warning
    assert len(provider.calls) == 2
