import json
import sys
from datetime import datetime, timedelta, timezone
from threading import Event
from time import monotonic, sleep
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"
sys.path.insert(0, str(PLUGIN))


class FakeNotices(dict):
    def delete(self, key):
        self.pop(key, None)


class FakeNode:
    def __init__(self, poly, primary, address, name):
        self.poly = poly
        self.primary = primary
        self.address = address
        self.name = name
        self.drivers = [dict(driver) for driver in type(self).drivers]
        self.driver_updates = []
        self.fail_driver_update = False

    def setDriver(self, driver, value, report=True, force=False):
        self.driver_updates.append((driver, value, report, force))
        if self.fail_driver_update:
            raise RuntimeError("driver publication failed")
        for item in self.drivers:
            if item["driver"] == driver:
                changed = item["value"] != value
                item["value"] = value
                return changed
        raise ValueError(f"invalid driver: {driver}")

    def getDriver(self, driver):
        for item in self.drivers:
            if item["driver"] == driver:
                return item["value"]
        return None


class FakeCustom:
    def __init__(self, poly, key):
        self.poly = poly
        self.key = key
        self.data = None

    def load(self, data, save=False):
        self.data = data
        self.poly.custom_loads.append((self.key, data, save))


class FakePolyglot:
    CONFIG = "CONFIG"
    CUSTOMPARAMS = "CUSTOMPARAMS"
    CUSTOMTYPEDDATA = "CUSTOMTYPEDDATA"
    CUSTOMNS = "CUSTOMNS"
    OAUTH = "OAUTH"
    POLL = "POLL"
    CONFIGDONE = "CONFIGDONE"
    START = "START"
    STOP = "STOP"
    ISY = "ISY"

    def __init__(self):
        self.Notices = FakeNotices()
        self.added = []
        self.subscriptions = []
        self.external_calls = []
        self.custom_loads = []
        self.poll_updates = []
        self.system_messages = []

    def addNode(self, node, conn_status=None):
        self.added.append((node, conn_status))

    def subscribe(self, topic, callback, address=None):
        self.subscriptions.append((topic, callback, address))

    def setPoll(self, short=None, long=None):
        self.poll_updates.append((short, long))

    def send(self, message, destination):
        self.system_messages.append((message, destination))


@pytest.fixture
def controller(monkeypatch):
    fake_udi = SimpleNamespace(Node=FakeNode, Custom=FakeCustom)
    monkeypatch.setitem(sys.modules, "udi_interface", fake_udi)
    import importlib
    module = importlib.import_module("controller")
    return module.CalendarController(FakePolyglot())


def test_metadata_requests_owner_approved_iox_access():
    metadata = json.loads((PLUGIN / "eisy_google_calendar.iox_plugin.json").read_text())
    plugin = metadata["plugin"]
    assert plugin["language"] == "python3"
    assert plugin["executableName"] == "main.py"
    assert plugin["installScript"] == "install.sh"
    assert plugin["worksOnEisy"] is True
    assert plugin["requiresIoXAccess"] is True
    assert plugin["enableOAUTH2"] is True
    assert plugin["enableDiscovery"] is False
    assert plugin["shortPoll"] == 60
    assert plugin["version"] == "0.4.11"
    assert "customParams" not in plugin
    assert "oauthParams" not in plugin
    assert "hardwareConfig" not in plugin


def test_runtime_and_plugin_metadata_versions_match():
    metadata = json.loads((PLUGIN / "eisy_google_calendar.iox_plugin.json").read_text())
    main_source = (PLUGIN / "main.py").read_text()

    assert metadata["plugin"]["version"] == "0.4.11"
    assert 'polyglot.start({"version": "0.4.11", "requestId": False})' in main_source
    assert metadata["plugin"]["profileVersion"] == "3.0.0"
    assert (PLUGIN / "profile/version.txt").read_text().strip() == "1.2.0"


def test_metadata_is_controller_only_with_refresh_command_and_no_children():
    metadata = json.loads((PLUGIN / "eisy_google_calendar.iox_plugin.json").read_text())
    assert len(metadata["nodedefs"]) == 1
    nodedef = metadata["nodedefs"][0]
    assert nodedef["id"] == "calctrl"
    assert nodedef["properties"] == [
        {"id": "ST", "editor": "bool", "name": "Online"}
    ]
    assert nodedef["commands"] == {
        "accepts": [{"id": "REFRESH", "name": "Refresh Now"}],
        "sends": [],
    }


def test_controller_identity_and_managed_connection_status(controller):
    assert controller.id == "calctrl"
    assert controller.address == "calctrl"
    assert controller.name == "Google Calendar"
    assert controller.children == []
    assert controller.drivers == [
        {"driver": "ST", "value": 0, "uom": 25},
        {"driver": "GV0", "value": 0, "uom": 25},
        {"driver": "GV1", "value": 0, "uom": 25},
        {"driver": "GV2", "value": 0, "uom": 56},
        {"driver": "GV3", "value": 0, "uom": 56},
        {"driver": "GV4", "value": 1, "uom": 25},
    ]
    assert controller.poly.added == []
    assert controller._registered is False


def test_register_node_is_idempotent_and_does_not_publish_connected(controller):
    assert controller.register_node() is True
    assert controller.register_node() is True
    assert controller.poly.added == [(controller, "ST")]
    assert controller._registered is True
    assert controller.driver_updates == [
        ("GV0", 0, True, False),
        ("GV1", 0, True, False),
        ("GV2", 0, True, False),
        ("GV3", 0, True, False),
        ("GV4", 1, True, False),
    ]
    assert controller.started is False
    assert controller.drivers[0] == {"driver": "ST", "value": 0, "uom": 25}


def test_controller_subscribes_only_required_lifecycle_events(controller):
    topics = [topic for topic, _, _ in controller.poly.subscriptions]
    assert topics == [
        "CONFIG",
        "CUSTOMPARAMS",
        "CUSTOMTYPEDDATA",
        "CONFIGDONE",
        "START",
        "STOP",
    ]
    assert "POLL" not in topics
    assert "DISCOVER" not in topics


def test_start_then_config_done_initializes_once(controller):
    controller.register_node()
    initial_status_updates = list(controller.driver_updates)
    assert controller._start_event() is True
    assert controller.started is False
    assert controller.driver_updates == initial_status_updates
    assert controller._config_done() is True
    assert controller.started is True
    assert controller.driver_updates == initial_status_updates + [
        ("ST", 1, True, True)
    ]
    assert controller.lifecycle == ["START", "CONFIGDONE", "start"]
    assert controller._config_done() is True
    assert controller._start_event() is True
    assert controller.lifecycle == ["START", "CONFIGDONE", "start", "CONFIGDONE", "START"]


def test_config_done_then_start_initializes_once_with_empty_config(controller):
    controller.register_node()
    assert controller.processConfig({}) is True
    assert controller.parameterHandler({}) is True
    assert controller._config_done() is True
    assert controller.started is False
    assert controller._start_event() is True
    assert controller.started is True
    assert controller.configuration == {}
    assert controller.custom_params == {}
    assert controller.lifecycle == ["CONFIG", "CUSTOMPARAMS", "CONFIGDONE", "START", "start"]


def test_stop_while_start_pending_cancels_pending_initialization(controller):
    assert controller._start_event() is True
    assert controller.started is False
    assert controller._stop_event() is True
    assert controller.started is False
    assert controller.start_requested is False
    assert controller._config_done() is True
    assert controller.started is False
    assert controller.lifecycle == ["START", "STOP", "stop", "CONFIGDONE"]
    assert controller._start_event() is True
    assert controller.started is True


def test_repeated_stop_and_start_are_safe(controller):
    controller._config_done()
    controller._start_event()
    assert controller.started is True
    assert controller._stop_event() is True
    assert controller._stop_event() is True
    assert controller.started is False
    assert controller._start_event() is True
    assert controller.started is False
    assert controller._config_done() is True
    assert controller.started is True


def test_invalid_calendar_config_does_not_block_polyglot_connection(controller):
    assert controller.processConfig(["invalid"]) is False
    assert controller._config_done() is True
    assert controller._start_event() is True
    assert controller.started is True
    assert controller.drivers[0] == {"driver": "ST", "value": 1, "uom": 25}
    assert "config" in controller.poly.Notices


def typed_payload(**changes):
    value = {
        "timezone": "America/New_York",
        "refresh_hours": 2,
        "calendars": [{"calendar_id": "private-calendar"}],
        "rules": [
            {
                "name": "Workshop",
                "title_contains": "Workshop",
                "case_sensitive": False,
                "state_variable_id": 101,
                "start_offset_minutes": -30,
                "end_offset_minutes": 0,
                "enabled": True,
            }
        ],
    }
    value.update(changes)
    return value


def test_typed_schema_is_published_with_native_list_configuration(controller):
    key, schema, save = controller.poly.custom_loads[0]
    assert key == "customtypedparams"
    assert save is True
    by_name = {item["name"]: item for item in schema}
    assert by_name["calendars"]["isList"] is True
    assert by_name["rules"]["isList"] is True
    assert by_name["refresh_hours"]["type"] == "STRING"
    assert by_name["show_event_titles"]["defaultValue"] == "false"
    assert by_name["upcoming_event_limit"]["type"] == "STRING"
    assert by_name["upcoming_event_limit"]["defaultValue"] == "5"
    assert {item["type"] for item in by_name["rules"]["params"]} == {"STRING"}
    rule_params = {item["name"]: item for item in by_name["rules"]["params"]}
    assert rule_params["title_match_type"]["defaultValue"] == "contains"


