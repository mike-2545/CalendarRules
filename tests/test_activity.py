"""Clock-controlled active/upcoming activity tests; no network or IoX access."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from eisy_google_calendar.activity import (
    MAX_ACTIVITY_NOTICE_CHARS,
    MAX_ACTIVE_ITEMS,
    MAX_COMPACT_NOTICE_CHARS,
    MAX_TITLE_CHARS,
    MAX_UPCOMING_ITEMS,
    calculate_activity,
    format_activity_notice,
    format_activity_notices,
)
from eisy_google_calendar.config_identity import configuration_fingerprint
from eisy_google_calendar.models import (
    CalendarConfig,
    CalendarEvent,
    CalendarSnapshot,
    Rule,
    SnapshotStatus,
)


UTC = timezone.utc
NOW = datetime(2026, 11, 12, 16, 30, tzinfo=UTC)
MAX_AGE = timedelta(days=30)


def config(*rules: Rule, timezone_name: str = "America/New_York") -> CalendarConfig:
    return CalendarConfig(("private-calendar",), timezone_name, 60, tuple(rules))


def rule(
    name: str = "Workshop rule",
    pattern: str = "Workshop",
    target: int = 38,
    *,
    match_type: str = "contains",
    case_sensitive: bool = False,
    start_offset: int = 0,
    end_offset: int = 0,
) -> Rule:
    return Rule(
        name,
        pattern,
        target,
        start_offset,
        end_offset,
        case_sensitive,
        title_match_type=match_type,
    )


def event(
    identifier: str,
    title: str,
    start: datetime,
    end: datetime,
    *,
    cancelled: bool = False,
    all_day: bool = False,
) -> CalendarEvent:
    return CalendarEvent(identifier, title, start, end, all_day, cancelled)


def snapshot(configuration: CalendarConfig, *events: CalendarEvent, fetched=NOW):
    return CalendarSnapshot(
        SnapshotStatus.VALID,
        tuple(events),
        fetched_at=fetched,
        coverage_start=NOW - timedelta(days=1),
        coverage_end=NOW + timedelta(days=32),
        configuration_fingerprint=configuration_fingerprint(configuration),
    )


def activity(configuration: CalendarConfig, snap: CalendarSnapshot, now=NOW, **kwargs):
    return calculate_activity(
        configuration,
        snap,
        now=now,
        max_snapshot_age=MAX_AGE,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("match_type", "pattern", "title", "case_sensitive", "matched"),
    [
        ("contains", "shop", "Workshop test3", True, True),
        ("contains", "SHOP", "Workshop test3", True, False),
        ("contains", "SHOP", "Workshop test3", False, True),
        ("exact", "test3", "test3", True, True),
        ("exact", "TEST3", "test3", True, False),
        ("exact", "TEST3", "test3", False, True),
        ("starts_with", "Workshop", "Workshop test3", True, True),
        ("starts_with", "WORKSHOP", "Workshop test3", True, False),
        ("starts_with", "WORKSHOP", "Workshop test3", False, True),
        ("ends_with", "test3", "Workshop test3", True, True),
        ("ends_with", "TEST3", "Workshop test3", True, False),
        ("ends_with", "TEST3", "Workshop test3", False, True),
    ],
)
def test_activity_uses_all_title_operators_and_case_modes(
    match_type, pattern, title, case_sensitive, matched
):
    configuration = config(
        rule(pattern=pattern, match_type=match_type, case_sensitive=case_sensitive)
    )
    result = activity(
        configuration,
        snapshot(
            configuration,
            event("private-id", title, NOW - timedelta(minutes=10), NOW + timedelta(minutes=10)),
        ),
    )
    assert bool(result.active) is matched
    assert result.variables[0].desired_state == int(matched)


def test_offsets_preserve_original_times_and_change_automation_window():
    configuration = config(rule(start_offset=-15, end_offset=30))
    original_start = NOW + timedelta(minutes=10)
    original_end = NOW + timedelta(hours=1)
    result = activity(
        configuration,
        snapshot(configuration, event("id", "Workshop", original_start, original_end)),
    )
    item = result.active[0]
    assert (item.event_start, item.event_end) == (original_start, original_end)
    assert item.automation_start == original_start - timedelta(minutes=15)
    assert item.automation_end == original_end + timedelta(minutes=30)


def test_overlapping_rules_union_target_window_without_false_off_transition():
    configuration = config(rule("A"), rule("B", pattern="Session"))
    result = activity(
        configuration,
        snapshot(
            configuration,
            event("a", "Workshop", NOW - timedelta(minutes=30), NOW + timedelta(minutes=30)),
            event("b", "Session", NOW, NOW + timedelta(hours=2)),
        ),
    )
    assert [item.rule_name for item in result.active] == ["A", "B"]
    variable = result.variables[0]
    assert variable.desired_state == 1
    assert variable.next_transition == NOW + timedelta(hours=2)
    assert variable.next_value == 0
    assert {item.next_variable_transition for item in result.active} == {
        NOW + timedelta(hours=2)
    }


def test_adjacent_windows_are_one_continuous_on_interval():
    configuration = config(rule())
    result = activity(
        configuration,
        snapshot(
            configuration,
            event("a", "Workshop A", NOW - timedelta(hours=1), NOW + timedelta(minutes=30)),
            event("b", "Workshop B", NOW + timedelta(minutes=30), NOW + timedelta(hours=2)),
        ),
    )
    assert result.variables[0].next_transition == NOW + timedelta(hours=2)


def test_two_rules_same_variable_are_deterministic_at_identical_timestamps():
    configuration = config(rule("Zulu", "Workshop"), rule("Alpha", "Workshop"))
    snap = snapshot(
        configuration,
        event("private", "Workshop", NOW, NOW + timedelta(hours=1)),
    )
    first = activity(configuration, snap)
    second = activity(configuration, snap)
    assert first == second
    assert [item.rule_name for item in first.active] == ["Alpha", "Zulu"]


def test_upcoming_event_reports_next_on_transition_and_desired_off():
    configuration = config(rule())
    start = NOW + timedelta(hours=1)
    result = activity(
        configuration,
        snapshot(configuration, event("id", "Workshop", start, start + timedelta(hours=1))),
    )
    assert not result.active
    assert result.upcoming[0].desired_state == 0
    assert result.variables[0].next_transition == start
    assert result.variables[0].next_value == 1


def test_disjoint_future_event_uses_its_own_interval_while_target_is_on_now():
    configuration = config(rule())
    future_start = NOW + timedelta(hours=4)
    result = activity(
        configuration,
        snapshot(
            configuration,
            event("active", "Workshop A", NOW - timedelta(hours=1), NOW + timedelta(hours=1)),
            event("future", "Workshop B", future_start, future_start + timedelta(hours=1)),
        ),
    )
    future = result.upcoming[0]
    assert result.variables[0].desired_state == 1
    assert result.variables[0].next_transition == NOW + timedelta(hours=1)
    assert future.target_interval_start == future_start
    assert future.target_interval_end == future_start + timedelta(hours=1)
    assert future.target_already_on_at_start is False
    notice = format_activity_notice(result, timezone_name="UTC", show_event_titles=True)
    future_line = next(line for line in notice.splitlines() if "Workshop B" in line)
    assert "expected ON" in future_line
    assert "target already desired ON" not in future_line


def test_overlapping_future_event_does_not_claim_a_second_on_transition():
    configuration = config(rule())
    first_start = NOW + timedelta(hours=1)
    first_end = NOW + timedelta(hours=4)
    result = activity(
        configuration,
        snapshot(
            configuration,
            event("first", "Workshop A", first_start, first_end),
            event(
                "nested",
                "Workshop B",
                NOW + timedelta(hours=2),
                NOW + timedelta(hours=3),
            ),
        ),
    )
    assert result.active == ()
    nested = next(item for item in result.upcoming if item.title == "Workshop B")
    assert nested.target_interval_start == first_start
    assert nested.target_interval_end == first_end
    assert nested.target_already_on_at_start is True
    notice = format_activity_notice(result, timezone_name="UTC", show_event_titles=True)
    nested_line = next(line for line in notice.splitlines() if "Workshop B" in line)
    assert "target already desired ON at event start" in nested_line
    assert "expected ON" not in nested_line


def test_adjacent_future_events_share_one_on_interval():
    configuration = config(rule())
    interval_start = NOW + timedelta(hours=1)
    boundary = NOW + timedelta(hours=2)
    interval_end = NOW + timedelta(hours=3)
    result = activity(
        configuration,
        snapshot(
            configuration,
            event("first", "Workshop A", interval_start, boundary),
            event("adjacent", "Workshop B", boundary, interval_end),
        ),
    )
    second = next(item for item in result.upcoming if item.title == "Workshop B")
    assert second.target_interval_start == interval_start
    assert second.target_interval_end == interval_end
    assert second.target_already_on_at_start is True


def test_offsets_and_multiple_future_intervals_are_assigned_independently():
    configuration = config(rule(start_offset=-30, end_offset=15))
    result = activity(
        configuration,
        snapshot(
            configuration,
            event(
                "first",
                "Workshop A",
                NOW + timedelta(hours=2),
                NOW + timedelta(hours=3),
            ),
            event(
                "overlap",
                "Workshop B",
                NOW + timedelta(hours=2, minutes=45),
                NOW + timedelta(hours=4),
            ),
            event(
                "separate",
                "Workshop C",
                NOW + timedelta(hours=6),
                NOW + timedelta(hours=7),
            ),
        ),
    )
    assert result.active == ()
    first, overlap, separate = result.upcoming
    assert first.target_interval_start == NOW + timedelta(hours=1, minutes=30)
    assert first.target_interval_end == NOW + timedelta(hours=4, minutes=15)
    assert first.target_already_on_at_start is False
    assert overlap.target_interval_start == first.target_interval_start
    assert overlap.target_interval_end == first.target_interval_end
    assert overlap.target_already_on_at_start is True
    assert separate.target_interval_start == NOW + timedelta(hours=5, minutes=30)
    assert separate.target_interval_end == NOW + timedelta(hours=7, minutes=15)
    assert separate.target_already_on_at_start is False


def test_cross_midnight_and_dst_instants_use_utc_for_interval_logic():
    configuration = config(rule(), timezone_name="America/New_York")
    dst_now = datetime(2026, 11, 1, 5, 45, tzinfo=UTC)  # 1:45 EDT
    snap = CalendarSnapshot(
        SnapshotStatus.VALID,
        (
            event(
                "dst",
                "Workshop",
                datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
                datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
            ),
            event(
                "midnight",
                "Workshop",
                datetime(2026, 11, 2, 4, 30, tzinfo=UTC),
                datetime(2026, 11, 2, 6, 30, tzinfo=UTC),
            ),
        ),
        fetched_at=dst_now,
        coverage_start=dst_now - timedelta(days=1),
        coverage_end=dst_now + timedelta(days=32),
        configuration_fingerprint=configuration_fingerprint(configuration),
    )
    result = activity(configuration, snap, now=dst_now)
    notice = format_activity_notice(
        result, timezone_name="America/New_York", show_event_titles=True
    )
    assert result.active[0].title == "Workshop"
    assert "EDT" in notice and "EST" in notice


def test_cancelled_all_day_and_nonmatching_events_are_excluded():
    configuration = config(rule())
    result = activity(
        configuration,
        snapshot(
            configuration,
            event("cancelled", "Workshop", NOW, NOW + timedelta(hours=1), cancelled=True),
            event("all-day", "Workshop", NOW, NOW + timedelta(days=1), all_day=True),
            event("other", "Private", NOW, NOW + timedelta(hours=1)),
        ),
    )
    assert result.trusted
    assert result.active == () and result.upcoming == ()
    assert result.variables[0].desired_state == 0


def test_valid_empty_snapshot_has_useful_empty_activity_state():
    configuration = config(rule())
    result = activity(configuration, snapshot(configuration))
    notice = format_activity_notice(
        result, timezone_name="America/New_York", show_event_titles=False
    )
    assert result.trusted and result.active == () and result.upcoming == ()
    assert "Active:\n- none" in notice
    assert "Upcoming:\n- none" in notice


@pytest.mark.parametrize(
    "mutation",
    [
        lambda cfg, snap: replace(snap, status=SnapshotStatus.FAILED),
        lambda cfg, snap: replace(snap, fetched_at=NOW - timedelta(days=31)),
        lambda cfg, snap: replace(snap, coverage_end=NOW - timedelta(seconds=1)),
        lambda cfg, snap: replace(snap, configuration_fingerprint="0" * 64),
    ],
)
def test_untrusted_expired_out_of_coverage_or_incompatible_snapshots_hide_activity(mutation):
    configuration = config(rule())
    result = activity(configuration, mutation(configuration, snapshot(configuration)))
    assert not result.trusted
    assert result.active == () and result.upcoming == ()
    assert "unavailable" in format_activity_notice(
        result, timezone_name="America/New_York", show_event_titles=True
    ).lower()


def test_invalid_adjusted_interval_fails_closed():
    configuration = config(rule(start_offset=60, end_offset=-60))
    result = activity(
        configuration,
        snapshot(
            configuration,
            event("id", "Workshop", NOW, NOW + timedelta(minutes=30)),
        ),
    )
    assert not result.trusted
    assert not result.variables


def test_trusted_offline_cache_is_labeled_and_remains_authoritative():
    configuration = config(rule())
    result = activity(
        configuration,
        snapshot(
            configuration,
            event("id", "Workshop", NOW - timedelta(minutes=1), NOW + timedelta(minutes=1)),
        ),
        using_cached_data=True,
    )
    notice = format_activity_notice(
        result, timezone_name="America/New_York", show_event_titles=False
    )
    assert result.trusted and result.cached
    assert "trusted cache" in notice
    compact = format_activity_notices(
        result,
        timezone_name="America/New_York",
        show_event_titles=False,
        upcoming_event_limit=5,
    )
    assert "Trusted cache" in compact["calendar_activity"]


def test_title_privacy_is_opt_in_and_provider_identifiers_are_never_rendered():
    configuration = config(rule("Friendly rule"))
    private_title = "Workshop — private medical appointment"
    result = activity(
        configuration,
        snapshot(
            configuration,
            event("PRIVATE_EVENT_ID", private_title, NOW, NOW + timedelta(hours=1)),
        ),
    )
    hidden = format_activity_notice(
        result, timezone_name="America/New_York", show_event_titles=False
    )
    shown = format_activity_notice(
        result, timezone_name="America/New_York", show_event_titles=True
    )
    assert private_title not in hidden and "[title hidden]" in hidden
    assert private_title in shown
    for notice in (hidden, shown):
        assert "PRIVATE_EVENT_ID" not in notice
        assert "private-calendar" not in notice


def test_display_limits_titles_items_and_total_message_length():
    configuration = config(rule())
    events = tuple(
        event(
            f"private-{index}",
            "Workshop " + "X" * (MAX_TITLE_CHARS + 100),
            NOW if index < 8 else NOW + timedelta(hours=index),
            NOW + timedelta(minutes=30) if index < 8 else NOW + timedelta(hours=index + 1),
        )
        for index in range(20)
    )
    result = activity(configuration, snapshot(configuration, *events))
    notice = format_activity_notice(
        result, timezone_name="America/New_York", show_event_titles=True
    )
    assert notice.count("| rule") <= MAX_ACTIVE_ITEMS + MAX_UPCOMING_ITEMS
    assert "omitted" in notice
    assert len(notice) <= MAX_ACTIVITY_NOTICE_CHARS
    assert "X" * (MAX_TITLE_CHARS + 1) not in notice


def test_activity_calculation_has_no_transport_or_write_side_effects():
    configuration = config(rule())
    original = snapshot(
        configuration,
        event("id", "Workshop", NOW, NOW + timedelta(hours=1)),
    )
    before = repr(original)
    result = activity(configuration, original)
    assert result.variables[0].desired_state == 1
    assert repr(original) == before


@pytest.mark.parametrize(("count", "limit"), [(0, 5), (1, 5), (5, 5), (6, 5), (10, 10), (12, 10)])
def test_compact_upcoming_notice_counts_limits_and_stable_order(count, limit):
    configuration = config(rule())
    events = tuple(
        event(
            f"private-{index}",
            f"Workshop {index}",
            NOW + timedelta(hours=index + 1),
            NOW + timedelta(hours=index + 1, minutes=30),
        )
        for index in reversed(range(count))
    )
    result = activity(configuration, snapshot(configuration, *events))
    notices = format_activity_notices(
        result,
        timezone_name="America/New_York",
        show_event_titles=True,
        upcoming_event_limit=limit,
    )

    upcoming = [
        notices[key]
        for key in sorted(notices)
        if key.startswith("calendar_activity_upcoming_")
    ]
    assert len(upcoming) == min(count, limit)
    assert all(len(value) <= MAX_COMPACT_NOTICE_CHARS for value in notices.values())
    if count:
        assert "Workshop 0" in upcoming[0]
        assert "ON" in upcoming[0] and "OFF" in upcoming[0]
    if count > limit:
        assert f"showing {limit}" in notices["calendar_activity"]


def test_compact_notices_use_effective_start_order_and_merged_target_interval():
    configuration = config(rule(start_offset=-30, end_offset=15))
    first_start = NOW + timedelta(hours=2)
    result = activity(
        configuration,
        snapshot(
            configuration,
            event("later-source", "Workshop B", first_start + timedelta(minutes=45), first_start + timedelta(hours=2)),
            event("first-source", "Workshop A", first_start, first_start + timedelta(hours=1)),
            event("disjoint", "Workshop C", first_start + timedelta(hours=5), first_start + timedelta(hours=6)),
        ),
    )
    notices = format_activity_notices(
        result,
        timezone_name="UTC",
        show_event_titles=True,
        upcoming_event_limit=5,
    )

    first = notices["calendar_activity_upcoming_01"]
    overlapping = notices["calendar_activity_upcoming_02"]
    disjoint = notices["calendar_activity_upcoming_03"]
    assert "Workshop A" in first and "ON 6:00 PM UTC" in first
    assert "Workshop B" in overlapping
    assert "Already desired ON since 6:00 PM UTC" in overlapping
    assert "OFF 8:45 PM UTC" in overlapping
    assert "Workshop C" in disjoint and "ON 11:00 PM UTC" in disjoint


def test_compact_active_summary_and_title_privacy_are_bounded():
    configuration = config(rule())
    result = activity(
        configuration,
        snapshot(
            configuration,
            *(
                event(
                    f"private-{index}",
                    "Workshop SECRET " + "X" * 100,
                    NOW - timedelta(minutes=index + 1),
                    NOW + timedelta(hours=index + 1),
                )
                for index in range(6)
            ),
        ),
    )
    hidden = format_activity_notices(
        result,
        timezone_name="UTC",
        show_event_titles=False,
        upcoming_event_limit=5,
    )
    shown = format_activity_notices(
        result,
        timezone_name="UTC",
        show_event_titles=True,
        upcoming_event_limit=5,
    )
    assert "SECRET" not in repr(hidden)
    assert "[title hidden]" in hidden["calendar_activity_active"]
    assert "SECRET" in shown["calendar_activity_active"]
    assert "+5 more" in shown["calendar_activity_active"]
    assert all(len(value) <= MAX_COMPACT_NOTICE_CHARS for value in shown.values())


def test_compact_entries_preserve_event_rule_targets_and_report_omissions():
    rules = tuple(rule(f"Rule {index}", target=38 + index) for index in range(6))
    configuration = config(*rules)
    start = NOW + timedelta(hours=1)
    result = activity(
        configuration,
        snapshot(configuration, event("private", "Workshop", start, start + timedelta(hours=1))),
    )
    notices = format_activity_notices(
        result,
        timezone_name="UTC",
        show_event_titles=False,
        upcoming_event_limit=5,
    )
    rendered = " ".join(notices.values())
    for target in range(38, 43):
        assert f"Var {target}" in rendered
    assert "Upcoming 6 (showing 5)" in notices["calendar_activity"]
    assert "Var 43" not in rendered


def test_compact_untrusted_activity_contains_no_stale_details():
    configuration = config(rule())
    result = activity(
        configuration,
        replace(snapshot(configuration), status=SnapshotStatus.FAILED),
    )
    notices = format_activity_notices(
        result,
        timezone_name="UTC",
        show_event_titles=True,
        upcoming_event_limit=10,
    )
    assert notices == {
        "calendar_activity": "Calendar activity unavailable: no trusted, compatible schedule."
    }
