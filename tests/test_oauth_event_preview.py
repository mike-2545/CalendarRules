"""Offline tests for event preview; no Google access and no IoX writes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
import sys

import pytest

# The live-preview adapter lives in the separately packaged plugin root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from eisy_google_calendar.auth import REQUIRED_GOOGLE_SCOPES
from eisy_google_calendar.ui_configuration import load_typed_configuration
from eisy_google_calendar.snapshot_store import FileSnapshotStorage
from iox_write_runtime import (
    TrustedStateWriter,
    WriteActivationGate,
)
from oauth_event_preview import ReadOnlyOAuthEventPreview


NOW = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)
SCOPES = " ".join(sorted(REQUIRED_GOOGLE_SCOPES))


class FakeSdkOAuth:
    def __init__(self, token="MOCK_ACCESS_TOKEN", token_exception=None):
        self.calls = []
        self.token = token
        self.token_exception = token_exception

    def customNsHandler(self, key, data):
        self.calls.append(("namespace", key))

    def oauthHandler(self, data):
        self.calls.append(("oauth",))

    def getAccessToken(self):
        self.calls.append(("access",))
        if self.token_exception is not None:
            raise self.token_exception
        return self.token


def configuration(*calendars):
    return load_typed_configuration({
        "timezone": "America/New_York",
        "refresh_hours": "2",
        "calendars": [
            {"calendar_id": name} for name in calendars or ("secret-calendar",)
        ],
        "rules": [
            {
                "name": "Workshop",
                "title_contains": "Workshop",
                "state_variable_id": "101",
                "case_sensitive": "false",
                "start_offset_minutes": "-5",
                "end_offset_minutes": "0",
                "enabled": "true",
            },
            {
                "name": "Office",
                "title_contains": "Office",
                "state_variable_id": "202",
                "case_sensitive": "false",
                "start_offset_minutes": "0",
                "end_offset_minutes": "0",
                "enabled": "true",
            },
        ],
    })



def variable_38_configuration():
    return load_typed_configuration({
        "timezone": "America/New_York",
        "refresh_hours": "2",
        "calendars": [{"calendar_id": "secret-calendar"}],
        "rules": [{
            "name": "Workshop",
            "title_contains": "Workshop",
            "state_variable_id": "38",
            "case_sensitive": "false",
            "start_offset_minutes": "0",
            "end_offset_minutes": "0",
            "enabled": "true",
        }],
    })


def timed(identifier, title, *, status="confirmed"):
    return {
        "id": identifier,
        "summary": title,
        "status": status,
        "start": {"dateTime": "2025-01-01T12:00:00Z"},
        "end": {"dateTime": "2025-01-01T13:00:00Z"},
    }


def configure_auth(probe, scope=SCOPES):
    assert probe.custom_namespace("oauth", {
        "scope": scope, "client_secret": "PRIVATE_CLIENT_SECRET",
    })
    assert probe.custom_namespace("oauthTokens", {
        "refresh_token": "PRIVATE_REFRESH_TOKEN",
        "access_token": "PRIVATE_ACCESS_TOKEN",
    })


def fake_factory(pages, calls):
    def make(get_token):
        def fetch(calendar_id, params):
            assert get_token() == "MOCK_ACCESS_TOKEN"
            calls.append((calendar_id, dict(params)))
            page = pages[len(calls) - 1]
            if isinstance(page, BaseException):
                raise page
            return page
        return fetch
    return make


def test_complete_valid_events_map_to_aggregate_would_be_variable_states():
    calls = []
    sdk = FakeSdkOAuth()
    pages = [
        {
            "items": [
                timed("PRIVATE_EVENT_ID", "Private Workshop title"),
                timed("PRIVATE_CANCELLED", "Workshop", status="cancelled"),
                {"id": "PRIVATE_ALLDAY", "summary": "Office", "status": "confirmed",
                 "start": {"date": "2025-01-01"}, "end": {"date": "2025-01-02"}},
            ]
        }
    ]
    probe = ReadOnlyOAuthEventPreview(
        sdk, safe_logging=lambda: True, clock=lambda: NOW,
        fetch_events_factory=fake_factory(pages, calls),
    )
    configure_auth(probe)
    result = probe.preview_once(configuration())
    assert result.status == "ready"
    assert result.events_checked == 3
    assert result.active_variable_count == 1
    assert result.total_variable_count == 2
    assert "PRIVATE" not in repr(result)
    assert len(calls) == 1
    assert calls[0][0] == "secret-calendar"
    assert ("access",) in sdk.calls
    assert probe.preview_once(configuration()).status == "already_attempted"
    assert len(calls) == 1


def test_empty_valid_calendar_is_trusted_but_all_variables_would_be_off():
    calls = []
    probe = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(), safe_logging=lambda: True, clock=lambda: NOW,
        fetch_events_factory=fake_factory([{"items": []}], calls),
    )
    configure_auth(probe)
    result = probe.preview_once(configuration())
    assert result.status == "ready"
    assert result.events_checked == 0
    assert result.active_variable_count == 0
    assert result.total_variable_count == 2


def test_no_valid_rule_configuration_never_requests_token_or_google():
    sdk = FakeSdkOAuth()
    calls = []
    probe = ReadOnlyOAuthEventPreview(
        sdk, safe_logging=lambda: True, clock=lambda: NOW,
        fetch_events_factory=fake_factory([], calls),
    )
    configure_auth(probe)
    assert probe.preview_once(None).status == "configuration_required"
    assert ("access",) not in sdk.calls
    assert calls == []


def test_missing_scope_prevents_access_to_google_events():
    sdk = FakeSdkOAuth()
    calls = []
    probe = ReadOnlyOAuthEventPreview(
        sdk, safe_logging=lambda: True, clock=lambda: NOW,
        fetch_events_factory=fake_factory([], calls),
    )
    configure_auth(
        probe, "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
    )
    assert probe.preview_once(configuration()).status == "missing_scopes"
    assert ("access",) not in sdk.calls
    assert not calls


def test_no_saved_grant_refuses_preview():
    sdk = FakeSdkOAuth()
    calls = []
    probe = ReadOnlyOAuthEventPreview(
        sdk, safe_logging=lambda: True, clock=lambda: NOW,
        fetch_events_factory=fake_factory([], calls),
    )
    probe.custom_namespace("oauth", {"scope": SCOPES})
    assert probe.preview_once(configuration()).status == "authentication_required"
    assert ("access",) not in sdk.calls
    assert calls == []


def test_debug_log_level_refuses_saving_grant_or_reading_google():
    sdk = FakeSdkOAuth()
    calls = []
    active = [False]
    probe = ReadOnlyOAuthEventPreview(
        sdk, safe_logging=lambda: active[0], clock=lambda: NOW,
        fetch_events_factory=fake_factory([], calls),
    )
    assert not probe.custom_namespace("oauth", {"scope": SCOPES})
    assert not probe.authorization_event({"access_token": "PRIVATE_ACCESS_TOKEN"})
    assert not probe.custom_namespace(
        "oauthTokens", {"refresh_token": "PRIVATE_REFRESH_TOKEN"}
    )
    assert probe.preview_once(configuration()).status == "blocked_logging"
    assert sdk.calls == []
    assert calls == []


def test_log_level_change_after_auth_blocks_each_token_lookup():
    sdk = FakeSdkOAuth()
    active = [True]
    calls = []
    def factory(get_token):
        def fake_fetch(_cal, _params):
            active[0] = False
            get_token()  # Must reject DEBUG before exposing a new token.
            calls.append("unexpected")
            return {"items": []}
        return fake_fetch

    probe = ReadOnlyOAuthEventPreview(
        sdk, safe_logging=lambda: active[0], clock=lambda: NOW,
        fetch_events_factory=factory,
    )
    configure_auth(probe)
    result = probe.preview_once(configuration())
    assert result.status == "api_error"
    assert calls == []
    assert ("access",) not in sdk.calls


def test_partial_second_calendar_failure_discards_first_calendar_data():
    sdk = FakeSdkOAuth()
    calls = []
    pages = [
        {"items": [timed("PRIVATE_GOOD", "Workshop")]},
        RuntimeError("PRIVATE_SECOND_CALENDAR_FAILURE"),
    ]
    probe = ReadOnlyOAuthEventPreview(
        sdk, safe_logging=lambda: True, clock=lambda: NOW,
        fetch_events_factory=fake_factory(pages, calls),
    )
    configure_auth(probe)
    result = probe.preview_once(configuration("first", "second"))
    assert result.status == "api_error"
    assert result.events_checked == 0
    assert result.active_variable_count == 0
    assert "PRIVATE" not in repr(result)
    assert len(calls) == 2


def test_malformed_event_cannot_produce_an_empty_success():
    calls = []
    probe = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(), safe_logging=lambda: True, clock=lambda: NOW,
        fetch_events_factory=fake_factory([
            {"items": [{"id": "PRIVATE_BAD_ID", "summary": "Workshop"}]}
        ], calls),
    )
    configure_auth(probe)
    assert probe.preview_once(configuration()).status == "api_error"
    assert len(calls) == 1


def test_token_provider_errors_do_not_leak():
    calls = []
    probe = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(token_exception=RuntimeError("PRIVATE_TOKEN_BODY")),
        safe_logging=lambda: True, clock=lambda: NOW,
        fetch_events_factory=fake_factory([{"items": []}], calls),
    )
    configure_auth(probe)
    result = probe.preview_once(configuration())
    assert result.status == "api_error"
    assert "PRIVATE" not in repr(result)
    assert calls == []


def test_periodic_schedule_refreshes_immediately_then_evaluates_cached_each_minute():
    now = [NOW]
    calls = []
    probe = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(),
        safe_logging=lambda: True,
        clock=lambda: now[0],
        fetch_events_factory=fake_factory(
            [
                {"items": [timed("PRIVATE_FIRST", "Workshop")]},
                {"items": [timed("PRIVATE_SECOND", "Office")]},
            ],
            calls,
        ),
    )
    configure_auth(probe)

    first = probe.schedule_tick(configuration())
    assert first.status == "ready"
    assert first.refresh_attempted is True
    assert first.refresh_succeeded is True
    assert first.evaluated is True
    assert first.activity is not None and first.activity.trusted
    assert first.write_status == "disabled"
    assert len(calls) == 1

    duplicate = probe.schedule_tick(configuration())
    assert duplicate.status == "idle"
    assert len(calls) == 1

    now[0] += timedelta(minutes=1)
    cached = probe.schedule_tick(configuration())
    assert cached.status == "ready"
    assert cached.refresh_attempted is False
    assert cached.refresh_succeeded is None
    assert cached.evaluated is True
    assert cached.activity is not None and cached.activity.trusted
    assert len(calls) == 1

    now[0] += timedelta(hours=1, minutes=59)
    periodic = probe.schedule_tick(configuration())
    assert periodic.status == "ready"
    assert periodic.refresh_attempted is True
    assert periodic.refresh_succeeded is True
    assert len(calls) == 2


def test_periodic_google_failure_retains_trusted_on_state_without_private_data():
    now = [NOW]
    calls = []
    long_event = timed("PRIVATE_EVENT", "Workshop")
    long_event["end"] = {"dateTime": "2025-01-01T16:00:00Z"}
    probe = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(),
        safe_logging=lambda: True,
        clock=lambda: now[0],
        fetch_events_factory=fake_factory(
            [
                {"items": [long_event]},
                RuntimeError("PRIVATE_GOOGLE_RESPONSE"),
            ],
            calls,
        ),
    )
    configure_auth(probe)
    assert probe.schedule_tick(configuration()).active_variable_count == 1

    now[0] += timedelta(hours=2)
    failed = probe.schedule_tick(configuration())
    assert failed.status == "ready"
    assert failed.refresh_attempted is True
    assert failed.refresh_succeeded is False
    assert failed.active_variable_count == 1
    assert failed.total_variable_count == 2
    assert failed.activity is not None and failed.activity.trusted
    assert failed.activity.cached is True
    assert "PRIVATE" not in repr(failed)


def test_trusted_cached_states_continue_across_failed_refresh_until_recovery():
    now = [NOW]
    calls = []
    consumed = []

    class Outcome:
        status = "noop"

    long_event = timed("PRIVATE_EVENT", "Workshop")
    long_event["end"] = {"dateTime": "2025-01-01T13:00:00Z"}
    probe = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(),
        safe_logging=lambda: True,
        clock=lambda: now[0],
        fetch_events_factory=fake_factory(
            [
                {"items": [long_event]},
                RuntimeError("PRIVATE_FAILURE"),
                {"items": []},
            ],
            calls,
        ),
        trusted_state_consumer=lambda states: consumed.append(dict(states)) or Outcome(),
    )
    configure_auth(probe)

    first = probe.schedule_tick(configuration())
    assert first.write_status == "noop"
    assert consumed == [{101: 1, 202: 0}]

    now[0] += timedelta(hours=2)
    failed = probe.schedule_tick(configuration())
    assert failed.refresh_succeeded is False
    assert failed.write_status == "noop"
    assert consumed == [{101: 1, 202: 0}, {101: 0, 202: 0}]

    # The complete saved schedule remains authoritative while its independent
    # age and coverage gates pass, even though Google is still unreachable.
    now[0] += timedelta(minutes=1)
    cached_off = probe.schedule_tick(configuration())
    assert cached_off.refresh_attempted is False
    assert cached_off.active_variable_count == 0
    assert cached_off.write_status == "noop"
    assert consumed == [
        {101: 1, 202: 0},
        {101: 0, 202: 0},
        {101: 0, 202: 0},
    ]

    # The scheduled retry completes a new full snapshot and only then
    # re-enables reconciliation.
    now[0] += timedelta(minutes=14)
    recovered = probe.schedule_tick(configuration())
    assert recovered.refresh_succeeded is True
    assert recovered.write_status == "noop"
    assert consumed[-1] == {101: 0, 202: 0}


def test_saved_32_day_schedule_drives_off_on_overlap_and_off_while_offline(tmp_path):
    now = [NOW]
    calls = []
    consumed = []

    class Outcome:
        status = "noop"

    first = timed("PRIVATE_FIRST", "Workshop")
    first["start"] = {"dateTime": "2025-01-01T13:00:00Z"}
    first["end"] = {"dateTime": "2025-01-01T14:00:00Z"}
    overlap = timed("PRIVATE_OVERLAP", "Workshop")
    overlap["start"] = {"dateTime": "2025-01-01T13:30:00Z"}
    overlap["end"] = {"dateTime": "2025-01-01T15:00:00Z"}
    storage = FileSnapshotStorage(
        tmp_path / ".calendar-cache" / "schedule.calendar-cache.json"
    )
    probe = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(),
        safe_logging=lambda: True,
        clock=lambda: now[0],
        fetch_events_factory=fake_factory(
            [
                {"items": [first, overlap]},
                RuntimeError("PRIVATE_OFFLINE"),
                RuntimeError("PRIVATE_STILL_OFFLINE"),
            ],
            calls,
        ),
        trusted_state_consumer=lambda states: consumed.append(dict(states)) or Outcome(),
        snapshot_storage=storage,
    )
    configure_auth(probe)

    assert probe.schedule_tick(configuration()).active_variable_count == 0
    assert storage.path.is_file()
    snapshot = storage.load()
    assert snapshot.coverage_end - snapshot.fetched_at == timedelta(days=32)

    now[0] += timedelta(hours=1)
    assert probe.schedule_tick(configuration()).active_variable_count == 1
    now[0] += timedelta(hours=1)
    failed = probe.schedule_tick(configuration())
    assert failed.refresh_succeeded is False
    assert failed.active_variable_count == 1
    now[0] += timedelta(hours=1)
    assert probe.schedule_tick(configuration()).active_variable_count == 0

    assert [states[101] for states in consumed] == [0, 1, 1, 0]
    assert all(states[202] == 0 for states in consumed)
    assert len(calls) == 3


def test_real_reconciler_drives_configured_variable_on_and_off_from_cache_during_outage(tmp_path):
    class Variable38Transport:
        def __init__(self):
            self.value = 0
            self.calls = []

        def get(self, path):
            self.calls.append(("get", path))
            return f'<var id="38" type="2"><val>{self.value}</val></var>'

        def set(self, path):
            self.calls.append(("set", path))
            self.value = int(path.rsplit("/", 1)[-1])
            return '<RestResponse succeeded="true"><status>200</status></RestResponse>'

    monotonic_now = [1000.0]
    gate = WriteActivationGate(
        marker_check=lambda: True,
        transport_ready=lambda: True,
        configured_ids=lambda: frozenset({38}),
        monotonic_clock=lambda: monotonic_now[0],
    )
    gate.update_runtime(
        started=True,
        minute_poll_verified=True,
        last_short_poll_at=monotonic_now[0],
    )
    transport = Variable38Transport()
    writer = TrustedStateWriter(transport, gate, safe_logging=lambda: True)

    event = timed("PRIVATE_OUTAGE", "Workshop")
    event["start"] = {"dateTime": "2025-01-01T13:00:00Z"}
    event["end"] = {"dateTime": "2025-01-01T14:00:00Z"}
    calls = []
    now = [NOW]
    preview = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(),
        safe_logging=lambda: True,
        clock=lambda: now[0],
        fetch_events_factory=fake_factory(
            [{"items": [event]}, RuntimeError("PRIVATE_GOOGLE_OUTAGE")],
            calls,
        ),
        trusted_state_consumer=writer,
        snapshot_storage=FileSnapshotStorage(tmp_path / "schedule.calendar-cache.json"),
    )
    configure_auth(preview)

    assert preview.schedule_tick(variable_38_configuration()).write_status == "noop"
    now[0] += timedelta(hours=1)
    monotonic_now[0] += 60
    gate.update_runtime(
        started=True,
        minute_poll_verified=True,
        last_short_poll_at=monotonic_now[0],
    )
    assert preview.schedule_tick(variable_38_configuration()).write_status == "written"
    assert transport.value == 1

    now[0] += timedelta(hours=1)
    monotonic_now[0] += 60
    gate.update_runtime(
        started=True,
        minute_poll_verified=True,
        last_short_poll_at=monotonic_now[0],
    )
    failed = preview.schedule_tick(variable_38_configuration())
    assert failed.refresh_attempted is True
    assert failed.refresh_succeeded is False
    assert failed.write_status == "written"
    assert transport.value == 0
    assert len(calls) == 2
    assert [path for operation, path in transport.calls if operation == "set"] == [
        "/rest/vars/set/2/38/1",
        "/rest/vars/set/2/38/0",
    ]


def test_reboot_restores_snapshot_and_transitions_while_google_is_offline(tmp_path):
    storage = FileSnapshotStorage(
        tmp_path / ".calendar-cache" / "schedule.calendar-cache.json"
    )
    event = timed("PRIVATE_REBOOT", "Workshop")
    event["start"] = {"dateTime": "2025-01-01T13:00:00Z"}
    event["end"] = {"dateTime": "2025-01-01T14:00:00Z"}

    online = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(),
        safe_logging=lambda: True,
        clock=lambda: NOW,
        fetch_events_factory=fake_factory([{"items": [event]}], []),
        snapshot_storage=storage,
    )
    configure_auth(online)
    assert online.schedule_tick(configuration()).status == "ready"

    now = [NOW + timedelta(hours=1, minutes=30)]
    calls = []
    consumed = []

    class Outcome:
        status = "noop"

    restarted = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(),
        safe_logging=lambda: True,
        clock=lambda: now[0],
        fetch_events_factory=fake_factory(
            [RuntimeError("PRIVATE_NO_INTERNET"), RuntimeError("PRIVATE_NO_INTERNET")],
            calls,
        ),
        trusted_state_consumer=lambda states: consumed.append(dict(states)) or Outcome(),
        snapshot_storage=storage,
    )
    configure_auth(restarted)

    restored = restarted.schedule_tick(configuration())
    assert restored.refresh_succeeded is False
    assert restored.active_variable_count == 1
    now[0] += timedelta(minutes=30)
    expired = restarted.schedule_tick(configuration())
    assert expired.active_variable_count == 0
    assert [states[101] for states in consumed] == [1, 0]
    assert len(calls) == 2


def test_reboot_with_expired_saved_snapshot_withholds_all_decisions(tmp_path):
    storage = FileSnapshotStorage(tmp_path / "private" / "snapshot.calendar-cache.json")
    online = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(),
        safe_logging=lambda: True,
        clock=lambda: NOW,
        fetch_events_factory=fake_factory([{"items": []}], []),
        snapshot_storage=storage,
    )
    configure_auth(online)
    assert online.schedule_tick(configuration()).status == "ready"

    boundary_states = []
    boundary = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(),
        safe_logging=lambda: True,
        clock=lambda: NOW + timedelta(days=30),
        fetch_events_factory=fake_factory([RuntimeError("PRIVATE_OFFLINE")], []),
        trusted_state_consumer=lambda states: boundary_states.append(dict(states)),
        snapshot_storage=storage,
    )
    configure_auth(boundary)
    assert boundary.schedule_tick(configuration()).status == "ready"
    assert boundary_states == [{101: 0, 202: 0}]

    consumed = []
    restarted = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(),
        safe_logging=lambda: True,
        clock=lambda: NOW + timedelta(days=30, minutes=1),
        fetch_events_factory=fake_factory([RuntimeError("PRIVATE_OFFLINE")], []),
        trusted_state_consumer=lambda states: consumed.append(states),
        snapshot_storage=storage,
    )
    configure_auth(restarted)
    result = restarted.schedule_tick(configuration())
    assert result.status == "untrusted"
    assert consumed == []


def test_periodic_schedule_never_reads_token_before_all_readiness_guards_pass():
    sdk = FakeSdkOAuth()
    calls = []
    probe = ReadOnlyOAuthEventPreview(
        sdk,
        safe_logging=lambda: True,
        clock=lambda: NOW,
        fetch_events_factory=fake_factory([], calls),
    )

    assert probe.schedule_tick(configuration()).status == "missing_scopes"
    assert probe.custom_namespace("oauth", {"scope": SCOPES})
    assert probe.schedule_tick(configuration()).status == "authentication_required"
    assert ("access",) not in sdk.calls
    assert calls == []


def test_background_refresh_has_one_bounded_complete_snapshot_attempt():
    elapsed = [0.0]
    calls = []
    consumed = []

    def factory(get_token):
        def fetch(calendar_id, params):
            assert get_token() == "MOCK_ACCESS_TOKEN"
            calls.append((calendar_id, dict(params)))
            elapsed[0] = 31.0
            return {"items": [], "nextPageToken": "PRIVATE_NEXT_PAGE"}

        return fetch

    probe = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(),
        safe_logging=lambda: True,
        clock=lambda: NOW,
        fetch_events_factory=factory,
        monotonic_clock=lambda: elapsed[0],
        refresh_timeout_seconds=30,
        trusted_state_consumer=lambda states: consumed.append(states),
    )
    configure_auth(probe)

    queued = probe.request_schedule_tick(configuration())
    assert queued.status == "pending"
    outcome = probe._worker_future.result(timeout=1)
    assert outcome.status == "untrusted"
    assert outcome.refresh_attempted is True
    assert outcome.refresh_succeeded is False
    assert len(calls) == 1
    assert consumed == []
    assert "PRIVATE" not in repr(outcome)


def test_configuration_change_queues_one_serialized_successor():
    started = Event()
    release = Event()
    calls = []
    active = [0]
    maximum_active = [0]

    def factory(get_token):
        def fetch(calendar_id, _params):
            assert get_token() == "MOCK_ACCESS_TOKEN"
            calls.append(calendar_id)
            active[0] += 1
            maximum_active[0] = max(maximum_active[0], active[0])
            try:
                if len(calls) == 1:
                    started.set()
                    assert release.wait(2)
                return {"items": []}
            finally:
                active[0] -= 1

        return fetch

    probe = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(),
        safe_logging=lambda: True,
        clock=lambda: NOW,
        fetch_events_factory=factory,
    )
    configure_auth(probe)
    try:
        assert probe.request_schedule_tick(configuration("old-private")).status == "pending"
        assert started.wait(1)
        probe.invalidate_schedule()
        assert probe.request_schedule_tick(configuration("new-private")).status == "pending"
        successor = probe._worker_future
        release.set()
        assert successor.result(timeout=1).status == "ready"
        assert calls == ["old-private", "new-private"]
        assert maximum_active[0] == 1
    finally:
        release.set()


def test_refresh_now_duplicates_coalesce_to_one_serialized_follow_up():
    started = Event()
    release = Event()
    calls = []
    active = [0]
    maximum_active = [0]

    def factory(get_token):
        def fetch(calendar_id, _params):
            assert get_token() == "MOCK_ACCESS_TOKEN"
            calls.append(calendar_id)
            active[0] += 1
            maximum_active[0] = max(maximum_active[0], active[0])
            try:
                if len(calls) == 1:
                    started.set()
                    assert release.wait(2)
                return {"items": [timed("event", "Workshop")]}
            finally:
                active[0] -= 1

        return fetch

    probe = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(),
        safe_logging=lambda: True,
        clock=lambda: NOW,
        fetch_events_factory=factory,
    )
    configure_auth(probe)
    settings = configuration()
    try:
        first = probe.request_refresh(settings)
        assert first.status == "pending"
        assert first.manual_refresh_pending is True
        assert started.wait(1)

        for _ in range(5):
            duplicate = probe.request_refresh(settings)
            assert duplicate.status == "pending"
            assert duplicate.manual_refresh_pending is True
        assert len(calls) == 1

        release.set()
        probe._worker_future.result(timeout=1)
        harvested = probe.request_schedule_tick(settings)
        assert harvested.refresh_succeeded is True
        assert harvested.manual_refresh_pending is True
        successor = probe._worker_future
        follow_up = successor.result(timeout=1)
        assert follow_up.refresh_succeeded is True
        final = probe.request_schedule_tick(settings)
        assert final.manual_refresh_pending is False
        assert len(calls) == 2
        assert maximum_active[0] == 1
    finally:
        release.set()


def test_clock_or_factory_exceptions_are_sanitized():
    probe = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(), safe_logging=lambda: True,
        clock=lambda: (_ for _ in ()).throw(ValueError("PRIVATE_TIMESTAMP")),
    )
    configure_auth(probe)
    outcome = probe.preview_once(configuration())
    assert outcome.status == "api_error"
    assert "PRIVATE" not in repr(outcome)


def test_source_contains_no_iox_device_mutations():
    from pathlib import Path
    file = Path(__file__).resolve().parents[1] / "plugin" / "oauth_event_preview.py"
    source = file.read_text()
    for forbidden in ("write_state_variable(", "reconcile_snapshot(", "ISY(", "setDriver(", "requests.post("):
        assert forbidden not in source


def test_invalid_interval_is_local_evaluation_failure_not_google_fetch_failure():
    configured = load_typed_configuration({
        "timezone": "America/New_York",
        "refresh_hours": "2",
        "calendars": [{"calendar_id": "secret-calendar"}],
        "rules": [{
            "name": "Workshop",
            "title_contains": "Workshop",
            "state_variable_id": "38",
            "case_sensitive": "false",
            "start_offset_minutes": "120",
            "end_offset_minutes": "0",
            "enabled": "true",
        }],
    })
    written = []
    probe = ReadOnlyOAuthEventPreview(
        FakeSdkOAuth(),
        safe_logging=lambda: True,
        clock=lambda: NOW,
        fetch_events_factory=fake_factory(
            [{"items": [timed("PRIVATE_EVENT", "Workshop PRIVATE_TITLE")]}], [],
        ),
        trusted_state_consumer=lambda state: written.append(state),
    )
    configure_auth(probe)
    result = probe.schedule_tick(configured)
    assert result.status == "invalid_interval"
    assert result.refresh_succeeded is True
    assert result.evaluated is True
    assert written == []
    assert "PRIVATE" not in repr(result)
