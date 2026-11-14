from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from eisy_google_calendar.evaluator import evaluate_snapshot
from eisy_google_calendar.models import CalendarEvent, CalendarSnapshot, Rule, SnapshotStatus
from eisy_google_calendar.validation import ConfigurationError, validate_event

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


def make_event(event_id, title, start, end, **kwargs):
    return CalendarEvent(event_id, title, start, end, **kwargs)


def valid_snapshot(*events):
    return CalendarSnapshot(SnapshotStatus.VALID, tuple(events))


def make_rule(title="Workshop", target=10, **kwargs):
    return Rule("test", title, target, **kwargs)


def test_case_insensitive_substring_matching():
    result = evaluate_snapshot(valid_snapshot(make_event("e1", "Saturday WORKSHOP", datetime(2025, 1, 1, 10, tzinfo=UTC), datetime(2025, 1, 1, 11, tzinfo=UTC))), [make_rule()], datetime(2025, 1, 1, 10, 30, tzinfo=UTC))
    assert result.desired_states == {10: 1}


def test_case_sensitive_matching_can_be_requested():
    result = evaluate_snapshot(valid_snapshot(make_event("e1", "workshop", datetime(2025, 1, 1, 10, tzinfo=UTC), datetime(2025, 1, 1, 11, tzinfo=UTC))), [make_rule(case_sensitive=True)], datetime(2025, 1, 1, 10, 30, tzinfo=UTC))
    assert result.desired_states == {10: 0}


def test_start_inclusive_end_exclusive():
    start = datetime(2025, 1, 1, 10, tzinfo=UTC)
    end = datetime(2025, 1, 1, 12, tzinfo=UTC)
    snap = valid_snapshot(make_event("e1", "Workshop", start, end))
    assert evaluate_snapshot(snap, [make_rule()], start).desired_states[10] == 1
    assert evaluate_snapshot(snap, [make_rule()], end - timedelta(microseconds=1)).desired_states[10] == 1
    assert evaluate_snapshot(snap, [make_rule()], end).desired_states[10] == 0


def test_signed_offsets_are_elapsed_minutes():
    start = datetime(2025, 1, 1, 10, tzinfo=UTC)
    end = datetime(2025, 1, 1, 12, tzinfo=UTC)
    snap = valid_snapshot(make_event("e1", "Workshop", start, end))
    adjusted = make_rule(start_offset_minutes=-30, end_offset_minutes=15)
    assert evaluate_snapshot(snap, [adjusted], datetime(2025, 1, 1, 9, 30, tzinfo=UTC)).desired_states[10] == 1
    assert evaluate_snapshot(snap, [adjusted], datetime(2025, 1, 1, 12, 15, tzinfo=UTC)).desired_states[10] == 0


def test_overlapping_events_use_or_until_last_event_ends():
    snap = valid_snapshot(make_event("a", "Workshop", datetime(2025, 1, 1, 10, tzinfo=UTC), datetime(2025, 1, 1, 12, tzinfo=UTC)), make_event("b", "Workshop", datetime(2025, 1, 1, 11, 30, tzinfo=UTC), datetime(2025, 1, 1, 13, tzinfo=UTC)))
    assert evaluate_snapshot(snap, [make_rule()], datetime(2025, 1, 1, 12, 30, tzinfo=UTC)).desired_states[10] == 1
    assert evaluate_snapshot(snap, [make_rule()], datetime(2025, 1, 1, 13, tzinfo=UTC)).desired_states[10] == 0


def test_multiple_rules_targeting_one_variable_use_or():
    snap = valid_snapshot(make_event("e", "Pottery", datetime(2025, 1, 1, 10, tzinfo=UTC), datetime(2025, 1, 1, 11, tzinfo=UTC)))
    assert evaluate_snapshot(snap, [make_rule("Workshop"), make_rule("Pottery")], datetime(2025, 1, 1, 10, 30, tzinfo=UTC)).desired_states[10] == 1


def test_midnight_crossing_event_and_offset():
    snap = valid_snapshot(make_event("e", "Night", datetime(2025, 1, 1, 23, 30, tzinfo=UTC), datetime(2025, 1, 2, 0, 30, tzinfo=UTC)))
    adjusted = make_rule("Night", start_offset_minutes=-30)
    assert evaluate_snapshot(snap, [adjusted], datetime(2025, 1, 1, 23, tzinfo=UTC)).desired_states[10] == 1
    assert evaluate_snapshot(snap, [adjusted], datetime(2025, 1, 2, 0, 30, tzinfo=UTC)).desired_states[10] == 0


def test_spring_forward_uses_actual_instants_and_elapsed_offsets():
    start = datetime(2025, 3, 9, 1, 30, tzinfo=NY)
    end = datetime(2025, 3, 9, 3, 30, tzinfo=NY)
    snap = valid_snapshot(make_event("spring", "DST", start, end))
    adjusted = make_rule("DST", start_offset_minutes=-30, end_offset_minutes=30)
    assert evaluate_snapshot(snap, [adjusted], datetime(2025, 3, 9, 7, tzinfo=UTC)).desired_states[10] == 1
    assert evaluate_snapshot(snap, [adjusted], datetime(2025, 3, 9, 8, tzinfo=UTC)).desired_states[10] == 0


