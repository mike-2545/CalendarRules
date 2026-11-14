from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from eisy_google_calendar.activity import calculate_activity, format_activity_notices
from eisy_google_calendar.calendar_api import event_from_api, fetch_snapshot
from eisy_google_calendar.cached_schedule import CachedScheduleEngine, SchedulePolicy
from eisy_google_calendar.config_identity import configuration_fingerprint
from eisy_google_calendar.evaluator import evaluate_snapshot
from eisy_google_calendar.models import (
    CalendarConfig,
    CalendarEvent,
    CalendarSnapshot,
    Rule,
    SnapshotStatus,
)
from eisy_google_calendar.reconciler import ReconciliationStatus, reconcile_snapshot
from eisy_google_calendar.ui_configuration import load_typed_configuration
from eisy_google_calendar.validation import ConfigurationError, load_config_data


UTC = timezone.utc
NOW = datetime(2026, 11, 13, 18, tzinfo=UTC)
LEGACY_FINGERPRINT = "7856281dbe10aea720441d174760ba86927a03515891dbfdb533112d5a7108b2"


def legacy_json() -> dict:
    return {
        "calendar_ids": ["primary"],
        "timezone": "America/New_York",
        "poll_seconds": 60,
        "rules": [
            {
                "name": "Workshop",
                "match": {
                    "title_contains": "Workshop",
                    "case_sensitive": False,
                },
                "isy_state_variable_id": 10,
                "start_offset_minutes": -30,
                "end_offset_minutes": 0,
            }
        ],
    }


def typed_rule(**changes) -> dict:
    row = {
        "name": "room",
        "title_contains": "Workshop",
        "title_match_type": "contains",
        "case_sensitive": "false",
        "state_variable_id": "38",
        "start_offset_minutes": "0",
        "end_offset_minutes": "0",
        "enabled": "true",
    }
    row.update(changes)
    return row


def typed_payload(*rules: dict) -> dict:
    return {
        "timezone": "America/New_York",
        "refresh_hours": "2",
        "calendars": [{"calendar_id": "primary"}],
        "rules": list(rules),
    }


def event(*, title="Staff schedule", location="Workshop East", **changes):
    values = {
        "event_id": "event",
        "title": title,
        "start": NOW - timedelta(minutes=10),
        "end": NOW + timedelta(minutes=10),
        "location": location,
    }
    values.update(changes)
    return CalendarEvent(**values)


def trusted(config: CalendarConfig, *events: CalendarEvent, complete=True):
    return CalendarSnapshot(
        SnapshotStatus.VALID,
        tuple(events),
        fetched_at=NOW - timedelta(minutes=1),
        coverage_start=NOW - timedelta(hours=1),
        coverage_end=NOW + timedelta(days=1),
        configuration_fingerprint=configuration_fingerprint(config),
        location_data_complete=complete,
    )


@pytest.mark.parametrize(
    "operator,pattern,matching,nonmatching",
    [
        ("contains", "shop", "Workshop East", "Meeting Room"),
        ("exact", "Workshop East", "Workshop East", "Workshop East Wing"),
        ("starts_with", "Workshop", "Workshop East", "East Workshop"),
        ("ends_with", "East", "Workshop East", "East Workshop"),
    ],
)
@pytest.mark.parametrize("field", ("title", "location"))
def test_each_operator_uses_selected_field_case_sensitively_and_insensitively(
    operator, pattern, matching, nonmatching, field
):
    matching_event = event(
        title=matching if field == "title" else "unrelated title",
        location=matching if field == "location" else "unrelated location",
    )
    nonmatching_event = event(
        title=nonmatching if field == "title" else matching,
        location=nonmatching if field == "location" else matching,
    )
    rule = Rule(
        "rule",
        pattern,
        38,
        title_match_type=operator,
        match_field=field,
        case_sensitive=True,
    )
    assert evaluate_snapshot(
        CalendarSnapshot(SnapshotStatus.VALID, (matching_event,)), (rule,), NOW
    ).desired_states == {38: 1}
    assert evaluate_snapshot(
        CalendarSnapshot(SnapshotStatus.VALID, (nonmatching_event,)), (rule,), NOW
    ).desired_states == {38: 0}

    folded = event(
        title=matching.swapcase() if field == "title" else "unrelated title",
        location=matching.swapcase() if field == "location" else "unrelated location",
    )
    insensitive = Rule(
        "rule",
        pattern,
        38,
        title_match_type=operator,
        match_field=field,
        case_sensitive=False,
    )
    assert evaluate_snapshot(
        CalendarSnapshot(SnapshotStatus.VALID, (folded,)), (insensitive,), NOW
    ).desired_states == {38: 1}


