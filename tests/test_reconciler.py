from datetime import datetime, timezone

import pytest

import eisy_google_calendar.reconciler as reconciler_module
from eisy_google_calendar.iox_variables import WriteStatus
from eisy_google_calendar.models import CalendarEvent, CalendarSnapshot, Rule, SnapshotStatus
from eisy_google_calendar.reconciler import (
    ReconciliationStatus,
    reconcile_desired_states,
    reconcile_snapshot,
)

UTC = timezone.utc
NOW = datetime(2025, 1, 1, 10, 30, tzinfo=UTC)
READ_ZERO = '<var id="10" type="2"><val>0</val></var>'
READ_ONE = '<var id="10" type="2"><val>1</val></var>'
SET_OK = '<RestResponse succeeded="true"><status>200</status></RestResponse>'


class FakeTransport:
    def __init__(self, values, set_bodies=None):
        self.values = dict(values)
        self.set_bodies = list(set_bodies or [])
        self.calls = []

    def get(self, path):
        self.calls.append(("get", path))
        variable_id = int(path.rsplit("/", 1)[-1])
        return f'<var id="{variable_id}" type="2"><val>{self.values[variable_id]}</val></var>'

    def set(self, path):
        self.calls.append(("set", path))
        parts = path.split("/")
        variable_id = int(parts[-2])
        value = int(parts[-1])
        self.values[variable_id] = value
        return self.set_bodies.pop(0) if self.set_bodies else SET_OK


def rule(target, title="Meeting", **kwargs):
    return Rule(f"rule-{target}-{title}", title, target, **kwargs)


def event(event_id="event", title="Meeting", start=NOW, end=None, **kwargs):
    return CalendarEvent(event_id, title, start, end or datetime(2025, 1, 1, 11, 30, tzinfo=UTC), **kwargs)


def snapshot(*events):
    return CalendarSnapshot(SnapshotStatus.VALID, tuple(events))


def test_valid_snapshot_changes_zero_to_one():
    transport = FakeTransport({10: 0})
    result = reconcile_snapshot(snapshot(event()), [rule(10)], NOW, transport)
    assert result.status is ReconciliationStatus.COMPLETE
    assert result.processed[10].status is WriteStatus.WRITTEN
    assert transport.values[10] == 1


def test_valid_snapshot_changes_one_to_zero():
    transport = FakeTransport({10: 1})
    result = reconcile_snapshot(snapshot(), [rule(10)], NOW, transport)
    assert result.status is ReconciliationStatus.COMPLETE
    assert result.processed[10].status is WriteStatus.WRITTEN
    assert transport.values[10] == 0


def test_already_correct_values_produce_only_noop():
    transport = FakeTransport({10: 1})
    result = reconcile_snapshot(snapshot(event()), [rule(10)], NOW, transport)
    assert result.status is ReconciliationStatus.COMPLETE
    assert result.processed[10].status is WriteStatus.NOOP
    assert [kind for kind, _ in transport.calls] == ["get"]


def test_multiple_variables_reconcile_in_ascending_id_order():
    transport = FakeTransport({30: 0, 10: 0, 20: 0})
    rules = [rule(30), rule(10), rule(20)]
    result = reconcile_snapshot(snapshot(event()), rules, NOW, transport)
    set_ids = [int(path.split("/")[-2]) for kind, path in transport.calls if kind == "set"]
    assert result.status is ReconciliationStatus.COMPLETE
    assert set_ids == [10, 20, 30]
    assert set(result.processed) == {10, 20, 30}


def test_overlapping_matching_events_hold_variable_at_one():
    transport = FakeTransport({10: 0})
    events = (event("one"), event("two", start=datetime(2025, 1, 1, 10, 15, tzinfo=UTC)))
    result = reconcile_snapshot(snapshot(*events), [rule(10)], NOW, transport)
    assert result.status is ReconciliationStatus.COMPLETE
    assert result.evaluation.active_matches[10] == ("one", "two")
    assert result.processed[10].requested_value == 1


