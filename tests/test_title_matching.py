from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from eisy_google_calendar.config_identity import configuration_fingerprint
from eisy_google_calendar.evaluator import evaluate_snapshot, title_matches
from eisy_google_calendar.models import (
    TITLE_MATCH_TYPES,
    CalendarConfig,
    CalendarEvent,
    CalendarSnapshot,
    Rule,
    SnapshotStatus,
)
from eisy_google_calendar.preview import preview_google_responses, preview_snapshot
from eisy_google_calendar.reconciler import ReconciliationStatus, reconcile_snapshot
from eisy_google_calendar.ui_configuration import load_typed_configuration
from eisy_google_calendar.validation import ConfigurationError, load_config_data


UTC = timezone.utc
NOW = datetime(2026, 11, 12, 19, tzinfo=UTC)
LEGACY_FINGERPRINT = "7856281dbe10aea720441d174760ba86927a03515891dbfdb533112d5a7108b2"


def legacy_config_data():
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


def typed_rule(**changes):
    value = {
        "name": "test3",
        "title_contains": "test",
        "case_sensitive": "false",
        "state_variable_id": "38",
        "start_offset_minutes": "0",
        "end_offset_minutes": "0",
        "enabled": "true",
    }
    value.update(changes)
    return value


def typed_payload(rule):
    return {
        "timezone": "America/New_York",
        "refresh_hours": "2",
        "calendars": [{"calendar_id": "primary"}],
        "rules": [rule],
    }


@pytest.mark.parametrize(
    "match_type,pattern,matching,nonmatching",
    [
        ("contains", "test", "Workshop test3", "Workshop"),
        ("exact", "test3", "test3", "test3 extended"),
        ("starts_with", "Workshop", "Workshop test3", "Morning Workshop"),
        ("ends_with", "test3", "Workshop test3", "test3 Workshop"),
    ],
)
def test_each_operator_matches_only_its_defined_case_sensitive_shape(
    match_type, pattern, matching, nonmatching
):
    assert title_matches(
        matching, pattern, match_type=match_type, case_sensitive=True
    )
    assert not title_matches(
        nonmatching, pattern, match_type=match_type, case_sensitive=True
    )
    assert not title_matches(
        matching.upper(), pattern, match_type=match_type, case_sensitive=True
    )
    assert title_matches(
        matching.upper(), pattern, match_type=match_type, case_sensitive=False
    )


@pytest.mark.parametrize(
    "match_type,title",
    [
        ("contains", "Vor Straße nach"),
        ("exact", "Straße"),
        ("starts_with", "Straße meeting"),
        ("ends_with", "Meeting Straße"),
    ],
)
def test_unicode_casefolding_is_used_for_every_operator(match_type, title):
    assert title_matches(
        title, "STRASSE", match_type=match_type, case_sensitive=False
    )
    assert not title_matches(
        title, "STRASSE", match_type=match_type, case_sensitive=True
    )


@pytest.mark.parametrize("pattern", ["", None, 1])
def test_empty_or_invalid_patterns_fail_closed(pattern):
    with pytest.raises(ConfigurationError, match="pattern"):
        title_matches("test3", pattern)  # type: ignore[arg-type]


@pytest.mark.parametrize("match_type", ["", "regex", "startswith", None])
def test_unknown_match_types_fail_closed(match_type):
    with pytest.raises(ConfigurationError, match="match type"):
        title_matches("test3", "test", match_type=match_type)  # type: ignore[arg-type]


def test_legacy_and_explicit_contains_configs_share_exact_043_fingerprint():
    legacy = load_config_data(legacy_config_data())
    explicit_data = deepcopy(legacy_config_data())
    explicit_data["rules"][0]["match"]["title_match_type"] = "contains"
    explicit = load_config_data(explicit_data)

    assert legacy.rules[0].title_match_type == "contains"
    assert explicit.rules[0].title_match_type == "contains"
    assert configuration_fingerprint(legacy) == LEGACY_FINGERPRINT
    assert configuration_fingerprint(explicit) == LEGACY_FINGERPRINT


def test_each_nondefault_operator_has_a_distinct_schedule_identity():
    fingerprints = {}
    for match_type in TITLE_MATCH_TYPES:
        data = deepcopy(legacy_config_data())
        data["rules"][0]["match"]["title_match_type"] = match_type
        fingerprints[match_type] = configuration_fingerprint(load_config_data(data))

    assert fingerprints["contains"] == LEGACY_FINGERPRINT
    assert len(set(fingerprints.values())) == len(TITLE_MATCH_TYPES)


def test_legacy_test3_contains_rule_keeps_the_043_desired_state():
    event = CalendarEvent(
        "event",
        "Workshop test3",
        NOW - timedelta(minutes=5),
        NOW + timedelta(minutes=5),
    )
    snapshot = CalendarSnapshot(SnapshotStatus.VALID, (event,))
    legacy = Rule("test3", "test", 38)
    explicit = Rule("test3", "test", 38, title_match_type="contains")

    assert evaluate_snapshot(snapshot, (legacy,), NOW).desired_states == {38: 1}
    assert evaluate_snapshot(snapshot, (explicit,), NOW).desired_states == {38: 1}


