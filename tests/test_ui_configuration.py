from datetime import datetime, timedelta, timezone

import pytest

from eisy_google_calendar.config_identity import configuration_fingerprint
from eisy_google_calendar.models import (
    CalendarEvent,
    CalendarSnapshot,
    SnapshotStatus,
)
from eisy_google_calendar.ui_configuration import (
    MAX_OFFSET_MINUTES,
    MIN_OFFSET_MINUTES,
    load_typed_configuration,
    typed_parameter_schema,
)
from eisy_google_calendar.validation import ConfigurationError


UTC = timezone.utc
NOW = datetime(2025, 1, 1, 12, tzinfo=UTC)


def rule(
    name="Workshop",
    title="Workshop",
    variable_id=101,
    start_offset=0,
    end_offset=0,
    enabled=True,
):
    return {
        "name": name,
        "title_contains": title,
        "case_sensitive": False,
        "state_variable_id": variable_id,
        "start_offset_minutes": start_offset,
        "end_offset_minutes": end_offset,
        "enabled": enabled,
    }


def payload(*rules, calendars=("primary",), refresh_hours=2):
    return {
        "timezone": "America/New_York",
        "refresh_hours": refresh_hours,
        "calendars": [{"calendar_id": item} for item in calendars],
        "rules": list(rules or (rule(),)),
    }


def test_schema_uses_only_documented_typed_fields_and_list_rows():
    schema = typed_parameter_schema()
    by_name = {item["name"]: item for item in schema}

    assert by_name["calendars"]["isList"] is True
    assert by_name["rules"]["isList"] is True
    assert by_name["refresh_hours"]["type"] == "STRING"
    assert by_name["refresh_hours"]["defaultValue"] == "2"
    assert by_name["show_event_titles"]["type"] == "STRING"
    assert by_name["show_event_titles"]["defaultValue"] == "false"
    assert by_name["upcoming_event_limit"]["type"] == "STRING"
    assert by_name["upcoming_event_limit"]["defaultValue"] == "5"
    assert {item["type"] for item in by_name["rules"]["params"]} == {"STRING"}
    rule_params = {item["name"]: item for item in by_name["rules"]["params"]}
    assert rule_params["match_field"]["type"] == "STRING"
    assert rule_params["match_field"]["defaultValue"] == "title"
    assert "location" in rule_params["match_field"]["desc"]
    assert rule_params["title_contains"]["title"] == "Event match pattern"
    assert rule_params["title_match_type"]["type"] == "STRING"
    assert rule_params["title_match_type"]["title"] == "Match type"
    assert rule_params["title_match_type"]["defaultValue"] == "contains"
    assert "starts_with" in rule_params["title_match_type"]["desc"]
    assert rule_params["case_sensitive"]["defaultValue"] == "false"
    assert rule_params["start_offset_minutes"]["defaultValue"] == "0"
    assert rule_params["end_offset_minutes"]["defaultValue"] == "0"
    assert rule_params["enabled"]["defaultValue"] == "true"
    assert "options" not in str(schema)
    assert "enum" not in str(schema)


def test_eisy_ui_compatibility_strings_are_parsed_explicitly():
    string_rule = rule(
        variable_id="101",
        start_offset=" -30 ",
        end_offset="+15",
        enabled=" TRUE ",
    )
    string_rule["case_sensitive"] = "true"

    settings = load_typed_configuration(
        payload(string_rule, refresh_hours=" 4 ")
    )

    parsed = settings.calendar.rules[0]
    assert settings.policy.refresh_hours == 4
    assert parsed.target_variable_id == 101
    assert parsed.start_offset_minutes == -30
    assert parsed.end_offset_minutes == 15
    assert parsed.case_sensitive is True
    assert settings.show_event_titles is False
    assert settings.upcoming_event_limit == 5


def test_event_title_display_is_an_explicit_privacy_opt_in():
    legacy = load_typed_configuration(payload(rule()))
    enabled_payload = payload(rule())
    enabled_payload["show_event_titles"] = "true"
    enabled = load_typed_configuration(enabled_payload)

    assert legacy.show_event_titles is False
    assert enabled.show_event_titles is True
    assert legacy.calendar == enabled.calendar
    assert legacy.policy == enabled.policy


def test_upcoming_limit_is_legacy_compatible_and_presentation_only():
    legacy = load_typed_configuration(payload(rule()))
    selected_payload = payload(rule())
    selected_payload["upcoming_event_limit"] = "10"
    selected = load_typed_configuration(selected_payload)

    assert legacy.upcoming_event_limit == 5
    assert selected.upcoming_event_limit == 10
    assert legacy.calendar == selected.calendar
    assert legacy.policy == selected.policy
    assert configuration_fingerprint(legacy.calendar) == configuration_fingerprint(
        selected.calendar
    )


@pytest.mark.parametrize("value", [0, 1, 6, 11, True, "", "  ", "5.0", "ten"])
def test_invalid_upcoming_limit_fails_validation(value):
    candidate = payload(rule())
    candidate["upcoming_event_limit"] = value
    with pytest.raises(ConfigurationError, match="upcoming_event_limit"):
        load_typed_configuration(candidate)


def test_string_false_disables_a_rule_without_changing_other_rules():
    disabled = rule("Disabled", "Secret", "202", enabled="false")
    enabled = rule(variable_id="101", enabled="true")

    settings = load_typed_configuration(payload(disabled, enabled))

    assert [item.name for item in settings.calendar.rules] == ["Workshop"]