def test_string_typed_values_activate_without_external_operations(controller):
    value = typed_payload(refresh_hours="2")
    value["rules"][0].update(
        {
            "case_sensitive": "false",
            "state_variable_id": "101",
            "start_offset_minutes": "-30",
            "end_offset_minutes": "0",
            "enabled": "true",
        }
    )

    assert controller.typedConfigurationHandler(value) is True
    rule = controller.active_configuration.calendar.rules[0]
    assert rule.target_variable_id == 101
    assert rule.start_offset_minutes == -30
    assert rule.case_sensitive is False
    assert rule.title_match_type == "contains"
    assert controller.poly.external_calls == []


def test_privacy_display_setting_does_not_invalidate_schedule(controller):
    class Bridge(FakePeriodicPreviewProbe):
        pass

    poly = FakePolyglot()
    bridge = Bridge()
    probe = type(controller)(poly, oauth_bridge=bridge)
    assert probe.typedConfigurationHandler(typed_payload()) is True
    fingerprint_before = probe.active_configuration.calendar

    assert probe.typedConfigurationHandler(
        typed_payload(show_event_titles="true")
    ) is True
    assert probe.active_configuration.show_event_titles is True
    assert probe.active_configuration.calendar == fingerprint_before
    assert bridge.invalidations == 0

    assert probe.typedConfigurationHandler(
        typed_payload(show_event_titles="true", upcoming_event_limit="10")
    ) is True
    assert probe.active_configuration.upcoming_event_limit == 10
    assert probe.active_configuration.calendar == fingerprint_before
    assert bridge.invalidations == 0


def test_activity_notice_is_owner_bounded_and_title_opt_in(controller):
    from eisy_google_calendar.activity import ActivityItem, ActivityResult, VariableActivity
    from eisy_google_calendar.config_identity import configuration_fingerprint
    from eisy_google_calendar.ui_configuration import load_typed_configuration

    poly = FakePolyglot()
    probe = type(controller)(poly)
    assert probe.typedConfigurationHandler(typed_payload()) is True
    now = datetime(2026, 11, 12, 17, tzinfo=timezone.utc)
    result = ActivityResult(
        evaluated_at=now,
        trusted=True,
        cached=True,
        active=(
            ActivityItem(
                "PRIVATE TITLE",
                "Workshop",
                101,
                now,
                now.replace(hour=18),
                now,
                now.replace(hour=18),
                True,
                1,
                now.replace(hour=18),
                0,
            ),
        ),
        variables=(VariableActivity(101, 1, now.replace(hour=18), 0),),
        configuration_fingerprint=configuration_fingerprint(
            probe.active_configuration.calendar
        ),
    )
    assert probe._publish_activity_notice(result) is True
    assert "PRIVATE TITLE" not in poly.Notices["calendar_activity"]
    assert "Trusted cache" in poly.Notices["calendar_activity"]
    assert "PRIVATE TITLE" not in poly.Notices["calendar_activity_active"]

    probe.active_configuration = load_typed_configuration(
        typed_payload(show_event_titles="true")
    )
    assert probe._publish_activity_notice(result) is True
    assert "PRIVATE TITLE" in poly.Notices["calendar_activity_active"]
    assert "Var 101 desired ON" in poly.Notices["calendar_activity_active"]

    assert probe.typedConfigurationHandler(typed_payload()) is True
    assert "PRIVATE TITLE" not in poly.Notices["calendar_activity"]
    assert "calendar_activity_active" not in poly.Notices
    assert "awaiting the next trusted" in poly.Notices["calendar_activity"]


def test_compact_activity_keys_are_deduplicated_and_obsolete_rows_are_deleted(controller):
    from eisy_google_calendar.activity import ActivityItem, ActivityResult
    from eisy_google_calendar.config_identity import configuration_fingerprint
    from eisy_google_calendar.ui_configuration import load_typed_configuration

    class CountingNotices(FakeNotices):
        def __init__(self):
            super().__init__()
            self.writes = 0

        def __setitem__(self, key, value):
            self.writes += 1
            super().__setitem__(key, value)

    poly = FakePolyglot()
    probe = type(controller)(poly)
    probe.active_configuration = load_typed_configuration(
        typed_payload(show_event_titles="true", upcoming_event_limit="10")
    )
    counting = CountingNotices()
    counting.update(poly.Notices)
    poly.Notices = counting
    now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
    upcoming = tuple(
        ActivityItem(
            f"PRIVATE {index}",
            "Workshop",
            101,
            now.replace(hour=13) + timedelta(hours=index),
            now.replace(hour=14) + timedelta(hours=index),
            now.replace(hour=13) + timedelta(hours=index),
            now.replace(hour=14) + timedelta(hours=index),
            False,
            0,
            now.replace(hour=13),
            1,
            now.replace(hour=13) + timedelta(hours=index),
            now.replace(hour=14) + timedelta(hours=index),
            False,
        )
        for index in range(10)
    )
    result = ActivityResult(
        evaluated_at=now,
        trusted=True,
        cached=False,
        upcoming=upcoming,
        configuration_fingerprint=configuration_fingerprint(
            probe.active_configuration.calendar
        ),
    )

    assert probe._publish_activity_notice(result) is True
    writes = counting.writes
    assert "PRIVATE 9" in counting["calendar_activity_upcoming_10"]
    assert probe._publish_activity_notice(result) is True
    assert counting.writes == writes

    probe.active_configuration = load_typed_configuration(
        typed_payload(show_event_titles="false", upcoming_event_limit="5")
    )
    assert probe._publish_activity_notice(result) is True
    assert "calendar_activity_upcoming_06" not in counting
    assert "calendar_activity_upcoming_10" not in counting
    assert "PRIVATE" not in repr(counting)
    assert "[title hidden]" in counting["calendar_activity_upcoming_01"]


def test_privacy_change_clears_details_and_stale_schedule_result_fails_closed(controller):
    from eisy_google_calendar.activity import ActivityItem, ActivityResult
    from eisy_google_calendar.config_identity import configuration_fingerprint

    poly = FakePolyglot()
    probe = type(controller)(poly)
    assert probe.typedConfigurationHandler(
        typed_payload(show_event_titles="true")
    ) is True
    now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
    result = ActivityResult(
        evaluated_at=now,
        trusted=True,
        cached=False,
        upcoming=(
            ActivityItem(
                "PRIVATE TITLE",
                "Workshop",
                101,
                now.replace(hour=13),
                now.replace(hour=14),
                now.replace(hour=13),
                now.replace(hour=14),
                False,
                0,
                now.replace(hour=13),
                1,
                now.replace(hour=13),
                now.replace(hour=14),
                False,
            ),
        ),
        configuration_fingerprint=configuration_fingerprint(
            probe.active_configuration.calendar
        ),
    )
    assert probe._publish_activity_notice(result) is True
    assert "PRIVATE TITLE" in repr(poly.Notices)

    assert probe.typedConfigurationHandler(typed_payload()) is True
    assert "PRIVATE TITLE" not in repr(poly.Notices)
    assert not any("upcoming" in key for key in poly.Notices)
    # A worker result that completed under the prior title-display setting is
    # rendered with the current privacy setting and cannot restore the title.
    assert probe._publish_activity_notice(result) is True
    assert "PRIVATE TITLE" not in repr(poly.Notices)
    assert "[title hidden]" in poly.Notices["calendar_activity_upcoming_01"]

    changed = typed_payload(show_event_titles="true")
    changed["rules"][0]["title_contains"] = "Different"
    assert probe.typedConfigurationHandler(changed) is True
    assert probe._publish_activity_notice(result) is False
    assert poly.Notices["calendar_activity"].startswith(
        "Calendar activity unavailable"
    )
    assert not any("upcoming" in key for key in poly.Notices)


def test_malformed_string_typed_values_invalidate_cache_without_disconnect(controller):
    class FakeSchedule:
        def __init__(self):
            self.invalidations = 0

        def invalidate_configuration(self):
            self.invalidations += 1

    schedule = FakeSchedule()
    controller.schedule_engine = schedule
    value = typed_payload(refresh_hours="2.0")

    assert controller.typedConfigurationHandler(value) is False
    assert controller.active_configuration is None
    assert controller.configuration_status == "invalid"
    assert controller.cache_status == "unavailable"
    assert schedule.invalidations == 1

    controller._config_done()
    assert controller._start_event() is True
    assert controller.drivers[0]["value"] == 1
    assert controller.poly.external_calls == []


def test_missing_auth_configuration_and_connected_health_are_independent(controller):
    assert controller.google_auth_status == "disabled"
    assert controller.typedConfigurationHandler(typed_payload()) is True
    assert controller.configuration_status == "valid"
    assert controller.cache_status == "unavailable"
    assert controller.google_auth_status == "disabled"

    controller._config_done()
    assert controller._start_event() is True
    assert controller.started is True
    assert controller.drivers[0]["value"] == 1
    assert "google_auth" in controller.poly.Notices


def test_partial_typed_update_suspends_schedule_without_disconnect(controller):
    class FakeSchedule:
        def __init__(self):
            self.reconfigured = []
            self.invalidations = 0

        def reconfigure(self, config, policy):
            self.reconfigured.append((config, policy))

        def invalidate_configuration(self):
            self.invalidations += 1

    schedule = FakeSchedule()
    controller.schedule_engine = schedule
    assert controller.typedConfigurationHandler(typed_payload()) is True
    assert len(schedule.reconfigured) == 1

    partial = typed_payload(rules=[])
    assert controller.typedConfigurationHandler(partial) is False
    assert controller.active_configuration is None
    assert controller.configuration_status == "invalid"
    assert controller.cache_status == "unavailable"
    assert schedule.invalidations == 1

    controller._config_done()
    assert controller._start_event() is True
    assert controller.drivers[0]["value"] == 1
    assert "calendar_config" in controller.poly.Notices
    assert "private-calendar" not in controller.poly.Notices["calendar_config"]