@pytest.mark.parametrize("event_kwargs", [{"cancelled": True}, {"all_day": True}])
def test_cancelled_or_all_day_events_follow_existing_evaluator(event_kwargs):
    transport = FakeTransport({10: 0})
    result = reconcile_snapshot(snapshot(event(**event_kwargs)), [rule(10)], NOW, transport)
    assert result.status is ReconciliationStatus.COMPLETE
    assert result.evaluation.desired_states == {10: 0}
    assert result.processed[10].status is WriteStatus.NOOP


def test_valid_empty_snapshot_requests_zero_for_configured_variables():
    transport = FakeTransport({10: 1, 20: 1})
    result = reconcile_snapshot(snapshot(), [rule(20), rule(10)], NOW, transport)
    assert result.status is ReconciliationStatus.COMPLETE
    assert {key: value.requested_value for key, value in result.processed.items()} == {10: 0, 20: 0}


@pytest.mark.parametrize("status", [SnapshotStatus.FAILED, SnapshotStatus.UNAVAILABLE])
def test_failed_or_unavailable_snapshot_causes_zero_transport_calls(status):
    transport = FakeTransport({10: 1})
    result = reconcile_snapshot(CalendarSnapshot(status, error="calendar unavailable"), [rule(10)], NOW, transport)
    assert result.status is ReconciliationStatus.UNTRUSTED
    assert result.processed == {}
    assert transport.calls == []


def test_invalid_event_interval_causes_zero_transport_calls():
    bad = event(start=NOW, end=datetime(2025, 1, 1, 11, tzinfo=UTC), title="Meeting")
    transport = FakeTransport({10: 1})
    result = reconcile_snapshot(snapshot(bad), [rule(10, start_offset_minutes=30)], NOW, transport)
    assert result.status is ReconciliationStatus.UNTRUSTED
    assert transport.calls == []


def test_second_variable_failure_stops_before_third_and_reports_all_groups():
    transport = FakeTransport(
        {10: 0, 20: 0, 30: 0},
        set_bodies=[SET_OK, '<RestResponse succeeded="false"><status>400</status></RestResponse>'],
    )
    result = reconcile_snapshot(snapshot(event()), [rule(30), rule(10), rule(20)], NOW, transport)
    assert result.status is ReconciliationStatus.PARTIAL_FAILURE
    assert set(result.processed) == {10, 20}
    assert result.processed[10].status is WriteStatus.WRITTEN
    assert result.processed[20].status is WriteStatus.FAILED
    assert result.failed_variable_id == 20
    assert result.unattempted_variable_ids == (30,)
    assert 30 not in result.processed


def test_trusted_reconciliation_can_recover_after_earlier_failure():
    failing = FakeTransport({10: 0}, set_bodies=['<RestResponse succeeded="false"><status>500</status></RestResponse>'])
    first = reconcile_snapshot(snapshot(event()), [rule(10)], NOW, failing)
    assert first.status is ReconciliationStatus.PARTIAL_FAILURE

    recovering = FakeTransport({10: 0})
    second = reconcile_snapshot(snapshot(event()), [rule(10)], NOW, recovering)
    assert second.status is ReconciliationStatus.COMPLETE
    assert second.processed[10].status is WriteStatus.WRITTEN


def test_trusted_desired_keys_mismatch_returns_error_without_transport_calls(monkeypatch):
    from eisy_google_calendar.models import EvaluationResult
    monkeypatch.setattr(
        reconciler_module,
        "evaluate_snapshot",
        lambda *args: EvaluationResult({11: 0}, SnapshotStatus.VALID, True),
    )
    transport = FakeTransport({10: 0})
    result = reconcile_snapshot(snapshot(), [rule(10)], NOW, transport)
    assert result.status is ReconciliationStatus.ERROR
    assert transport.calls == []


def test_trusted_invalid_desired_value_returns_error_without_transport_calls(monkeypatch):
    from eisy_google_calendar.models import EvaluationResult
    monkeypatch.setattr(
        reconciler_module,
        "evaluate_snapshot",
        lambda *args: EvaluationResult({10: 2}, SnapshotStatus.VALID, True),
    )
    transport = FakeTransport({10: 0})
    result = reconcile_snapshot(snapshot(), [rule(10)], NOW, transport)
    assert result.status is ReconciliationStatus.ERROR
    assert transport.calls == []


