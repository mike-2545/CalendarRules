import json
from pathlib import Path

import pytest

from eisy_google_calendar.models import CalendarConfig
from eisy_google_calendar.validation import ConfigurationError, load_config, load_config_data


EXAMPLE = Path(__file__).parents[1] / "examples" / "rules.example.json"


def base_config():
    return {
        "calendar_ids": ["primary"],
        "timezone": "America/New_York",
        "poll_seconds": 60,
        "rules": [
            {
                "name": "Workshop",
                "match": {"title_contains": "Workshop", "case_sensitive": False},
                "isy_state_variable_id": 10,
                "start_offset_minutes": -30,
                "end_offset_minutes": 0,
            }
        ],
    }


def assert_config_error(data, path):
    with pytest.raises(ConfigurationError, match=rf"{path}"):
        load_config_data(data)


def test_example_file_loads_without_modifying_schema():
    config = load_config(EXAMPLE)
    assert isinstance(config, CalendarConfig)
    assert config.calendar_ids == ("primary",)
    assert config.timezone == "America/New_York"
    assert config.poll_seconds == 60
    assert [rule.name for rule in config.rules] == ["Workshop", "Conference Room"]
    assert config.rules[0].target_variable_id == 10
    assert config.rules[0].start_offset_minutes == -30


def test_rule_defaults_are_applied():
    data = base_config()
    data["rules"][0]["match"] = {"title_contains": "Meeting"}
    data["rules"][0].pop("start_offset_minutes")
    data["rules"][0].pop("end_offset_minutes")
    config = load_config_data(data)
    parsed = config.rules[0]
    assert parsed.case_sensitive is False
    assert parsed.start_offset_minutes == 0
    assert parsed.end_offset_minutes == 0
    assert parsed.ignore_all_day is True
    assert parsed.ignore_cancelled is True


def test_optional_ignore_settings_are_loaded():
    data = base_config()
    data["rules"][0].update({"ignore_all_day": False, "ignore_cancelled": False})
    parsed = load_config_data(data).rules[0]
    assert parsed.ignore_all_day is False
    assert parsed.ignore_cancelled is False


@pytest.mark.parametrize("missing", ["calendar_ids", "timezone", "poll_seconds", "rules"])
def test_required_top_level_settings(missing):
    data = base_config()
    del data[missing]
    assert_config_error(data, missing)


def test_calendar_ids_must_be_non_empty_strings():
    for value in ([], [""], [1], "primary"):
        data = base_config()
        data["calendar_ids"] = value
        assert_config_error(data, "calendar_ids")


@pytest.mark.parametrize("timezone_name", ["", "Not/AZone", "UTC/NotAZone"])
def test_timezone_must_be_valid_iana_name(timezone_name):
    data = base_config()
    data["timezone"] = timezone_name
    assert_config_error(data, "timezone")


@pytest.mark.parametrize("value", [0, -1, True, 60.0, "60"])
def test_poll_seconds_must_be_positive_strict_integer(value):
    data = base_config()
    data["poll_seconds"] = value
    assert_config_error(data, "poll_seconds")


def test_rules_must_be_non_empty_list():
    for value in ([], {}, "rules"):
        data = base_config()
        data["rules"] = value
        assert_config_error(data, "rules")


@pytest.mark.parametrize("value", [0, -1, True, 10.0, "10"])
def test_variable_id_must_be_positive_integer_not_boolean(value):
    data = base_config()
    data["rules"][0]["isy_state_variable_id"] = value
    assert_config_error(data, r"rules\[0\]\.isy_state_variable_id")


def test_title_matching_is_required_and_strict():
    for value in [None, "", 1, True]:
        data = base_config()
        data["rules"][0]["match"]["title_contains"] = value
        assert_config_error(data, r"rules\[0\]\.match\.title_contains")


def test_case_sensitive_must_be_boolean():
    data = base_config()
    data["rules"][0]["match"]["case_sensitive"] = 0
    assert_config_error(data, r"rules\[0\]\.match\.case_sensitive")


@pytest.mark.parametrize("field", ["start_offset_minutes", "end_offset_minutes"])
def test_offsets_are_signed_strict_integers(field):
    for value in [-120, 0, 45]:
        data = base_config()
        data["rules"][0][field] = value
        assert isinstance(load_config_data(data).rules[0].__getattribute__(field), int)
    for value in [True, 1.5, "-30"]:
        data = base_config()
        data["rules"][0][field] = value
        assert_config_error(data, rf"rules\[0\]\.{field}")


def test_duplicate_rule_names_are_case_insensitive():
    data = base_config()
    duplicate = dict(data["rules"][0])
    duplicate["name"] = "wOrKsHoP"
    data["rules"].append(duplicate)
    assert_config_error(data, r"rules\[1\]\.name")


@pytest.mark.parametrize("location", ["root", "rule", "match"])
def test_unknown_fields_are_rejected_at_each_level(location):
    data = base_config()
    if location == "root":
        data["unexpected"] = True
        path = r"config\.unexpected"
    elif location == "rule":
        data["rules"][0]["unexpected"] = True
        path = r"rules\[0\]\.unexpected"
    else:
        data["rules"][0]["match"]["unexpected"] = True
        path = r"rules\[0\]\.match\.unexpected"
    assert_config_error(data, path)


def test_note_metadata_is_accepted_and_ignored():
    data = base_config()
    data["note"] = "documentation only"
    assert load_config_data(data).calendar_ids == ("primary",)


def test_malformed_json_has_clear_config_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="config: invalid JSON"):
        load_config(path)


def test_non_object_data_has_config_path():
    with pytest.raises(ConfigurationError, match="config: must be an object"):
        load_config_data([])


def test_missing_file_has_clear_config_error(tmp_path):
    with pytest.raises(ConfigurationError, match="config: file not found"):
        load_config(tmp_path / "missing.json")