def test_state_variable_catalog_validation_requires_no_iox_call(controller):
    controller.available_state_variable_ids = {101, 202}
    assert controller.typedConfigurationHandler(typed_payload()) is True
    bad_rule = dict(typed_payload()["rules"][0], state_variable_id=303)
    assert controller.typedConfigurationHandler(typed_payload(rules=[bad_rule])) is False
    assert "available type-2" in controller.poly.Notices["calendar_config"]
    assert controller.poly.external_calls == []


def test_enabled_rules_define_probe_targets_and_invalid_config_clears_them(controller):
    class TargetProbe:
        failure_category = None

        def __init__(self):
            self.target_ids = frozenset()
            self.configured = []

        def configure_targets(self, target_ids):
            self.target_ids = frozenset(target_ids)
            self.configured.append(self.target_ids)
            return "pending" if self.target_ids else "unconfigured"

        def request(self):
            return "pending" if self.target_ids else "unconfigured"

    probe = TargetProbe()
    instance = type(controller)(FakePolyglot(), iox_read_only_probe=probe)
    first = typed_payload()["rules"][0]
    disabled = dict(
        first,
        name="Disabled",
        state_variable_id=999,
        enabled=False,
    )
    second = dict(
        first,
        name="Office",
        title_contains="Office",
        state_variable_id=202,
    )
    same_target = dict(
        first,
        name="Second Workshop Rule",
        title_contains="Workshop Extended",
    )

    assert instance.typedConfigurationHandler(
        typed_payload(rules=[first, disabled])
    ) is True
    assert probe.target_ids == frozenset({101})

    assert instance.typedConfigurationHandler(
        typed_payload(rules=[first, same_target])
    ) is True
    assert probe.target_ids == frozenset({101})

    assert instance.typedConfigurationHandler(
        typed_payload(rules=[first, second])
    ) is True
    assert probe.target_ids == frozenset({101, 202})

    assert instance.typedConfigurationHandler(typed_payload(rules=[])) is False
    assert probe.target_ids == frozenset()
    assert probe.configured == [
        frozenset({101}),
        frozenset({101}),
        frozenset({101, 202}),
        frozenset(),
    ]


def test_mocked_typed_events_add_edit_disable_and_delete_rules(controller):
    class FakeSchedule:
        def __init__(self):
            self.reconfigured = []
            self.invalidations = 0

        def reconfigure(self, config, policy):
            self.reconfigured.append((config, policy))

        def invalidate_configuration(self):
            self.invalidations += 1

    schedule = FakeSchedule()
    controller.schedule_engine = schedule
    controller._config_done()
    controller._start_event()
    assert controller.drivers[0]["value"] == 1
    connected_updates = list(controller.driver_updates)

    callbacks = {
        topic: callback
        for topic, callback, _address in controller.poly.subscriptions
    }
    configure = callbacks[controller.poly.CUSTOMTYPEDDATA]
    first = typed_payload()["rules"][0]
    second = dict(
        first,
        name="Office",
        title_contains="Office",
        state_variable_id=202,
    )

    assert configure(typed_payload(rules=[first])) is True
    assert len(controller.active_configuration.calendar.rules) == 1

    assert configure(typed_payload(rules=[first, second])) is True
    assert [rule.name for rule in controller.active_configuration.calendar.rules] == [
        "Workshop",
        "Office",
    ]

    edited_second = dict(second, title_contains="Staff Office", end_offset_minutes=20)
    assert configure(typed_payload(rules=[first, edited_second])) is True
    assert controller.active_configuration.calendar.rules[1].title_contains == (
        "Staff Office"
    )
    assert controller.active_configuration.calendar.rules[1].end_offset_minutes == 20

    disabled_first = dict(first, enabled=False)
    assert configure(typed_payload(rules=[disabled_first, edited_second])) is True
    assert [rule.name for rule in controller.active_configuration.calendar.rules] == [
        "Office"
    ]

    assert configure(typed_payload(rules=[edited_second])) is True
    assert [rule.name for rule in controller.active_configuration.calendar.rules] == [
        "Office"
    ]

    assert configure(typed_payload(rules=[])) is False
    assert controller.active_configuration is None
    assert controller.configuration_status == "invalid"
    assert schedule.invalidations == 1
    assert controller.drivers[0]["value"] == 1
    assert controller.driver_updates == connected_updates
    assert controller.poly.external_calls == []


def test_clean_stop(controller):
    controller._config_done()
    controller._start_event()
    assert controller._stop_event() is True
    assert controller.started is False
    assert controller.stopped is True
    assert controller.lifecycle[-2:] == ["STOP", "stop"]
    assert controller.driver_updates == [
        ("ST", 1, True, True),
        ("ST", 0, True, True),
    ]


def test_connection_status_is_published_only_after_ready(controller):
    controller.register_node()
    initial_status_updates = list(controller.driver_updates)
    controller._start_event()
    assert controller.driver_updates == initial_status_updates
    controller._config_done()
    assert controller.driver_updates == initial_status_updates + [
        ("ST", 1, True, True)
    ]
    assert controller.poly.external_calls == []
    assert controller.poly.added == [(controller, "ST")]
    assert controller.drivers[0] == {"driver": "ST", "value": 1, "uom": 25}


def test_connection_status_failure_leaves_controller_disconnected(controller):
    controller.fail_driver_update = True
    controller._config_done()
    assert controller._start_event() is False
    assert controller.started is False
    assert controller.stopped is True
    assert controller.drivers[0] == {"driver": "ST", "value": 0, "uom": 25}
    assert "connection" in controller.poly.Notices


def test_shutdown_status_failure_is_reported_but_still_stops(controller):
    controller._config_done()
    controller._start_event()
    controller.fail_driver_update = True
    assert controller._stop_event() is False
    assert controller.started is False
    assert controller.stopped is True
    assert "connection" in controller.poly.Notices


def test_launcher_constructs_controller_before_start(monkeypatch):
    events = []

    class FakeInterface:
        def start(self, options):
            events.append(("start", options))

        def ready(self):
            events.append("ready")

        def updateProfile(self):
            events.append("updateProfile")

        def runForever(self):
            events.append("runForever")

    fake_poly = FakeInterface()
    fake_udi = SimpleNamespace(Interface=lambda _args: fake_poly)
    def build_controller(poly, *, oauth_bridge=None):
        assert oauth_bridge is None
        events.append(("controller", poly))
        return SimpleNamespace(
            register_node=lambda: events.append("register_node")
        )

    fake_controller = SimpleNamespace(CalendarController=build_controller)
    monkeypatch.setitem(sys.modules, "udi_interface", fake_udi)
    monkeypatch.setitem(sys.modules, "controller", fake_controller)
    sys.modules.pop("main", None)

    import importlib

    module = importlib.import_module("main")
    module.main()

    assert events == [
        ("controller", fake_poly),
        ("start", {"version": "0.4.11", "requestId": False}),
        "ready",
        "updateProfile",
        "register_node",
        "runForever",
    ]


def test_launcher_continues_read_only_when_profile_publication_fails(monkeypatch, caplog):
    events = []

    class FakeInterface:
        def start(self, _options):
            events.append("start")

        def ready(self):
            events.append("ready")

        def updateProfile(self):
            events.append("updateProfile")
            raise RuntimeError("synthetic profile failure")

        def runForever(self):
            events.append("runForever")

    fake_poly = FakeInterface()
    fake_udi = SimpleNamespace(Interface=lambda _args: fake_poly)

    def build_controller(poly, *, oauth_bridge=None):
        assert oauth_bridge is None
        events.append(("controller", poly))
        return SimpleNamespace(
            register_node=lambda: events.append("register_node")
        )

    monkeypatch.setitem(sys.modules, "udi_interface", fake_udi)
    monkeypatch.setitem(
        sys.modules,
        "controller",
        SimpleNamespace(CalendarController=build_controller),
    )
    sys.modules.pop("main", None)

    import importlib

    module = importlib.import_module("main")
    with caplog.at_level("WARNING"):
        module.main()

    assert events == [
        ("controller", fake_poly),
        "start",
        "ready",
        "updateProfile",
        "register_node",
        "runForever",
    ]
    assert "IoX profile publication request failed" in caplog.text
    assert "synthetic profile failure" not in caplog.text


def test_core_package_imports_without_wrapper_side_effects():
    import eisy_google_calendar
    from eisy_google_calendar import evaluator, iox_variables, models, reconciler, validation

    assert eisy_google_calendar.evaluate_snapshot is evaluator.evaluate_snapshot
    assert iox_variables.write_state_variable.__module__.endswith("iox_variables")
    assert reconciler.reconcile_snapshot.__module__.endswith("reconciler")
    assert models.CalendarSnapshot.__module__.endswith("models")
    assert validation.validate_rule.__module__.endswith("validation")


def test_wrapper_source_contains_no_forbidden_integration_calls():
    source = (PLUGIN / "controller.py").read_text() + (PLUGIN / "main.py").read_text()
    for forbidden in ("googleapiclient", "getAccessToken(", "ISY(", "write_state_variable", "requests.", "urllib.", "threading.Thread", "time.sleep"):
        assert forbidden not in source


class FakeSdkOAuth:
    """Only records event *types*; never stores credential payloads."""

    def __init__(self):
        self.calls = []
        self.failure = None

    def customNsHandler(self, key, data):
        if self.failure is not None:
            raise RuntimeError(self.failure)
        self.calls.append(("CUSTOMNS", key))

    def oauthHandler(self, data):
        if self.failure is not None:
            raise RuntimeError(self.failure)
        self.calls.append(("OAUTH",))