def test_fall_back_valid_event_spanning_repeated_hour():
    start = datetime(2025, 11, 2, 0, 30, tzinfo=NY)
    end = datetime(2025, 11, 2, 2, 30, tzinfo=NY)
    normalized = validate_event(make_event("fall", "DST", start, end))
    assert normalized.start == datetime(2025, 11, 2, 4, 30, tzinfo=UTC)
    assert normalized.end == datetime(2025, 11, 2, 7, 30, tzinfo=UTC)


def test_fall_back_reversed_actual_instants_are_rejected():
    # The local labels look ordered, but fold=1 is 06:30 UTC and fold=0 is 05:30 UTC.
    start = datetime(2025, 11, 2, 1, 30, fold=1, tzinfo=NY)
    end = datetime(2025, 11, 2, 1, 30, fold=0, tzinfo=NY)
    with pytest.raises(ConfigurationError, match="event end must be after event start"):
        validate_event(make_event("reversed-fall", "DST", start, end))


def test_cancelled_and_all_day_events_are_ignored_by_default():
    start = datetime(2025, 1, 1, 10, tzinfo=UTC)
    end = datetime(2025, 1, 1, 11, tzinfo=UTC)
    snap = valid_snapshot(make_event("c", "Workshop", start, end, cancelled=True), make_event("a", "Workshop", start, end, all_day=True))
    result = evaluate_snapshot(snap, [make_rule()], datetime(2025, 1, 1, 10, 30, tzinfo=UTC))
    assert result.desired_states == {10: 0}
    assert result.trusted is True
    assert result.ignored_event_count == 2


def test_ignored_event_count_is_unique_across_rules_and_settings():
    start = datetime(2025, 1, 1, 10, tzinfo=UTC)
    end = datetime(2025, 1, 1, 11, tzinfo=UTC)
    cancelled = make_event("cancelled", "Workshop", start, end, cancelled=True)
    all_day = make_event("all-day", "Workshop", start, end, all_day=True)
    rules = [make_rule("Workshop"), make_rule("Workshop", ignore_cancelled=False), make_rule("Workshop", ignore_all_day=False)]
    result = evaluate_snapshot(valid_snapshot(cancelled, all_day), rules, datetime(2025, 1, 1, 10, 30, tzinfo=UTC))
    assert result.ignored_event_count == 2
    assert result.desired_states == {10: 1}


def test_valid_empty_snapshot_is_trusted_and_returns_zero():
    result = evaluate_snapshot(CalendarSnapshot(SnapshotStatus.VALID), [make_rule()], datetime(2025, 1, 1, tzinfo=UTC))
    assert result.desired_states == {10: 0}
    assert result.trusted is True


@pytest.mark.parametrize("status", [SnapshotStatus.FAILED, SnapshotStatus.UNAVAILABLE])
def test_failed_or_unavailable_snapshots_are_not_trusted(status):
    result = evaluate_snapshot(CalendarSnapshot(status, error="not available"), [make_rule()], datetime(2025, 1, 1, tzinfo=UTC))
    assert result.desired_states == {}
    assert result.status is status
    assert result.trusted is False


def test_invalid_adjusted_interval_is_untrusted_and_has_no_desired_states():
    start = datetime(2025, 1, 1, 10, tzinfo=UTC)
    end = datetime(2025, 1, 1, 11, tzinfo=UTC)
    adjusted = make_rule(start_offset_minutes=120, end_offset_minutes=-120)
    result = evaluate_snapshot(valid_snapshot(make_event("e", "Workshop", start, end)), [adjusted], datetime(2025, 1, 1, 12, tzinfo=UTC))
    assert result.status is SnapshotStatus.VALID
    assert result.trusted is False
    assert result.desired_states == {}
    assert result.invalid_interval_count == 1
    assert "test" in result.diagnostics[0]
    assert "e" in result.diagnostics[0]


def test_timezone_aware_now_is_required():
    with pytest.raises(ConfigurationError):
        evaluate_snapshot(valid_snapshot(), [make_rule()], datetime(2025, 1, 1))


def test_target_ids_must_be_positive_integers_and_not_booleans():
    start = datetime(2025, 1, 1, 10, tzinfo=UTC)
    end = datetime(2025, 1, 1, 11, tzinfo=UTC)
    for bad_id in (True, False, 0, -1, "10"):
        with pytest.raises(ConfigurationError):
            evaluate_snapshot(valid_snapshot(make_event("e", "Workshop", start, end)), [make_rule(target=bad_id)], start)


def test_event_timestamps_must_be_aware_and_ordered():
    start = datetime(2025, 1, 1, 10, tzinfo=UTC)
    with pytest.raises(ConfigurationError):
        evaluate_snapshot(valid_snapshot(make_event("naive", "Workshop", datetime(2025, 1, 1, 10), start)), [make_rule()], start)
    with pytest.raises(ConfigurationError):
        evaluate_snapshot(valid_snapshot(make_event("backwards", "Workshop", start, start)), [make_rule()], start)