def test_add_update_and_delete_rule_rows_are_atomic():
    added = load_typed_configuration(
        payload(rule(), rule("Office", "Office", 202))
    )
    assert [item.name for item in added.calendar.rules] == ["Workshop", "Office"]

    updated = load_typed_configuration(
        payload(rule(start_offset=-30, end_offset=15))
    )
    assert updated.calendar.rules[0].start_offset_minutes == -30
    assert updated.calendar.rules[0].end_offset_minutes == 15

    deleted = load_typed_configuration(payload(rule("Office", "Office", 202)))
    assert [item.name for item in deleted.calendar.rules] == ["Office"]
    with pytest.raises(ConfigurationError, match="enabled rule"):
        load_typed_configuration(payload(rule(enabled=False)))


@pytest.mark.parametrize("hours", [1, 2, 4])
def test_refresh_interval_choices_build_the_offline_policy(hours):
    settings = load_typed_configuration(payload(refresh_hours=hours))
    assert settings.policy.refresh_hours == hours
    assert settings.policy.evaluation_interval == timedelta(minutes=1)
    assert settings.policy.max_snapshot_age == timedelta(days=30)
    assert settings.policy.coverage_horizon == timedelta(days=32)


@pytest.mark.parametrize(
    "hours",
    [0, 3, 8, True, "", "  ", "2.0", "1e0", "three"],
)
def test_invalid_refresh_interval_is_rejected(hours):
    with pytest.raises(ConfigurationError, match="refresh_hours"):
        load_typed_configuration(payload(refresh_hours=hours))


@pytest.mark.parametrize(
    "start,end",
    [
        (MIN_OFFSET_MINUTES, MAX_OFFSET_MINUTES),
        (0, 0),
    ],
)
def test_offset_boundaries_are_accepted(start, end):
    settings = load_typed_configuration(
        payload(rule(start_offset=start, end_offset=end))
    )
    assert settings.calendar.rules[0].start_offset_minutes == start
    assert settings.calendar.rules[0].end_offset_minutes == end


@pytest.mark.parametrize(
    "start,end",
    [
        (MIN_OFFSET_MINUTES - 1, 0),
        (0, MAX_OFFSET_MINUTES + 1),
        (True, 0),
        ("1.5", "0"),
        ("--1", "0"),
    ],
)
def test_invalid_offsets_are_rejected(start, end):
    with pytest.raises(ConfigurationError, match="offset"):
        load_typed_configuration(
            payload(rule(start_offset=start, end_offset=end))
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", "yes"),
        ("enabled", "1"),
        ("case_sensitive", "0"),
        ("case_sensitive", 1),
    ],
)
def test_malformed_boolean_compatibility_values_fail_closed(field, value):
    bad_rule = rule()
    bad_rule[field] = value

    with pytest.raises(ConfigurationError, match="true or false"):
        load_typed_configuration(payload(bad_rule))


def test_duplicate_calendars_and_enabled_rule_names_are_rejected():
    with pytest.raises(ConfigurationError, match="duplicate calendar"):
        load_typed_configuration(payload(calendars=("private", "private")))
    with pytest.raises(ConfigurationError, match="duplicate enabled rule"):
        load_typed_configuration(payload(rule(), rule("workshop", "Other", 202)))


def test_existing_type_two_variable_catalog_can_be_injected_offline():
    accepted = load_typed_configuration(
        payload(rule(variable_id=202)),
        available_state_variable_ids={101, 202},
    )
    assert accepted.calendar.rules[0].target_variable_id == 202

    with pytest.raises(ConfigurationError, match="available type-2"):
        load_typed_configuration(
            payload(rule(variable_id=303)),
            available_state_variable_ids={101, 202},
        )


@pytest.mark.parametrize(
    "bad_payload",
    [
        {},
        {"calendars": [], "rules": []},
        payload(rule(variable_id=0)),
        payload(rule(title="")),
        payload(rule()) | {"unknown": "private value"},
    ],
)
def test_missing_partial_and_invalid_payloads_fail_closed(bad_payload):
    with pytest.raises(ConfigurationError):
        load_typed_configuration(bad_payload)


def test_preview_bridge_withholds_unavailable_data_and_accepts_valid_empty():
    settings = load_typed_configuration(payload())
    unavailable = settings.preview(
        CalendarSnapshot(SnapshotStatus.UNAVAILABLE),
        now=NOW,
    )
    assert unavailable.trusted is False
    assert unavailable.desired_states == {}

    empty = settings.preview(
        CalendarSnapshot(
            SnapshotStatus.VALID,
            events=(),
            fetched_at=NOW,
            coverage_start=NOW,
            coverage_end=NOW + timedelta(hours=24),
        ),
        now=NOW,
    )
    assert empty.trusted is True
    assert empty.desired_states == {101: 0}


def test_preview_bridge_uses_enabled_rules_without_variable_operations():
    settings = load_typed_configuration(
        payload(rule(), rule("Disabled", "Secret", 202, enabled=False))
    )
    snapshot = CalendarSnapshot(
        SnapshotStatus.VALID,
        events=(
            CalendarEvent(
                "synthetic",
                "Workshop",
                NOW - timedelta(minutes=5),
                NOW + timedelta(minutes=5),
            ),
        ),
        fetched_at=NOW,
        coverage_start=NOW,
        coverage_end=NOW + timedelta(hours=24),
    )
    result = settings.preview(snapshot, now=NOW)
    assert result.desired_states == {101: 1}