def test_default_plugin_does_not_subscribe_to_oauth_or_create_sdk(controller):
    assert controller.oauth_bridge is None
    topics = [topic for topic, _, _ in controller.poly.subscriptions]
    assert "CUSTOMNS" not in topics
    assert "OAUTH" not in topics
    assert controller.google_auth_status == "disabled"


def test_explicit_injected_bridge_forwards_only_sdk_events(controller):
    from oauth_event_bridge import UdiOAuthEventBridge

    sdk = FakeSdkOAuth()
    poly = FakePolyglot()
    probe = type(controller)(poly, oauth_bridge=UdiOAuthEventBridge(sdk))
    callbacks = {
        topic: callback
        for topic, callback, _ in poly.subscriptions
    }
    assert "CUSTOMNS" in callbacks and "OAUTH" in callbacks
    probe._config_done()
    probe._start_event()
    assert probe.drivers[0]["value"] == 1

    sensitive_config = {"client_secret": "SENSITIVE_CLIENT_SECRET"}
    sensitive_tokens = {
        "access_token": "SENSITIVE_ACCESS_TOKEN",
        "refresh_token": "SENSITIVE_REFRESH_TOKEN",
    }

    assert callbacks["CUSTOMNS"]("unrelated", sensitive_config) is True
    assert sdk.calls == []
    assert callbacks["CUSTOMNS"]("oauth", sensitive_config) is True
    assert probe.google_auth_status == "configuration_received"
    assert callbacks["CUSTOMNS"]("oauthTokens", sensitive_tokens) is True
    assert callbacks["OAUTH"](sensitive_tokens) is True
    assert probe.google_auth_status == "grant_received"
    assert sdk.calls == [
        ("CUSTOMNS", "oauth"),
        ("CUSTOMNS", "oauthTokens"),
        ("OAUTH",),
    ]
    assert probe.drivers[0]["value"] == 1
    assert probe.driver_updates == [("ST", 1, True, True)]
    assert poly.external_calls == []
    for sensitive in tuple(sensitive_config.values()) + tuple(sensitive_tokens.values()):
        assert sensitive not in str(poly.Notices)
        assert sensitive not in probe.google_auth_status


def test_injected_sdk_errors_are_redacted_without_disconnect(controller):
    from oauth_event_bridge import UdiOAuthEventBridge

    sdk = FakeSdkOAuth()
    sdk.failure = "SENSITIVE_SDK_EXCEPTION"
    poly = FakePolyglot()
    probe = type(controller)(poly, oauth_bridge=UdiOAuthEventBridge(sdk))
    callbacks = {
        topic: callback
        for topic, callback, _ in poly.subscriptions
    }
    probe._config_done()
    probe._start_event()
    assert callbacks["CUSTOMNS"]("oauth", {"client_secret": "SENSITIVE_VALUE"}) is False
    assert callbacks["OAUTH"]({"access_token": "SENSITIVE_VALUE"}) is False
    assert probe.google_auth_status == "event_error"
    assert "SENSITIVE_" not in str(poly.Notices)
    assert probe.drivers[0]["value"] == 1


def test_oauth_bridge_rejects_malformed_payloads_and_never_checks_access(controller):
    from oauth_event_bridge import OAuthEventStatus, UdiOAuthEventBridge

    sdk = FakeSdkOAuth()
    bridge = UdiOAuthEventBridge(sdk)
    assert bridge.authorization_event(None) is False
    assert bridge.status is OAuthEventStatus.EVENT_ERROR
    assert bridge.custom_namespace("oauthTokens", "SENSITIVE_BAD_PAYLOAD") is False
    assert bridge.custom_namespace("oauth", {}) is True
    assert bridge.status is OAuthEventStatus.CONFIGURATION_RECEIVED
    assert not hasattr(bridge, "get_access_token")
    assert sdk.calls == [("CUSTOMNS", "oauth")]


class FakeReadOnlyProbe:
    def __init__(self, *, status="ready", count=2):
        from oauth_event_bridge import OAuthEventStatus
        self.status = OAuthEventStatus.NOT_CONFIGURED
        self.result_status = status
        self.count = count
        self.calls = 0

    def custom_namespace(self, key, data):
        return True

    def authorization_event(self, data):
        return True

    def discover_once(self):
        self.calls += 1
        return SimpleNamespace(status=self.result_status, calendar_count=self.count)


def test_opt_in_discovery_uses_one_poll_and_aggregate_only_notice(controller):
    poly = FakePolyglot()
    bridge = FakeReadOnlyProbe()
    probe = type(controller)(poly, oauth_bridge=bridge)
    callbacks = {topic: fn for topic, fn, _ in poly.subscriptions}
    assert "POLL" in callbacks
    assert callbacks["POLL"]() is False  # Not started yet
    assert bridge.calls == 0

    probe._config_done()
    probe._start_event()
    before = list(probe.driver_updates)
    assert callbacks["POLL"]() is True
    assert bridge.calls == 1
    assert probe.google_auth_status == "ready"
    assert "2 calendars found" in poly.Notices["google_auth"]
    assert "SENSITIVE" not in str(poly.Notices)
    assert callbacks["POLL"]() is True
    assert bridge.calls == 1
    assert probe.driver_updates == before
    assert poly.external_calls == []


def test_auth_failure_does_not_report_empty_success_or_change_connected(controller):
    poly = FakePolyglot()
    bridge = FakeReadOnlyProbe(status="api_error", count=5)
    probe = type(controller)(poly, oauth_bridge=bridge)
    callbacks = {topic: fn for topic, fn, _ in poly.subscriptions}
    probe._config_done()
    probe._start_event()
    before = list(probe.driver_updates)
    assert callbacks["POLL"]() is False
    assert "discovery failed" in poly.Notices["google_auth"]
    assert probe.google_auth_status == "discovery_failed"
    assert probe.driver_updates == before
    assert poly.external_calls == []
    assert callbacks["POLL"]() is True
    assert bridge.calls == 1


def test_unexpected_discovery_exception_does_not_leak_or_disconnect(controller):
    class FailingReadOnly(FakeReadOnlyProbe):
        def discover_once(self):
            raise RuntimeError("PRIVATE_ACCESS_TOKEN_IN_EXCEPTION")

    poly = FakePolyglot()
    probe = type(controller)(poly, oauth_bridge=FailingReadOnly())
    callbacks = {topic: fn for topic, fn, _ in poly.subscriptions}
    probe._config_done()
    probe._start_event()
    assert callbacks["POLL"]() is False
    assert "PRIVATE_" not in str(poly.Notices)
    assert probe.google_auth_status == "discovery_failed"
    assert probe.drivers[0]["value"] == 1


def test_existing_token_blind_bridge_has_no_discovery_poll(controller):
    from oauth_event_bridge import TokenBlindOAuthSink, UdiOAuthEventBridge
    poly = FakePolyglot()
    probe = type(controller)(poly, oauth_bridge=UdiOAuthEventBridge(TokenBlindOAuthSink()))
    assert "POLL" not in [topic for topic, _, _ in poly.subscriptions]


class FakeEventPreviewProbe:
    def __init__(self, status="ready", events=8, on=1, total=2):
        from oauth_event_bridge import OAuthEventStatus
        self.status = OAuthEventStatus.NOT_CONFIGURED
        self.result_status = status
        self.events = events
        self.on = on
        self.total = total
        self.calls = 0
        self.got_config = None

    def custom_namespace(self, key, data):
        return True

    def authorization_event(self, data):
        return True

    def preview_once(self, config):
        self.calls += 1
        self.got_config = config
        return SimpleNamespace(
            status=self.result_status, events_checked=self.events,
            active_variable_count=self.on, total_variable_count=self.total,
        )


class FakePeriodicPreviewProbe:
    """Lifecycle-aware stand-in that never contacts Google or IoX."""

    def __init__(self):
        from oauth_event_bridge import OAuthEventStatus

        self._status_type = OAuthEventStatus
        self.status = OAuthEventStatus.NOT_CONFIGURED
        self.tick_calls = 0
        self.refreshes = 0
        self.invalidations = 0
        self.completed = False

    def custom_namespace(self, key, _data):
        if key == "oauth":
            self.status = self._status_type.CONFIGURATION_RECEIVED
        elif key == "oauthTokens":
            self.status = self._status_type.GRANT_RECEIVED
        return True

    def authorization_event(self, _data):
        self.status = self._status_type.GRANT_RECEIVED
        return True

    def schedule_tick(self, _config):
        self.tick_calls += 1
        if self.status is not self._status_type.GRANT_RECEIVED:
            return SimpleNamespace(status="authentication_required")
        if self.completed:
            return SimpleNamespace(status="idle")
        self.completed = True
        self.refreshes += 1
        return SimpleNamespace(
            status="ready",
            refresh_succeeded=True,
            events_checked=3,
            active_variable_count=1,
            total_variable_count=2,
        )

    def invalidate_schedule(self):
        self.invalidations += 1
        self.completed = False


