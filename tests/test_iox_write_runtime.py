"""Offline Stage 3 write-gate and transport regression tests."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from iox_write_runtime import (
    TrustedStateWriter,
    WriteActivationGate,
)


SET_OK = '<RestResponse succeeded="true"><status>200</status></RestResponse>'


class FakeTransport:
    def __init__(self, value=0, fail_set=False, fail_get=False, after_get=None):
        self.values = value if isinstance(value, dict) else {38: value}
        self.fail_set = fail_set
        self.fail_get = fail_get
        self.calls = []
        self.after_get = after_get

    def get(self, path):
        self.calls.append(("get", path))
        variable_id = int(path.rsplit("/", 1)[-1])
        if self.fail_get is True or (
            isinstance(self.fail_get, set) and variable_id in self.fail_get
        ):
            raise TimeoutError("bounded request expired")
        if variable_id not in self.values:
            raise TimeoutError("configured variable is unavailable")
        body = (
            f'<var id="{variable_id}" type="2">'
            f'<val>{self.values[variable_id]}</val></var>'
        )
        if self.after_get is not None:
            self.after_get()
        return body

    def set(self, path):
        self.calls.append(("set", path))
        _prefix, variable_id, value = path.rsplit("/", 2)
        if self.fail_set is True or (
            isinstance(self.fail_set, set) and int(variable_id) in self.fail_set
        ):
            raise TimeoutError("bounded request expired")
        self.values[int(variable_id)] = int(value)
        return SET_OK


def runtime(
    *,
    started=True,
    cadence=True,
    logging=True,
    value=0,
    fail_set=False,
    fail_get=False,
    marker=True,
    transport_ready=True,
    configured_ids=frozenset({38}),
):
    now = [1000.0]
    marker_state = [marker]
    transport_state = [transport_ready]
    transport = FakeTransport(value=value, fail_set=fail_set, fail_get=fail_get)
    gate = WriteActivationGate(
        marker_check=lambda: marker_state[0],
        transport_ready=lambda: transport_state[0],
        configured_ids=(
            configured_ids if callable(configured_ids) else lambda: configured_ids
        ),
        monotonic_clock=lambda: now[0],
    )
    gate.update_runtime(
        started=started,
        minute_poll_verified=cadence,
        last_short_poll_at=now[0] if cadence else None,
    )
    writer = TrustedStateWriter(
        transport, gate, safe_logging=lambda: logging
    )
    return transport, gate, writer, now, marker_state, transport_state


def test_default_closed_gates_issue_no_iox_commands():
    for kwargs in (
        {"started": False},
        {"cadence": False},
        {"logging": False},
        {"marker": False},
        {"transport_ready": False},
    ):
        transport, _gate, writer, *_ = runtime(**kwargs)
        assert writer({38: 1}).status == "blocked"
        assert transport.calls == []


def test_one_configured_type2_variable_can_be_written_and_read_back():
    transport, gate, writer, *_ = runtime()
    assert gate.enabled is True
    assert writer({38: 1}).status == "written"
    assert transport.calls == [
        ("get", "/rest/vars/get/2/38"),
        ("set", "/rest/vars/set/2/38/1"),
        ("get", "/rest/vars/get/2/38"),
    ]


def test_single_configured_target_is_not_restricted_to_variable_38():
    transport, _gate, writer, *_ = runtime(
        value={104: 0}, configured_ids=frozenset({104})
    )
    assert writer({104: 1}).status == "written"
    assert transport.calls == [
        ("get", "/rest/vars/get/2/104"),
        ("set", "/rest/vars/set/2/104/1"),
        ("get", "/rest/vars/get/2/104"),
    ]


def test_same_trusted_state_is_idempotent_and_skips_set():
    transport, _gate, writer, *_ = runtime(value=1)
    assert writer({38: 1}).status == "noop"
    assert transport.calls == [("get", "/rest/vars/get/2/38")]


def test_non_allowlisted_or_malformed_decisions_fail_closed_before_transport():
    transport, _gate, writer, *_ = runtime()
    assert writer({39: 1}).status == "blocked"
    assert writer({38: 2}).status == "failed"
    assert transport.calls == []


def test_multiple_configured_variables_are_reconciled_in_numeric_order():
    transport, _gate, writer, *_ = runtime(
        value={38: 0, 104: 1}, configured_ids=frozenset({38, 104})
    )
    assert writer({104: 0, 38: 1}).status == "written"
    assert transport.values == {38: 1, 104: 0}
    assert transport.calls == [
        ("get", "/rest/vars/get/2/38"),
        ("set", "/rest/vars/set/2/38/1"),
        ("get", "/rest/vars/get/2/38"),
        ("get", "/rest/vars/get/2/104"),
        ("set", "/rest/vars/set/2/104/0"),
        ("get", "/rest/vars/get/2/104"),
    ]


def test_partial_target_map_and_unconfigured_target_issue_no_commands():
    transport, _gate, writer, *_ = runtime(
        value={38: 0, 104: 0}, configured_ids=frozenset({38, 104})
    )
    assert writer({38: 1}).status == "blocked"
    assert writer({38: 1, 105: 1}).status == "blocked"
    assert transport.calls == []


def test_partial_iox_failure_stops_before_later_configured_variables():
    transport, _gate, writer, *_ = runtime(
        value={38: 0, 104: 0, 205: 0},
        configured_ids=frozenset({38, 104, 205}),
        fail_get={104},
    )
    assert writer({38: 1, 104: 1, 205: 1}).status == "failed"
    assert transport.values == {38: 1, 104: 0, 205: 0}
    assert ("get", "/rest/vars/get/2/205") not in transport.calls
    assert all("/205" not in path for _kind, path in transport.calls)


def test_target_set_change_between_read_and_set_closes_gate():
    configured = [frozenset({38})]
    transport, _gate, writer, *_ = runtime(
        configured_ids=lambda: configured[0]
    )
    transport.after_get = lambda: configured.__setitem__(0, frozenset({104}))
    assert writer({38: 1}).status == "failed"
    assert transport.calls == [("get", "/rest/vars/get/2/38")]
    assert transport.values[38] == 0


def test_bounded_transport_timeout_fails_before_set_and_can_recover():
    transport, _gate, writer, *_ = runtime(fail_get=True)
    assert writer({38: 1}).status == "failed"
    assert transport.calls == [("get", "/rest/vars/get/2/38")]
    transport.fail_get = False
    assert writer({38: 1}).status == "written"
    assert transport.values[38] == 1


def test_failed_set_recovers_on_next_trusted_tick():
    transport, _gate, writer, *_ = runtime(fail_set=True)
    assert writer({38: 1}).status == "failed"
    assert transport.values[38] == 0
    transport.fail_set = False
    assert writer({38: 1}).status == "written"
    assert transport.values[38] == 1


def test_removed_marker_prevents_all_subsequent_iox_commands():
    transport, _gate, writer, _now, marker_state, _transport_state = runtime(value=1)
    assert writer({38: 1}).status == "noop"
    transport.calls.clear()
    marker_state[0] = False
    assert writer({38: 0}).status == "blocked"
    assert transport.calls == []


def test_authorization_revocation_prevents_all_subsequent_iox_commands():
    transport, _gate, writer, _now, _marker_state, transport_state = runtime(
        value=1
    )
    assert writer({38: 1}).status == "noop"
    transport.calls.clear()
    transport_state[0] = False
    assert writer({38: 0}).status == "blocked"
    assert transport.calls == []


def test_expired_poll_cadence_without_callback_prevents_iox_commands():
    transport, gate, writer, now, *_ = runtime(value=1)
    assert gate.enabled is True
    now[0] += 91
    assert gate.enabled is False
    assert writer({38: 0}).status == "blocked"
    assert transport.calls == []


def test_gate_is_rechecked_between_read_and_set_during_stop():
    transport, gate, writer, now, *_ = runtime()
    transport.after_get = lambda: gate.update_runtime(
        started=False,
        minute_poll_verified=False,
        last_short_poll_at=now[0],
    )
    assert writer({38: 1}).status == "failed"
    assert transport.calls == [("get", "/rest/vars/get/2/38")]
    assert transport.values[38] == 0


def test_overlapping_trusted_evaluations_hold_on_until_final_expiration():
    transport, _gate, writer, *_ = runtime(value=0)
    assert writer({38: 1}).status == "written"
    assert writer({38: 1}).status == "noop"
    assert transport.values[38] == 1
    assert writer({38: 0}).status == "written"
    assert transport.values[38] == 0
    set_paths = [path for kind, path in transport.calls if kind == "set"]
    assert set_paths == ["/rest/vars/set/2/38/1", "/rest/vars/set/2/38/0"]