def test_location_matching_uses_unicode_casefolding():
    rule = Rule("room", "STRASSE", 38, match_field="location")
    result = evaluate_snapshot(
        CalendarSnapshot(SnapshotStatus.VALID, (event(location="Große Straße"),)),
        (rule,),
        NOW,
    )
    assert result.desired_states == {38: 1}


@pytest.mark.parametrize("location", (None, 7, [], {}))
def test_provider_rejects_non_string_location_without_exposing_value(location):
    raw = {
        "id": "private-id",
        "summary": "Staff",
        "location": location,
        "start": {"dateTime": "2026-11-13T17:00:00Z"},
        "end": {"dateTime": "2026-11-13T19:00:00Z"},
    }
    with pytest.raises(ConfigurationError, match="location must be a string") as exc:
        event_from_api("private-calendar", raw, timezone_name="UTC")
    assert repr(location) not in str(exc.value)


@pytest.mark.parametrize("raw_location", (pytest.param("missing", id="missing"), ""))
def test_missing_or_empty_location_is_valid_but_never_matches(raw_location):
    raw = {
        "id": "event",
        "summary": "Workshop East",
        "start": {"dateTime": "2026-11-13T17:00:00Z"},
        "end": {"dateTime": "2026-11-13T19:00:00Z"},
    }
    if raw_location != "missing":
        raw["location"] = raw_location
    converted = event_from_api("primary", raw, timezone_name="UTC")
    assert converted is not None and converted.location == ""
    rule = Rule("room", "Workshop", 38, match_field="location")
    assert evaluate_snapshot(
        CalendarSnapshot(SnapshotStatus.VALID, (converted,)), (rule,), NOW
    ).desired_states == {38: 0}


def test_location_change_is_part_of_provider_dedup_identity():
    pages = [
        {
            "items": [
                {
                    "id": "same",
                    "summary": "Staff",
                    "location": "Room A",
                    "start": {"dateTime": "2026-11-13T17:00:00Z"},
                    "end": {"dateTime": "2026-11-13T19:00:00Z"},
                }
            ],
            "nextPageToken": "next",
        },
        {
            "items": [
                {
                    "id": "same",
                    "summary": "Staff",
                    "location": "Room B",
                    "start": {"dateTime": "2026-11-13T17:00:00Z"},
                    "end": {"dateTime": "2026-11-13T19:00:00Z"},
                }
            ]
        },
    ]
    config = CalendarConfig(
        ("primary",), "UTC", 60, (Rule("room", "Room", 38, match_field="location"),)
    )
    result = fetch_snapshot(
        config,
        fetch_page=lambda _calendar, _params: pages.pop(0),
        now=NOW,
    )
    assert result.status is SnapshotStatus.FAILED
    assert result.error == "calendar snapshot could not be completed"


def test_mixed_title_and_location_rules_share_or_reconciliation_and_offsets():
    snapshot = CalendarSnapshot(
        SnapshotStatus.VALID,
        (
            event(title="Staff Workshop", location="Elsewhere"),
            event(
                event_id="room",
                title="Staff",
                location="Workshop East",
                start=NOW + timedelta(minutes=5),
                end=NOW + timedelta(minutes=30),
            ),
        ),
    )
    rules = (
        Rule("title", "Workshop", 38),
        Rule("location", "Workshop", 38, start_offset_minutes=-10, match_field="location"),
        Rule("other-target", "East", 104, match_field="location"),
    )
    result = evaluate_snapshot(snapshot, rules, NOW)
    assert result.trusted is True
    assert result.desired_states == {38: 1, 104: 0}
    assert result.matched_rules == {38: ("title", "location")}


def test_legacy_and_explicit_title_keep_exact_fingerprint_but_location_changes_it():
    legacy = load_config_data(legacy_json())
    explicit_data = deepcopy(legacy_json())
    explicit_data["rules"][0]["match"]["match_field"] = "title"
    explicit = load_config_data(explicit_data)
    location_data = deepcopy(explicit_data)
    location_data["rules"][0]["match"]["match_field"] = "location"
    location = load_config_data(location_data)

    assert legacy.rules[0].match_field == "title"
    assert configuration_fingerprint(legacy) == LEGACY_FINGERPRINT
    assert configuration_fingerprint(explicit) == LEGACY_FINGERPRINT
    assert configuration_fingerprint(location) != LEGACY_FINGERPRINT