def test_refresh_now_uses_supported_controller_command_and_sanitized_status(
    controller,
):
    class RefreshBridge(FakePeriodicPreviewProbe):
        def __init__(self):
            super().__init__()
            self.status = self._status_type.GRANT_RECEIVED
            self.manual_requests = 0
            self.manual_pending = False
            self.fail_next = False

        def request_refresh(self, _config):
            self.manual_requests += 1
            self.manual_pending = True
            return SimpleNamespace(status="pending", manual_refresh_pending=True)

        def schedule_tick(self, _config):
            if self.manual_pending:
                self.manual_pending = False
                return SimpleNamespace(
                    status="ready",
                    refresh_succeeded=not self.fail_next,
                    manual_refresh_pending=False,
                    last_successful_sync_at=datetime(
                        2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc
                    ),
                    events_checked=1,
                    active_variable_count=0,
                    total_variable_count=1,
                )
            return SimpleNamespace(status="idle")

    poly = FakePolyglot()
    bridge = RefreshBridge()
    probe = type(controller)(poly, oauth_bridge=bridge)
    assert probe.commands == {"REFRESH": type(probe).refresh_now}
    assert probe.typedConfigurationHandler(typed_payload())
    probe._config_done()
    probe._start_event()

    assert probe.refresh_now({"cmd": "REFRESH"}) is True
    assert bridge.manual_requests == 1
    assert probe.refresh_status == "pending"
    assert "pending" in poly.Notices["calendar_refresh"]
    assert "not available" in poly.Notices["calendar_refresh"]

    assert probe._periodic_schedule_handler() is True
    assert probe.refresh_status == "succeeded"
    assert (
        "Last successful Google sync: Jan 1, 2025, 10:04:05 PM EST."
        in poly.Notices["calendar_refresh"]
    )
    assert probe.last_successful_sync_at == datetime(
        2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc
    )
    assert probe.last_successful_sync_at.tzinfo is timezone.utc
    assert "PRIVATE" not in str(poly.Notices)
    assert probe.iox_writes_allowed is False

    bridge.fail_next = True
    assert probe.refresh_now({"cmd": "REFRESH"}) is True
    assert probe._periodic_schedule_handler() is True
    assert probe.refresh_status == "failed_using_cache"
    assert "using the last trusted cache" in poly.Notices["calendar_refresh"]
    assert (
        "Last successful Google sync: Jan 1, 2025, 10:04:05 PM EST."
        in poly.Notices["calendar_refresh"]
    )
    assert probe.iox_writes_allowed is False


def test_native_status_drivers_follow_the_asynchronous_refresh_lifecycle(controller):
    class RefreshBridge(FakePeriodicPreviewProbe):
        def __init__(self):
            super().__init__()
            self.status = self._status_type.GRANT_RECEIVED
            self.manual_pending = False

        def request_refresh(self, _config):
            self.manual_pending = True
            return SimpleNamespace(status="pending", manual_refresh_pending=True)

        def schedule_tick(self, _config):
            if not self.manual_pending:
                return SimpleNamespace(status="idle")
            self.manual_pending = False
            return SimpleNamespace(
                status="ready",
                refresh_succeeded=True,
                manual_refresh_pending=False,
                events_checked=7,
                active_variable_count=2,
                total_variable_count=3,
            )

    poly = FakePolyglot()
    bridge = RefreshBridge()
    probe = type(controller)(poly, oauth_bridge=bridge)
    probe.register_node()
    assert probe.typedConfigurationHandler(typed_payload())
    probe._config_done()
    probe._start_event()

    assert probe.refresh_now({"cmd": "REFRESH"}) is True
    assert probe.refresh_status == "pending"
    assert probe.getDriver("GV0") == 1
    assert probe.getDriver("GV1") == 0

    assert probe._periodic_schedule_handler() is True
    assert probe.refresh_status == "succeeded"
    assert probe.cache_status == "trusted"
    assert probe.getDriver("GV0") == 2
    assert probe.getDriver("GV1") == 1
    assert probe.getDriver("GV2") == 7
    assert probe.getDriver("GV3") == 2
    assert ("GV0", 2, True, False) in probe.driver_updates
    assert all(type(update[1]) is int for update in probe.driver_updates)


def test_failed_status_driver_publication_is_retried_without_false_success(controller):
    bridge = FakePeriodicPreviewProbe()
    probe = type(controller)(FakePolyglot(), oauth_bridge=bridge)
    probe.register_node()
    assert probe.typedConfigurationHandler(typed_payload())
    probe._config_done()
    probe._start_event()

    assert probe._requested_status_values["GV0"] == 0
    probe.fail_driver_update = True
    assert probe._set_refresh_status("succeeded") is False
    assert probe.refresh_status == "succeeded"
    assert probe.getDriver("GV0") == 0
    assert probe._requested_status_values["GV0"] == 0

    probe.fail_driver_update = False
    before_retry = len(probe.driver_updates)
    probe._periodic_schedule_handler()
    assert probe.getDriver("GV0") == 2
    assert probe._requested_status_values["GV0"] == 2
    assert probe.driver_updates[before_retry:] == [("GV0", 2, True, False)]

    after_success = list(probe.driver_updates)
    probe._periodic_schedule_handler()
    assert probe.driver_updates == after_success



def test_post_registration_short_poll_repairs_stale_disconnected_driver(controller):
    bridge = FakePeriodicPreviewProbe()
    bridge.status = bridge._status_type.GRANT_RECEIVED
    poly = FakePolyglot()
    probe = type(controller)(poly, oauth_bridge=bridge)
    assert probe.typedConfigurationHandler(typed_payload())
    probe._config_done()
    probe._start_event()
    probe.register_node()

    # Simulate eisy-ui not receiving the initial asynchronous ST=1 request.
    for item in probe.drivers:
        if item["driver"] == "ST":
            item["value"] = 0
    probe.driver_updates.clear()
    callbacks = {key: callback for key, callback, _ in poly.subscriptions}
    assert callbacks["POLL"]("shortPoll") is True
    assert ("ST", 1, True, True) in probe.driver_updates
    assert probe.getDriver("ST") == 1

    after_recovery = list(probe.driver_updates)
    assert callbacks["POLL"]("shortPoll") is True
    assert probe.driver_updates == after_recovery

    probe._stop_event()
    assert probe.getDriver("ST") == 0


def test_invalid_interval_shows_successful_google_fetch_and_no_trusted_schedule(controller):
    class InvalidIntervalBridge(FakePeriodicPreviewProbe):
        def __init__(self):
            super().__init__()
            self.status = self._status_type.GRANT_RECEIVED
            self.pending = False

        def request_refresh(self, _config):
            self.pending = True
            return SimpleNamespace(status="pending", manual_refresh_pending=True)

        def schedule_tick(self, _config):
            if self.pending:
                self.pending = False
                return SimpleNamespace(
                    status="invalid_interval",
                    refresh_succeeded=True,
                    manual_refresh_pending=False,
                    events_checked=0,
                )
            return SimpleNamespace(status="idle")

    poly = FakePolyglot()
    probe = type(controller)(poly, oauth_bridge=InvalidIntervalBridge())
    probe.register_node()
    assert probe.typedConfigurationHandler(typed_payload())
    probe._config_done()
    probe._start_event()
    assert probe.refresh_now() is True
    assert probe._periodic_schedule_handler() is False
    assert probe.refresh_status == "succeeded"
    assert probe.cache_status == "unavailable"
    assert probe.events_evaluated == 0
    assert probe.targets_desired_on == 0
    assert "invalid adjusted event interval" in poly.Notices["google_auth"]
    assert "refresh succeeded" in poly.Notices["calendar_refresh"]
    assert "Refresh Now failed" not in poly.Notices["calendar_refresh"]


def test_first_short_poll_forces_latest_complete_snapshot_after_registration(controller):
    class ChangingSchedule(FakePeriodicPreviewProbe):
        def __init__(self):
            super().__init__()
            self.status = self._status_type.GRANT_RECEIVED
            self.events = 1
            self.active = 0

        def schedule_tick(self, _config):
            return SimpleNamespace(
                status="ready",
                refresh_succeeded=None,
                events_checked=self.events,
                active_variable_count=self.active,
                total_variable_count=1,
            )

    poly = FakePolyglot()
    bridge = ChangingSchedule()
    probe = type(controller)(poly, oauth_bridge=bridge)
    assert probe.typedConfigurationHandler(typed_payload())
    probe._config_done()
    probe._start_event()
    probe.register_node()
    assert probe._forced_status_snapshot_pending is True

    # Treat every pre-poll SDK request as dropped by the asynchronous receiver.
    probe.driver_updates.clear()
    bridge.events = 7
    bridge.active = 1
    callbacks = {key: callback for key, callback, _ in poly.subscriptions}
    assert callbacks["POLL"]("shortPoll") is True

    forced = [update for update in probe.driver_updates if update[3] is True]
    assert forced == [
        ("ST", 1, True, True),
        ("GV0", 0, True, True),
        ("GV1", 1, True, True),
        ("GV2", 7, True, True),
        ("GV3", 1, True, True),
        ("GV4", 1, True, True),
    ]
    assert probe._forced_status_snapshot_pending is False

    after_recovery = list(probe.driver_updates)
    assert callbacks["POLL"]("shortPoll") is True
    assert probe.driver_updates == after_recovery


def test_forced_snapshot_retries_local_failure_on_later_short_poll(controller):
    bridge = FakePeriodicPreviewProbe()
    bridge.status = bridge._status_type.GRANT_RECEIVED
    poly = FakePolyglot()
    probe = type(controller)(poly, oauth_bridge=bridge)
    assert probe.typedConfigurationHandler(typed_payload())
    probe._config_done()
    probe._start_event()
    probe.register_node()
    callbacks = {key: callback for key, callback, _ in poly.subscriptions}

    probe.fail_driver_update = True
    assert callbacks["POLL"]("shortPoll") is True
    assert probe._forced_status_snapshot_pending is True

    probe.fail_driver_update = False
    probe.driver_updates.clear()
    assert callbacks["POLL"]("shortPoll") is True
    assert [update[0] for update in probe.driver_updates if update[3] is True] == [
        "ST",
        "GV0",
        "GV1",
        "GV2",
        "GV3",
        "GV4",
    ]
    assert probe._forced_status_snapshot_pending is False