def test_evaluator_exception_returns_error_without_transport_calls(monkeypatch):
    def explode(*args):
        raise RuntimeError("internal detail")

    monkeypatch.setattr(reconciler_module, "evaluate_snapshot", explode)
    transport = FakeTransport({10: 0})
    result = reconcile_snapshot(snapshot(), [rule(10)], NOW, transport)
    assert result.status is ReconciliationStatus.ERROR
    assert result.error == "evaluation failed: RuntimeError"
    assert transport.calls == []


def test_duplicate_targets_are_processed_once():
    transport = FakeTransport({10: 0})
    result = reconcile_snapshot(snapshot(event()), [rule(10, "Meeting"), rule(10, "Other")], NOW, transport)
    assert result.status is ReconciliationStatus.COMPLETE
    assert list(result.processed) == [10]
    assert [kind for kind, _ in transport.calls] == ["get", "set", "get"]


def test_unexpected_status_on_second_variable_returns_error_and_stops(monkeypatch):
    from eisy_google_calendar.models import EvaluationResult
    from eisy_google_calendar.iox_variables import WriteResult

    results = iter((WriteResult(WriteStatus.NOOP, 10, 1), WriteResult("mystery", 20, 1)))
    monkeypatch.setattr(reconciler_module, "write_state_variable", lambda *args: next(results))
    monkeypatch.setattr(
        reconciler_module,
        "evaluate_snapshot",
        lambda *args: EvaluationResult({10: 1, 20: 1, 30: 1}, SnapshotStatus.VALID, True),
    )
    transport = FakeTransport({10: 0, 20: 0, 30: 0})
    result = reconcile_snapshot(snapshot(event()), [rule(30), rule(10), rule(20)], NOW, transport)
    assert result.status is ReconciliationStatus.ERROR
    assert list(result.processed) == [10]
    assert result.failed_variable_id == 20
    assert result.unattempted_variable_ids == (30,)
    assert result.error == "unexpected write status for variable 20"


def test_invalid_write_result_type_returns_error_without_complete(monkeypatch):
    from eisy_google_calendar.models import EvaluationResult

    monkeypatch.setattr(reconciler_module, "write_state_variable", lambda *args: object())
    monkeypatch.setattr(
        reconciler_module,
        "evaluate_snapshot",
        lambda *args: EvaluationResult({10: 1}, SnapshotStatus.VALID, True),
    )
    transport = FakeTransport({10: 0})
    result = reconcile_snapshot(snapshot(event()), [rule(10)], NOW, transport)
    assert result.status is ReconciliationStatus.ERROR
    assert result.failed_variable_id == 10
    assert result.error == "invalid write result for variable 10"
    assert result.processed == {}


def test_same_transport_can_recover_after_ambiguous_set_response_failure():
    transport = FakeTransport({10: 0}, set_bodies=['<RestResponse succeeded="true"><status>ambiguous</status></RestResponse>'])
    first = reconcile_snapshot(snapshot(event()), [rule(10)], NOW, transport)
    assert first.status is ReconciliationStatus.PARTIAL_FAILURE
    assert first.processed[10].status is WriteStatus.FAILED

    second = reconcile_snapshot(snapshot(event()), [rule(10)], NOW, transport)
    assert second.status is ReconciliationStatus.COMPLETE
    assert second.processed[10].status is WriteStatus.NOOP


def test_trusted_state_entry_point_enforces_explicit_allowlist():
    transport = FakeTransport({38: 0, 39: 0})
    blocked = reconcile_desired_states(
        {38: 1, 39: 1}, transport, allowed_variable_ids=frozenset({38})
    )
    assert blocked.status is ReconciliationStatus.ERROR
    assert transport.calls == []

    allowed = reconcile_desired_states(
        {38: 1}, transport, allowed_variable_ids=frozenset({38})
    )
    assert allowed.status is ReconciliationStatus.COMPLETE
    assert transport.values[38] == 1