def test_typed_rows_default_title_and_reject_unknown_field_even_when_disabled():
    legacy = load_typed_configuration(typed_payload(typed_rule()))
    location = load_typed_configuration(
        typed_payload(typed_rule(match_field="location"))
    )
    assert legacy.calendar.rules[0].match_field == "title"
    assert location.calendar.rules[0].match_field == "location"

    for enabled in ("true", "false"):
        with pytest.raises(ConfigurationError, match="match_field"):
            load_typed_configuration(
                typed_payload(typed_rule(match_field="description", enabled=enabled))
            )


def test_json_rejects_unknown_match_field_and_empty_pattern():
    invalid_field = legacy_json()
    invalid_field["rules"][0]["match"]["match_field"] = "description"
    with pytest.raises(ConfigurationError, match="match_field"):
        load_config_data(invalid_field)

    empty_pattern = legacy_json()
    empty_pattern["rules"][0]["match"]["title_contains"] = ""
    with pytest.raises(ConfigurationError, match="title_contains"):
        load_config_data(empty_pattern)


def test_activity_and_evaluator_share_location_decision_without_disclosing_location():
    config = CalendarConfig(
        ("primary",),
        "America/New_York",
        60,
        (Rule("facility room", "Secret Room", 38, match_field="location"),),
    )
    snapshot = trusted(
        config,
        event(title="Staff meeting", location="Secret Room"),
    )
    evaluation = evaluate_snapshot(snapshot, config.rules, NOW)
    activity = calculate_activity(
        config,
        snapshot,
        now=NOW,
        max_snapshot_age=timedelta(days=30),
    )
    notices = format_activity_notices(
        activity,
        timezone_name=config.timezone,
        show_event_titles=True,
        upcoming_event_limit=5,
    )
    rendered = " ".join(notices.values())

    assert evaluation.desired_states == {38: 1}
    assert activity.trusted is True and len(activity.active) == 1
    assert "Staff meeting" in rendered
    assert "Secret Room" not in rendered


def test_upcoming_location_match_never_discloses_location_even_with_titles_enabled():
    config = CalendarConfig(
        ("primary",),
        "America/New_York",
        60,
        (Rule("facility room", "Secret Room", 38, match_field="location"),),
    )
    snapshot = trusted(
        config,
        event(
            title="Visible staff meeting",
            location="Secret Room",
            start=NOW + timedelta(hours=1),
            end=NOW + timedelta(hours=2),
        ),
    )
    activity = calculate_activity(
        config, snapshot, now=NOW, max_snapshot_age=timedelta(days=30)
    )
    rendered = " ".join(
        format_activity_notices(
            activity,
            timezone_name=config.timezone,
            show_event_titles=True,
            upcoming_event_limit=5,
        ).values()
    )
    assert len(activity.upcoming) == 1
    assert "Visible staff meeting" in rendered
    assert "Secret Room" not in rendered


def test_location_matching_preserves_cancelled_and_all_day_exclusions():
    rules = (Rule("room", "Workshop", 38, match_field="location"),)
    snapshot = CalendarSnapshot(
        SnapshotStatus.VALID,
        (
            event(event_id="cancelled", location="Workshop", cancelled=True),
            event(event_id="all-day", location="Workshop", all_day=True),
        ),
    )
    result = evaluate_snapshot(snapshot, rules, NOW)
    assert result.trusted is True
    assert result.desired_states == {38: 0}
    assert result.ignored_reasons == {"cancelled": 1, "all_day": 1}


def test_legacy_snapshot_cannot_authorize_location_off_or_reconciliation_io():
    rule = Rule("room", "Workshop", 38, match_field="location")
    snapshot = CalendarSnapshot(
        SnapshotStatus.VALID,
        (),
        location_data_complete=False,
    )

    class NoIo:
        calls = []

        def get(self, path):
            self.calls.append(("get", path))
            raise AssertionError("legacy location data must not reach IoX")

        def set(self, path):
            self.calls.append(("set", path))
            raise AssertionError("legacy location data must not reach IoX")

    transport = NoIo()
    evaluated = evaluate_snapshot(snapshot, (rule,), NOW)
    reconciled = reconcile_snapshot(snapshot, (rule,), NOW, transport)
    assert evaluated.trusted is False and evaluated.desired_states == {}
    assert reconciled.status is ReconciliationStatus.UNTRUSTED
    assert transport.calls == []