def test_forced_snapshot_uses_completed_iox_probe_state(controller):
    class ReadyProbe:
        failure_category = None
        target_ids = frozenset({38})

        def configure_targets(self, target_ids):
            self.target_ids = frozenset(target_ids)
            return "pending"

        def start(self):
            return True

        def request(self):
            return "ready"

        def stop(self):
            return True

    bridge = FakePeriodicPreviewProbe()
    bridge.status = bridge._status_type.GRANT_RECEIVED
    poly = FakePolyglot()
    probe = type(controller)(
        poly, oauth_bridge=bridge, iox_read_only_probe=ReadyProbe()
    )
    assert probe.typedConfigurationHandler(typed_payload())
    probe._config_done()
    probe._start_event()
    probe.register_node()
    probe.driver_updates.clear()

    callbacks = {key: callback for key, callback, _ in poly.subscriptions}
    assert callbacks["POLL"]("shortPoll") is True
    assert ("GV4", 0, True, True) in probe.driver_updates
    assert probe.iox_status == "ready"


def test_refresh_failure_with_trusted_cache_reports_using_cache(controller):
    class CachedFailureBridge(FakePeriodicPreviewProbe):
        def __init__(self):
            super().__init__()
            self.status = self._status_type.GRANT_RECEIVED
            self.pending = False

        def request_refresh(self, _config):
            self.pending = True
            return SimpleNamespace(status="pending", manual_refresh_pending=True)

        def schedule_tick(self, _config):
            if not self.pending:
                return SimpleNamespace(status="idle")
            self.pending = False
            return SimpleNamespace(
                status="ready",
                refresh_succeeded=False,
                manual_refresh_pending=False,
                events_checked=4,
                active_variable_count=1,
                total_variable_count=1,
            )

    probe = type(controller)(FakePolyglot(), oauth_bridge=CachedFailureBridge())
    probe.register_node()
    assert probe.typedConfigurationHandler(typed_payload())
    probe._config_done()
    probe._start_event()

    assert probe.refresh_now() is True
    assert probe.getDriver("GV0") == 1
    assert probe._periodic_schedule_handler() is True
    assert probe.refresh_status == "failed_using_cache"
    assert probe.cache_status == "trusted"
    assert probe.getDriver("GV0") == 3
    assert probe.getDriver("GV1") == 1
    assert probe.getDriver("GV2") == 4
    assert probe.getDriver("GV3") == 1


def test_refresh_failure_without_trusted_evaluation_reports_failed(controller):
    class UntrustedFailureBridge(FakePeriodicPreviewProbe):
        def __init__(self):
            super().__init__()
            self.status = self._status_type.GRANT_RECEIVED
            self.pending = False

        def request_refresh(self, _config):
            self.pending = True
            return SimpleNamespace(status="pending", manual_refresh_pending=True)

        def schedule_tick(self, _config):
            if not self.pending:
                return SimpleNamespace(status="idle")
            self.pending = False
            return SimpleNamespace(
                status="api_error",
                refresh_succeeded=False,
                manual_refresh_pending=False,
            )

    probe = type(controller)(FakePolyglot(), oauth_bridge=UntrustedFailureBridge())
    probe.register_node()
    assert probe.typedConfigurationHandler(typed_payload())
    probe._config_done()
    probe._start_event()

    assert probe.refresh_now() is True
    assert probe._periodic_schedule_handler() is False
    assert probe.refresh_status == "failed"
    assert probe.cache_status == "unavailable"
    assert probe.getDriver("GV0") == 4
    assert probe.getDriver("GV1") == 0
    assert probe.getDriver("GV2") == 0
    assert probe.getDriver("GV3") == 0


def test_restart_recovery_reports_restored_trusted_cache_counts(controller):
    class RestoredCacheBridge(FakePeriodicPreviewProbe):
        def __init__(self):
            super().__init__()
            self.status = self._status_type.GRANT_RECEIVED

        def schedule_tick(self, _config):
            return SimpleNamespace(
                status="ready",
                refresh_succeeded=None,
                events_checked=5,
                active_variable_count=1,
                total_variable_count=1,
            )

    probe = type(controller)(FakePolyglot(), oauth_bridge=RestoredCacheBridge())
    probe.register_node()
    assert probe.getDriver("GV1") == 0
    assert probe.typedConfigurationHandler(typed_payload())
    probe._config_done()
    probe._start_event()

    assert probe.started is True
    assert probe.refresh_status == "idle"
    assert probe.cache_status == "trusted"
    assert probe.getDriver("GV0") == 0
    assert probe.getDriver("GV1") == 1
    assert probe.getDriver("GV2") == 5
    assert probe.getDriver("GV3") == 1


@pytest.mark.parametrize(
    ("timezone_name", "instant", "expected"),
    [
        (
            "America/New_York",
            datetime(2026, 11, 11, 23, 43, 11, tzinfo=timezone.utc),
            "Nov 11, 2026, 6:43:11 PM EST",
        ),
        (
            "America/New_York",
            datetime(2026, 7, 11, 22, 43, 11, tzinfo=timezone.utc),
            "Jul 11, 2026, 6:43:11 PM EDT",
        ),
        (
            "America/New_York",
            datetime(2026, 3, 8, 6, 59, 59, tzinfo=timezone.utc),
            "Mar 8, 2026, 1:59:59 AM EST",
        ),
        (
            "America/New_York",
            datetime(2026, 3, 8, 7, 0, 0, tzinfo=timezone.utc),
            "Mar 8, 2026, 3:00:00 AM EDT",
        ),
        (
            "America/New_York",
            datetime(2026, 11, 1, 5, 59, 59, tzinfo=timezone.utc),
            "Nov 1, 2026, 1:59:59 AM EDT",
        ),
        (
            "America/New_York",
            datetime(2026, 11, 1, 6, 0, 0, tzinfo=timezone.utc),
            "Nov 1, 2026, 1:00:00 AM EST",
        ),
        (
            "Asia/Tokyo",
            datetime(2026, 11, 11, 23, 43, 11, tzinfo=timezone.utc),
            "Nov 12, 2026, 8:43:11 AM JST",
        ),
    ],
)
def test_sync_notice_uses_configured_timezone_and_tzdb_dst_rules(
    controller, timezone_name, instant, expected
):
    assert controller.typedConfigurationHandler(
        typed_payload(timezone=timezone_name)
    )
    controller.last_successful_sync_at = instant

    controller._refresh_notice("Google Calendar refresh succeeded.")

    assert f"Last successful Google sync: {expected}." in (
        controller.poly.Notices["calendar_refresh"]
    )
    assert controller.last_successful_sync_at is instant


def test_sync_notice_without_a_successful_sync_reports_not_available(controller):
    controller._refresh_notice("Refresh Now requested.")

    assert controller.poly.Notices["calendar_refresh"] == (
        "Refresh Now requested. Last successful Google sync: not available."
    )


@pytest.mark.parametrize("configured_timezone", [None, "Private/Secret-Timezone"])
def test_sync_notice_falls_back_to_sanitized_utc_for_missing_or_invalid_state(
    controller, configured_timezone
):
    if configured_timezone is not None:
        controller.active_configuration = SimpleNamespace(
            calendar=SimpleNamespace(timezone=configured_timezone)
        )
    controller.last_successful_sync_at = datetime(
        2026, 11, 11, 23, 43, 11, tzinfo=timezone.utc
    )

    controller._refresh_notice("Google Calendar refresh succeeded.")

    notice = controller.poly.Notices["calendar_refresh"]
    assert "Last successful Google sync: Nov 11, 2026, 11:43:11 PM UTC." in notice
    assert "Private" not in notice


@pytest.mark.parametrize(
    "order",
    [
        ("typed", "config_done", "start", "oauth", "tokens"),
        ("oauth", "tokens", "start", "typed", "config_done"),
        ("start", "oauth", "typed", "tokens", "config_done"),
    ],
)
def test_periodic_preview_refreshes_once_when_lifecycle_becomes_ready(
    controller, order
):
    poly = FakePolyglot()
    bridge = FakePeriodicPreviewProbe()
    probe = type(controller)(poly, oauth_bridge=bridge)
    callbacks = {key: callback for key, callback, _ in poly.subscriptions}
    actions = {
        "typed": lambda: probe.typedConfigurationHandler(typed_payload()),
        "config_done": probe._config_done,
        "start": probe._start_event,
        "oauth": lambda: callbacks["CUSTOMNS"]("oauth", {"scope": "readonly"}),
        "tokens": lambda: callbacks["CUSTOMNS"](
            "oauthTokens", {"refresh_token": "PRIVATE"}
        ),
    }

    for name in order:
        actions[name]()

    assert probe.started is True
    assert bridge.refreshes == 1
    assert poly.poll_updates == [(60, None)]
    assert probe.cache_status == "trusted"
    assert "No IoX writes performed" in poly.Notices["google_auth"]
    assert "PRIVATE" not in str(poly.Notices)

    # Duplicate readiness callbacks and long polls cannot duplicate a refresh.
    assert callbacks["CUSTOMNS"]("oauthTokens", {"refresh_token": "PRIVATE"})
    assert callbacks["POLL"]("longPoll") is True
    assert callbacks["POLL"]("shortPoll") is True
    assert bridge.refreshes == 1
    assert probe.google_auth_status == "schedule_ready"


def test_failed_poll_request_does_not_claim_verified_minute_scheduling(
    controller
):
    class PollFailure(FakePolyglot):
        def setPoll(self, short=None, long=None):
            raise RuntimeError("PRIVATE POLL FAILURE")

    poly = PollFailure()
    bridge = FakePeriodicPreviewProbe()
    bridge.status = bridge._status_type.GRANT_RECEIVED
    probe = type(controller)(poly, oauth_bridge=bridge)
    assert probe.typedConfigurationHandler(typed_payload())
    probe._config_done()
    probe._start_event()

    assert probe.started is True
    assert bridge.refreshes == 1
    assert probe._minute_poll_requested is False
    assert probe.minute_poll_verified is False
    assert probe.iox_writes_allowed is False
    assert "writes remain disabled" in poly.Notices["schedule_poll"]
    assert "PRIVATE" not in str(poly.Notices)
    callbacks = {key: callback for key, callback, _ in poly.subscriptions}
    assert callbacks["POLL"]("shortPoll") is True
    assert bridge.refreshes == 1


