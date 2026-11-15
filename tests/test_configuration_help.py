"""Keep user-visible eisy-ui help accurate and complete."""

from eisy_google_calendar.ui_configuration import typed_parameter_schema


def test_every_visible_typed_field_has_help():
    schema = typed_parameter_schema()
    for field in schema:
        assert field.get("desc", "").strip(), field["name"]
        for nested in field.get("params", []):
            assert nested.get("desc", "").strip(), (
                field["name"], nested["name"]
            )


def test_calendar_id_help_accepts_primary_and_identifies_exact_ids():
    schema = {field["name"]: field for field in typed_parameter_schema()}
    calendar_id = schema["calendars"]["params"][0]["desc"]
    assert "primary" in calendar_id
    assert "@group.calendar.google.com" in calendar_id
    assert "Integrate calendar" in calendar_id
    assert "display name" in calendar_id
    assert "web URL" in calendar_id
    assert "OAuth discovery is not enabled" not in schema["calendars"]["desc"]


def test_tooltips_describe_real_matching_and_offsets():
    schema = {field["name"]: field for field in typed_parameter_schema()}
    rule = {item["name"]: item for item in schema["rules"]["params"]}
    assert "Location field" in rule["match_field"]["desc"]
    assert "description" in rule["match_field"]["desc"]
    assert all(op in rule["title_match_type"]["desc"] for op in (
        "contains", "exact", "starts_with", "ends_with"
    ))
    assert "-30" in rule["start_offset_minutes"]["desc"]
    assert "+30" in rule["end_offset_minutes"]["desc"]
    assert "type-2" in rule["state_variable_id"]["desc"]
    assert "does not force" in rule["enabled"]["desc"]


def test_general_tooltips_distinguish_google_refresh_and_eisy_poll():
    schema = {field["name"]: field for field in typed_parameter_schema()}
    assert "All-day events are ignored" in schema["timezone"]["desc"]
    assert "Short/Long Poll" in schema["refresh_hours"]["desc"]
    assert "does not limit events evaluated" in schema["upcoming_event_limit"]["desc"]
    assert "locations stay hidden" in schema["show_event_titles"]["desc"]