@pytest.mark.parametrize(
    "match_type,pattern,title,expected",
    [
        ("contains", "test", "Workshop test3", 1),
        ("exact", "test3", "Workshop test3", 0),
        ("starts_with", "Workshop", "Workshop test3", 1),
        ("ends_with", "test3", "Workshop test3", 1),
    ],
)
def test_offline_preview_uses_the_selected_operator(
    match_type, pattern, title, expected
):
    config = CalendarConfig(
        ("primary",),
        "America/New_York",
        60,
        (Rule("rule", pattern, 38, title_match_type=match_type),),
    )
    snapshot = CalendarSnapshot(
        SnapshotStatus.VALID,
        (
            CalendarEvent(
                "event",
                title,
                NOW - timedelta(minutes=5),
                NOW + timedelta(minutes=5),
            ),
        ),
        fetched_at=NOW,
        coverage_start=NOW - timedelta(hours=1),
        coverage_end=NOW + timedelta(hours=1),
    )

    result = preview_snapshot(config, snapshot, now=NOW, require_coverage=True)
    assert result.trusted is True
    assert result.desired_states == {38: expected}


def test_multiple_operator_rules_and_overlapping_events_still_combine_with_or():
    snapshot = CalendarSnapshot(
        SnapshotStatus.VALID,
        (
            CalendarEvent(
                "exact-event",
                "test3",
                NOW - timedelta(minutes=10),
                NOW + timedelta(minutes=10),
            ),
            CalendarEvent(
                "starts-event",
                "Workshop extended",
                NOW - timedelta(minutes=5),
                NOW + timedelta(minutes=20),
            ),
        ),
    )
    rules = (
        Rule("exact", "test3", 38, title_match_type="exact"),
        Rule("starts", "Workshop", 38, title_match_type="starts_with"),
    )

    result = evaluate_snapshot(snapshot, rules, NOW)
    assert result.trusted is True
    assert result.desired_states == {38: 1}
    assert result.active_matches == {38: ("exact-event", "starts-event")}
    assert result.matched_rules == {38: ("exact", "starts")}


def test_multiple_title_operators_do_not_add_google_requests():
    calls = []

    def fetch_page(calendar_id, params):
        calls.append((calendar_id, dict(params)))
        return {
            "items": [
                {
                    "id": "event",
                    "summary": "Workshop test3",
                    "status": "confirmed",
                    "start": {"dateTime": "2026-11-12T18:55:00Z"},
                    "end": {"dateTime": "2026-11-12T19:05:00Z"},
                }
            ]
        }

    config = CalendarConfig(
        ("primary",),
        "America/New_York",
        60,
        tuple(
            Rule(f"rule-{match_type}", pattern, 38, title_match_type=match_type)
            for match_type, pattern in (
                ("contains", "test"),
                ("exact", "Workshop test3"),
                ("starts_with", "Workshop"),
                ("ends_with", "test3"),
            )
        ),
    )

    result = preview_google_responses(config, fetch_page, now=NOW)
    assert result.desired_states == {38: 1}
    assert len(calls) == 1


def test_typed_config_defaults_legacy_rows_and_rejects_unknown_modes():
    legacy = load_typed_configuration(typed_payload(typed_rule()))
    explicit = load_typed_configuration(
        typed_payload(typed_rule(title_match_type="exact"))
    )
    assert legacy.calendar.rules[0].title_match_type == "contains"
    assert explicit.calendar.rules[0].title_match_type == "exact"
    assert explicit.calendar.calendar_ids == legacy.calendar.calendar_ids
    assert explicit.calendar.rules[0].target_variable_id == 38
    assert explicit.calendar.rules[0].start_offset_minutes == 0
    assert explicit.calendar.rules[0].end_offset_minutes == 0
    assert explicit.calendar.rules[0].case_sensitive is False
    assert explicit.calendar.rules[0].ignore_all_day is True
    assert explicit.calendar.rules[0].ignore_cancelled is True
    assert explicit.calendar.timezone == legacy.calendar.timezone
    assert explicit.policy == legacy.policy

    with pytest.raises(ConfigurationError, match="title_match_type"):
        load_typed_configuration(
            typed_payload(typed_rule(title_match_type="wildcard"))
        )

    with pytest.raises(ConfigurationError, match="title_match_type"):
        load_typed_configuration(
            typed_payload(
                typed_rule(title_match_type="wildcard", enabled="false")
            )
        )


def test_json_config_rejects_unknown_operator_instead_of_defaulting():
    data = legacy_config_data()
    data["rules"][0]["match"]["title_match_type"] = "regex"
    with pytest.raises(ConfigurationError, match="title_match_type"):
        load_config_data(data)


def test_match_modes_do_not_bypass_variable_catalog_or_calendar_trust():
    with pytest.raises(ConfigurationError, match="available type-2"):
        load_typed_configuration(
            typed_payload(
                typed_rule(title_match_type="exact", state_variable_id="39")
            ),
            available_state_variable_ids={38},
        )

    class NoCallTransport:
        def __init__(self):
            self.calls = []

        def get(self, path):
            self.calls.append(("get", path))
            raise AssertionError("untrusted data must not read IoX")

        def set(self, path):
            self.calls.append(("set", path))
            raise AssertionError("untrusted data must not write IoX")

    transport = NoCallTransport()
    result = reconcile_snapshot(
        CalendarSnapshot(SnapshotStatus.FAILED, error="offline"),
        (Rule("exact", "test3", 38, title_match_type="exact"),),
        NOW,
        transport,
    )
    assert result.status is ReconciliationStatus.UNTRUSTED
    assert transport.calls == []


def test_invalid_operator_cannot_reach_reconciliation_transport():
    class NoCallTransport:
        calls = []

        def get(self, path):
            self.calls.append(("get", path))

        def set(self, path):
            self.calls.append(("set", path))

    transport = NoCallTransport()
    result = reconcile_snapshot(
        CalendarSnapshot(SnapshotStatus.VALID),
        (Rule("bad", "test", 38, title_match_type="regex"),),
        NOW,
        transport,
    )
    assert result.status is ReconciliationStatus.ERROR
    assert transport.calls == []