def test_set_poll_is_only_a_request_until_short_poll_cadence_is_observed(
    controller
):
    now = [1000.0]
    poly = FakePolyglot()
    bridge = FakePeriodicPreviewProbe()
    bridge.status = bridge._status_type.GRANT_RECEIVED
    probe = type(controller)(poly, oauth_bridge=bridge, poll_clock=lambda: now[0])
    assert probe.typedConfigurationHandler(typed_payload())
    probe._config_done()
    probe._start_event()
    callbacks = {key: callback for key, callback, _ in poly.subscriptions}

    assert poly.poll_updates == [(60, None)]
    assert probe._minute_poll_requested is True
    assert probe.minute_poll_verified is False
    assert probe.iox_writes_allowed is False

    assert callbacks["POLL"]("shortPoll") is True
    assert probe.minute_poll_verified is False
    now[0] += 60
    assert callbacks["POLL"]("shortPoll") is True
    assert probe.minute_poll_verified is False
    assert probe.iox_writes_allowed is False
    now[0] += 60
    assert callbacks["POLL"]("shortPoll") is True
    assert probe.minute_poll_verified is True
    assert probe.iox_writes_allowed is False
    assert "schedule_poll" not in poly.Notices

    now[0] += 180
    assert callbacks["POLL"]("shortPoll") is True
    assert probe.minute_poll_verified is False
    assert probe.iox_writes_allowed is False
    assert "not been observed reliably" in poly.Notices["schedule_poll"]


def test_write_gate_opens_only_after_observed_cadence_and_closes_on_stop(controller):
    class Gate:
        enabled = False

        def update_runtime(
            self, *, started, minute_poll_verified, last_short_poll_at
        ):
            self.enabled = started and minute_poll_verified

    now = [1000.0]
    poly = FakePolyglot()
    bridge = FakePeriodicPreviewProbe()
    bridge.status = bridge._status_type.GRANT_RECEIVED
    gate = Gate()
    probe = type(controller)(
        poly, oauth_bridge=bridge, poll_clock=lambda: now[0], write_gate=gate
    )
    assert probe.typedConfigurationHandler(typed_payload())
    probe._config_done()
    probe._start_event()
    callback = {key: value for key, value, _ in poly.subscriptions}["POLL"]

    callback("shortPoll")
    now[0] += 60
    callback("shortPoll")
    assert probe.iox_writes_allowed is False
    now[0] += 60
    callback("shortPoll")
    assert probe.iox_writes_allowed is True

    probe._stop_event()
    assert probe.iox_writes_allowed is False


def test_iox_probe_is_nonblocking_sanitized_and_stopped_with_controller(controller):
    class Probe:
        def __init__(self):
            self.started = 0
            self.stopped = 0
            self.statuses = iter(("pending", "ready"))
            self.target_ids = frozenset({38})

        def start(self):
            self.started += 1

        def stop(self):
            self.stopped += 1

        def request(self):
            return next(self.statuses, "ready")

    iox_probe = Probe()
    poly = FakePolyglot()
    instance = type(controller)(poly, iox_read_only_probe=iox_probe)
    instance.register_node()
    assert instance.getDriver("GV4") == 3
    instance._config_done()

    assert instance._start_event() is True
    assert iox_probe.started == 1
    assert "in progress" in poly.Notices["iox_access"]

    assert instance._update_iox_probe_status() == "ready"
    assert "variable 38 is readable" in poly.Notices["iox_access"]
    assert instance.getDriver("GV4") == 0

    assert instance._stop_event() is True
    assert iox_probe.stopped == 1


@pytest.mark.parametrize(
    ("probe_status", "expected_driver"),
    [
        ("ready", 0),
        ("unauthorized", 1),
        ("inactive", 1),
        ("failed", 2),
        ("pending", 3),
        ("unexpected-private-value", 2),
    ],
)
def test_iox_communication_driver_uses_only_fixed_sanitized_states(
    controller, probe_status, expected_driver
):
    class Probe:
        target_ids = frozenset({38})

        def request(self):
            return probe_status

    instance = type(controller)(FakePolyglot(), iox_read_only_probe=Probe())
    instance.register_node()

    instance._update_iox_probe_status()

    assert instance.getDriver("GV4") == expected_driver
    assert isinstance(instance.getDriver("GV4"), int)


def test_iox_probe_failures_never_escape_sdk_callbacks(controller):
    class BrokenProbe:
        def start(self):
            raise RuntimeError("PRIVATE_CREDENTIAL")

        def stop(self):
            raise RuntimeError("PRIVATE_CREDENTIAL")

        def request(self):
            raise RuntimeError("PRIVATE_CREDENTIAL")

    poly = FakePolyglot()
    instance = type(controller)(poly, iox_read_only_probe=BrokenProbe())
    instance._config_done()
    assert instance._start_event() is True
    assert "writes remain blocked" in poly.Notices["iox_access"]
    assert "PRIVATE" not in repr(poly.Notices)
    assert instance._stop_event() is True


def test_iox_probe_notice_uses_only_fixed_tls_failure_category(controller):
    class TlsFailureProbe:
        failure_category = "tls-certificate-validation"

        def request(self):
            return "failed"

    poly = FakePolyglot()
    instance = type(controller)(poly, iox_read_only_probe=TlsFailureProbe())

    assert instance._update_iox_probe_status() == "failed"
    assert poly.Notices["iox_access"] == (
        "IoX configured-target read-only validation failed "
        "(TLS certificate validation failed); writes remain blocked."
    )


def test_iox_probe_notices_report_only_sanitized_configured_variable_ids(controller):
    class MultiReadyProbe:
        target_ids = frozenset({104, 38})
        failure_category = None

        def request(self):
            return "ready"

    poly = FakePolyglot()
    instance = type(controller)(poly, iox_read_only_probe=MultiReadyProbe())
    assert instance._update_iox_probe_status() == "ready"
    assert poly.Notices["iox_access"] == (
        "IoX type-2 variables 38, 104 are readable; "
        "IoX write gate is closed."
    )

    class FailedProbe:
        target_ids = frozenset({38, 104})
        failure_category = "response-validation"
        failed_variable_id = 104

        def request(self):
            return "failed"

    failed = type(controller)(poly, iox_read_only_probe=FailedProbe())
    assert failed._update_iox_probe_status() == "failed"
    assert poly.Notices["iox_access"] == (
        "IoX configured-target read-only validation failed for configured "
        "variable 104 (response validation failed); writes remain blocked."
    )


@pytest.mark.parametrize(
    ("target_ids", "gate_open", "expected"),
    (
        (
            frozenset({38}),
            False,
            "IoX type-2 variable 38 is readable; IoX write gate is closed.",
        ),
        (
            frozenset({38, 104}),
            True,
            "IoX type-2 variables 38, 104 are readable; IoX write gate is open.",
        ),
        (
            frozenset(range(1, 14)),
            False,
            "IoX type-2 variables 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12 "
            "(+1 more) are readable; IoX write gate is closed.",
        ),
    ),
)
def test_ready_iox_notice_reports_actual_gate_and_bounded_targets(
    controller, target_ids, gate_open, expected
):
    class Probe:
        failure_category = None

        def request(self):
            return "ready"

    class Gate:
        enabled = gate_open

    probe = Probe()
    probe.target_ids = target_ids
    instance = type(controller)(
        FakePolyglot(), iox_read_only_probe=probe, write_gate=Gate()
    )

    assert instance._update_iox_probe_status() == "ready"
    assert instance.poly.Notices["iox_access"] == expected


def test_short_poll_refreshes_gate_notice_after_marker_and_cadence_changes(controller):
    from iox_write_runtime import WriteActivationGate

    class Probe:
        failure_category = None

        def __init__(self):
            self.target_ids = frozenset({38, 104})
            self.authorized = True
            self.active = True

        @property
        def ready(self):
            return self.active and self.authorized

        def request(self):
            if not self.active:
                return "inactive"
            return "ready" if self.authorized else "unauthorized"

        def stop(self):
            self.active = False

    now = [1000.0]
    marker = [False]
    iox_probe = Probe()
    gate = WriteActivationGate(
        marker_check=lambda: marker[0],
        transport_ready=lambda: iox_probe.ready,
        configured_ids=lambda: iox_probe.target_ids,
        monotonic_clock=lambda: now[0],
    )
    instance = type(controller)(
        FakePolyglot(),
        poll_clock=lambda: now[0],
        write_gate=gate,
        iox_read_only_probe=iox_probe,
    )
    instance.started = True
    instance.stopped = False
    instance._update_write_gate()

    assert instance._update_iox_probe_status() == "ready"
    assert instance.poly.Notices["iox_access"].endswith(
        "IoX write gate is closed."
    )

    marker[0] = True
    for timestamp in (1000.0, 1060.0, 1120.0):
        now[0] = timestamp
        instance._observe_short_poll()
        instance._update_iox_probe_status()
    assert instance.iox_writes_allowed is True
    assert instance.poly.Notices["iox_access"].endswith(
        "IoX write gate is open."
    )

    marker[0] = False
    now[0] = 1180.0
    instance._observe_short_poll()
    instance._update_iox_probe_status()
    assert instance.iox_writes_allowed is False
    assert instance.poly.Notices["iox_access"].endswith(
        "IoX write gate is closed."
    )

    marker[0] = True
    now[0] = 1360.0
    instance._observe_short_poll()
    instance._update_iox_probe_status()
    assert instance.minute_poll_verified is False
    assert instance.iox_writes_allowed is False
    assert instance.poly.Notices["iox_access"].endswith(
        "IoX write gate is closed."
    )