def test_location_rules_do_not_add_google_requests():
    calls = []

    def page(calendar, params):
        calls.append((calendar, params))
        return {"items": []}

    config = CalendarConfig(
        ("primary",),
        "UTC",
        60,
        (
            Rule("title", "Staff", 38),
            Rule("room", "Workshop", 104, match_field="location"),
        ),
    )
    snapshot = fetch_snapshot(config, fetch_page=page, now=NOW)
    assert snapshot.status is SnapshotStatus.VALID
    assert snapshot.location_data_complete is True
    assert len(calls) == 1


class _LegacyStorage:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.saves = 0

    def load(self):
        return self.snapshot

    def save(self, snapshot):
        self.snapshot = snapshot
        self.saves += 1


def _offline_engine(config: CalendarConfig, snapshot: CalendarSnapshot):
    def unavailable(_calendar, _params):
        raise RuntimeError("private Google outage")

    return CachedScheduleEngine(
        config,
        policy=SchedulePolicy(),
        fetch_page=unavailable,
        clock=lambda: NOW,
        storage=_LegacyStorage(snapshot),
    )


def test_outage_restores_legacy_v2_equivalent_for_title_rules_only():
    title_config = load_config_data(legacy_json())
    legacy_snapshot = CalendarSnapshot(
        SnapshotStatus.VALID,
        (event(title="Workshop setup", location=""),),
        fetched_at=NOW - timedelta(hours=1),
        coverage_start=NOW - timedelta(hours=2),
        coverage_end=NOW + timedelta(days=1),
        configuration_fingerprint=configuration_fingerprint(title_config),
        location_data_complete=False,
    )
    result = _offline_engine(title_config, legacy_snapshot).tick()
    assert result.refresh_succeeded is False
    assert result.preview.trusted is True
    assert result.preview.desired_states == {10: 1}


def test_outage_rejects_legacy_v2_equivalent_for_location_rules_without_false_off():
    location_data = legacy_json()
    location_data["rules"][0]["match"]["match_field"] = "location"
    location_config = load_config_data(location_data)
    legacy_snapshot = CalendarSnapshot(
        SnapshotStatus.VALID,
        (),
        fetched_at=NOW - timedelta(hours=1),
        coverage_start=NOW - timedelta(hours=2),
        coverage_end=NOW + timedelta(days=1),
        configuration_fingerprint=configuration_fingerprint(location_config),
        location_data_complete=False,
    )
    engine = _offline_engine(location_config, legacy_snapshot)
    result = engine.tick()
    assert result.refresh_succeeded is False
    assert result.preview.trusted is False
    assert result.preview.desired_states == {}
    assert engine.snapshot is None
    assert "no compatible trusted snapshot" in (result.warning or "")


def test_switching_from_title_to_location_invalidates_in_memory_snapshot():
    title_config = load_config_data(legacy_json())
    location_data = deepcopy(legacy_json())
    location_data["rules"][0]["match"]["match_field"] = "location"
    location_config = load_config_data(location_data)
    snapshot = trusted(title_config, event(title="Workshop", location="Workshop"))
    engine = _offline_engine(title_config, snapshot)

    assert engine.restore() is True
    assert engine.snapshot is snapshot
    assert engine.reconfigure(location_config, engine.policy) is True
    assert engine.snapshot is None
    assert "complete snapshot is required" in (engine.last_warning or "")


def test_trusted_v3_location_snapshot_continues_on_off_evaluation_during_outage():
    config = CalendarConfig(
        ("primary",),
        "UTC",
        60,
        (Rule("room", "Workshop", 38, match_field="location"),),
    )
    snapshot = trusted(
        config,
        event(
            title="Private event",
            location="Workshop",
            start=NOW - timedelta(minutes=5),
            end=NOW + timedelta(minutes=5),
        ),
    )
    current = [NOW]

    def unavailable(_calendar, _params):
        raise RuntimeError("private Google outage")

    engine = CachedScheduleEngine(
        config,
        policy=SchedulePolicy(),
        fetch_page=unavailable,
        clock=lambda: current[0],
        storage=_LegacyStorage(snapshot),
    )
    active = engine.tick()
    assert active.refresh_succeeded is False
    assert active.preview.trusted is True
    assert active.preview.desired_states == {38: 1}

    current[0] = NOW + timedelta(minutes=6)
    expired = engine.tick()
    assert expired.refresh_attempted is False
    assert expired.preview.trusted is True
    assert expired.preview.desired_states == {38: 0}