def test_authorization_loss_and_stop_clear_stale_open_gate_notice(controller):
    class Probe:
        failure_category = None

        def __init__(self):
            self.target_ids = frozenset({38})
            self.authorized = True
            self.active = True

        def request(self):
            if not self.active:
                return "inactive"
            return "ready" if self.authorized else "unauthorized"

        def stop(self):
            self.active = False

    class Gate:
        def __init__(self):
            self.enabled = True

        def update_runtime(self, *, started, minute_poll_verified, last_short_poll_at):
            self.enabled = bool(started and minute_poll_verified)

    iox_probe = Probe()
    gate = Gate()
    instance = type(controller)(
        FakePolyglot(), write_gate=gate, iox_read_only_probe=iox_probe
    )
    instance.started = True
    instance.stopped = False
    instance._minute_poll_verified = True
    instance._last_short_poll_at = 1000.0
    assert instance._update_iox_probe_status() == "ready"
    assert instance.poly.Notices["iox_access"].endswith(
        "IoX write gate is open."
    )

    iox_probe.authorized = False
    gate.enabled = False
    assert instance._update_iox_probe_status() == "unauthorized"
    assert instance.poly.Notices["iox_access"] == (
        "IoX access is not authorized; configured state variables were not read or changed."
    )

    iox_probe.authorized = True
    gate.enabled = True
    assert instance._update_iox_probe_status() == "ready"
    assert instance._stop_event() is True
    assert gate.enabled is False
    assert instance.poly.Notices["iox_access"] == (
        "IoX configured-target access check is inactive."
    )


def test_configuration_probe_failure_replaces_stale_open_notice(controller):
    class Probe:
        target_ids = frozenset({38})
        failure_category = None

        def request(self):
            return "ready"

        def configure_targets(self, _target_ids):
            raise RuntimeError("PRIVATE CONFIGURATION FAILURE")

    class Gate:
        enabled = True

    instance = type(controller)(
        FakePolyglot(), iox_read_only_probe=Probe(), write_gate=Gate()
    )
    assert instance._update_iox_probe_status() == "ready"
    assert instance.poly.Notices["iox_access"].endswith(
        "IoX write gate is open."
    )

    assert instance.typedConfigurationHandler(typed_payload()) is False
    assert instance.poly.Notices["iox_access"] == (
        "IoX configured-target read-only validation failed; writes remain blocked."
    )
    assert "PRIVATE" not in str(instance.poly.Notices)


@pytest.mark.parametrize(
    ("write_status", "gate_open", "expected"),
    (
        ("written", False, "IoX state write and readback verified."),
        ("noop", True, "IoX state verified without a change."),
    ),
)
def test_schedule_write_outcome_notice_is_independent_of_probe_gate_notice(
    controller, write_status, gate_open, expected
):
    class Bridge:
        def schedule_tick(self, _config):
            return SimpleNamespace(
                status="ready",
                refresh_succeeded=None,
                events_checked=1,
                active_variable_count=1,
                total_variable_count=1,
                write_status=write_status,
            )

    class Gate:
        enabled = gate_open

    instance = type(controller)(
        FakePolyglot(), oauth_bridge=Bridge(), write_gate=Gate()
    )
    instance.started = True
    instance.config_done = True
    instance.configuration_status = "valid"
    instance.active_configuration = SimpleNamespace()

    assert instance._periodic_schedule_handler() is True
    assert expected in instance.poly.Notices["google_auth"]


def test_slow_refresh_does_not_block_duplicate_poll_or_stop(controller):
    from eisy_google_calendar.auth import REQUIRED_GOOGLE_SCOPES
    from oauth_event_preview import ReadOnlyOAuthEventPreview

    class TokenSdk(FakeSdkOAuth):
        def getAccessToken(self):
            self.calls.append(("ACCESS",))
            return "MOCK_ACCESS_TOKEN"

    started = Event()
    fetch_returned = Event()
    release = Event()
    fetch_calls = []
    active_fetches = [0]
    maximum_active = [0]

    def factory(get_token):
        def slow_fetch(calendar_id, params):
            assert get_token() == "MOCK_ACCESS_TOKEN"
            fetch_calls.append((calendar_id, dict(params)))
            active_fetches[0] += 1
            maximum_active[0] = max(maximum_active[0], active_fetches[0])
            started.set()
            try:
                if not release.wait(2):
                    raise RuntimeError("test refresh was not released")
                return {"items": []}
            finally:
                active_fetches[0] -= 1
                fetch_returned.set()

        return slow_fetch

    bridge = ReadOnlyOAuthEventPreview(
        TokenSdk(),
        safe_logging=lambda: True,
        fetch_events_factory=factory,
    )
    scopes = " ".join(sorted(REQUIRED_GOOGLE_SCOPES))
    assert bridge.custom_namespace("oauth", {"scope": scopes})
    assert bridge.custom_namespace("oauthTokens", {"refresh_token": "PRIVATE"})
    poly = FakePolyglot()
    probe = type(controller)(poly, oauth_bridge=bridge)
    callbacks = {key: callback for key, callback, _ in poly.subscriptions}

    try:
        assert probe.typedConfigurationHandler(typed_payload())
        probe._config_done()
        before_start = monotonic()
        probe._start_event()
        assert monotonic() - before_start < 0.5
        assert started.wait(1)

        before_poll = monotonic()
        assert callbacks["POLL"]("shortPoll") is True
        assert monotonic() - before_poll < 0.5
        assert len(fetch_calls) == 1

        notices_before_stop = dict(poly.Notices)
        before_stop = monotonic()
        assert probe._stop_event() is True
        assert monotonic() - before_stop < 0.5
        assert probe.started is False
        assert probe.drivers[0]["value"] == 0
        assert len(fetch_calls) == 1
        assert dict(poly.Notices) == notices_before_stop

        release.set()
        assert fetch_returned.wait(1)
        assert callbacks["POLL"]("shortPoll") is False
        assert dict(poly.Notices) == notices_before_stop

        # Restart waits behind the cancelled generation and starts one new
        # refresh without overlapping it or publishing the stale result.
        probe._config_done()
        probe._start_event()
        deadline = monotonic() + 1
        while probe.cache_status != "trusted" and monotonic() < deadline:
            callbacks["POLL"]("shortPoll")
            sleep(0.01)
        assert probe.cache_status == "trusted"
        assert len(fetch_calls) == 2
        assert maximum_active[0] == 1
    finally:
        release.set()


def test_preview_poll_would_be_on_counts_never_writes_iox(controller):
    poly = FakePolyglot()
    bridge = FakeEventPreviewProbe()
    probe = type(controller)(poly, oauth_bridge=bridge)
    topics = {key: callback for key, callback, _ in poly.subscriptions}
    assert "POLL" in topics
    assert topics["POLL"]() is False
    assert bridge.calls == 0
    assert probe.typedConfigurationHandler(typed_payload()) is True
    probe._config_done()
    probe._start_event()
    driver_updates = list(probe.driver_updates)
    assert topics["POLL"]() is True
    assert bridge.calls == 1
    assert bridge.got_config is probe.active_configuration
    assert probe.google_auth_status == "preview_ready"
    assert "8 events checked" in poly.Notices["google_auth"]
    assert "1 of 2 state variables would be ON" in poly.Notices["google_auth"]
    assert "No IoX writes performed" in poly.Notices["google_auth"]
    assert topics["POLL"]() is True
    assert bridge.calls == 1
    assert probe.driver_updates == driver_updates
    assert poly.external_calls == []


def test_event_preview_failure_is_not_a_trusted_empty_schedule(controller):
    poly = FakePolyglot()
    bridge = FakeEventPreviewProbe(status="api_error", events=999, on=1, total=1)
    probe = type(controller)(poly, oauth_bridge=bridge)
    topics = {key: callback for key, callback, _ in poly.subscriptions}
    probe._config_done()
    probe._start_event()
    assert topics["POLL"]() is False
    assert probe.google_auth_status == "preview_unavailable"
    assert "could not complete" in poly.Notices["google_auth"]
    assert "999" not in poly.Notices["google_auth"]
    assert probe.drivers[0]["value"] == 1
    assert poly.external_calls == []


def test_preview_invalid_counts_do_not_create_success_notice(controller):
    poly = FakePolyglot()
    bridge = FakeEventPreviewProbe(events=-1, on=5, total=2)
    probe = type(controller)(poly, oauth_bridge=bridge)
    topics = {key: callback for key, callback, _ in poly.subscriptions}
    probe._config_done()
    probe._start_event()
    assert topics["POLL"]() is False
    assert probe.google_auth_status == "preview_unavailable"
    assert "events checked" not in poly.Notices["google_auth"]


def test_event_preview_exception_redacts_private_google_data(controller):
    class ExplodingPreview(FakeEventPreviewProbe):
        def preview_once(self, _config):
            raise RuntimeError("PRIVATE_ACCESS_TOKEN AND PRIVATE_EVENT_TITLE")

    poly = FakePolyglot()
    probe = type(controller)(poly, oauth_bridge=ExplodingPreview())
    topics = {key: callback for key, callback, _ in poly.subscriptions}
    probe._config_done()
    probe._start_event()
    assert topics["POLL"]() is False
    assert "PRIVATE_" not in str(poly.Notices)
    assert probe.drivers[0]["value"] == 1
