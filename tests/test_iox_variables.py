import pytest

from eisy_google_calendar.iox_variables import (
    StateVariableError,
    WriteStatus,
    read_state_variable,
    write_state_variable,
)


READ_ONE = '<var id="10" type="2"><val>1</val><init>0</init><ts>20261108 11:00:00</ts></var>'
READ_ZERO = '<var id="10" type="2"><val>0</val><init>0</init><ts>20261108 11:00:00</ts></var>'
SET_OK = '<RestResponse succeeded="true"><status>200</status></RestResponse>'


class FakeTransport:
    def __init__(self, read_body=READ_ZERO, set_body=SET_OK, read_bodies=None):
        self.read_body = read_body
        self.set_body = set_body
        self.read_bodies = list(read_bodies or [])
        self.calls = []

    def get(self, path):
        self.calls.append(("get", path))
        if self.read_bodies:
            return self.read_bodies.pop(0)
        return self.read_body

    def set(self, path):
        self.calls.append(("set", path))
        return self.set_body


def test_read_state_variable_validates_type_id_and_integer_value():
    transport = FakeTransport(read_body=READ_ONE)
    assert read_state_variable(transport, 10) == 1
    assert transport.calls == [("get", "/rest/vars/get/2/10")]


@pytest.mark.parametrize("value", [0, -1, True, False, "10", 1.0])
def test_read_rejects_invalid_variable_ids(value):
    with pytest.raises(StateVariableError, match="positive integer"):
        read_state_variable(FakeTransport(), value)


@pytest.mark.parametrize(
    "body, message",
    [
        ("", "empty"),
        ("<var", "malformed"),
        ('<wrong id="10" type="2"><val>1</val></wrong>', "root"),
        ('<var id="11" type="2"><val>1</val></var>', "wrong variable ID"),
        ('<var id="10" type="1"><val>1</val></var>', "not type 2"),
        ('<var id="10" type="2"></var>', "missing val"),
        ('<var id="10" type="2"><val>0</val><val>1</val></var>', "exactly one val"),
        ('<var id="10" type="2"><val>yes</val></var>', "not an integer"),
        ('<var id="10" type="2"><val>2</val></var>', "must be 0 or 1"),
    ],
)
def test_read_fails_closed_for_malformed_or_wrong_xml(body, message):
    with pytest.raises(StateVariableError, match=message):
        read_state_variable(FakeTransport(read_body=body), 10)


def test_noop_reads_then_skips_set_when_value_matches():
    transport = FakeTransport(read_body=READ_ONE)
    result = write_state_variable(transport, 10, 1)
    assert result.status is WriteStatus.NOOP
    assert result.previous_value == 1
    assert result.verified_value == 1
    assert transport.calls == [("get", "/rest/vars/get/2/10")]


def test_changed_value_issues_exactly_one_set_then_reads_back():
    transport = FakeTransport(read_bodies=[READ_ZERO, READ_ONE])
    result = write_state_variable(transport, 10, 1)
    assert result.status is WriteStatus.WRITTEN
    assert result.previous_value == 0
    assert result.verified_value == 1
    assert transport.calls == [
        ("get", "/rest/vars/get/2/10"),
        ("set", "/rest/vars/set/2/10/1"),
        ("get", "/rest/vars/get/2/10"),
    ]


@pytest.mark.parametrize("value", [-1, 2, True, False, "1", 1.0])
def test_calendar_writes_restrict_desired_value_to_zero_or_one(value):
    transport = FakeTransport()
    result = write_state_variable(transport, 10, value)
    assert result.status is WriteStatus.FAILED
    assert "0 or 1" in result.error
    assert transport.calls == []


def test_successful_set_requires_success_xml_and_status():
    for body in (
        '<RestResponse succeeded="false"><status>400</status></RestResponse>',
        '<RestResponse succeeded="true"><status>500</status></RestResponse>',
        '<RestResponse succeeded="true"></RestResponse>',
        '<RestResponse succeeded="true"><status>not-public-diagnostic-text</status></RestResponse>',
        '<notRestResponse succeeded="true"><status>200</status></notRestResponse>',
    ):
        transport = FakeTransport(read_bodies=[READ_ZERO, READ_ONE], set_body=body)
        result = write_state_variable(transport, 10, 1)
        assert result.status is WriteStatus.FAILED


def test_readback_mismatch_fails_closed_after_one_set():
    transport = FakeTransport(read_bodies=[READ_ZERO, READ_ZERO])
    result = write_state_variable(transport, 10, 1)
    assert result.status is WriteStatus.FAILED
    assert "readback" in result.error
    assert [call[0] for call in transport.calls] == ["get", "set", "get"]


def test_transport_exceptions_fail_without_exposing_response_contents():
    class BrokenTransport(FakeTransport):
        def get(self, path):
            raise RuntimeError("credentials should not appear in diagnostics")

    result = write_state_variable(BrokenTransport(), 10, 1)
    assert result.status is WriteStatus.FAILED
    assert result.error == "state-variable read failed: RuntimeError"
    assert "credentials" not in result.error


def test_malformed_status_is_not_reflected_verbatim_in_errors():
    secret = "server-secret-status-text"
    transport = FakeTransport(
        read_bodies=[READ_ZERO],
        set_body=f'<RestResponse succeeded="true"><status>{secret}</status></RestResponse>',
    )
    result = write_state_variable(transport, 10, 1)
    assert result.status is WriteStatus.FAILED
    assert result.error == "state-variable set response has an invalid status"
    assert secret not in result.error
    assert [call[0] for call in transport.calls] == ["get", "set"]


def test_invalid_inputs_never_issue_a_set():
    for variable_id, value in ((0, 1), (-1, 1), (True, 1), (10, -1), (10, 2), (10, True)):
        transport = FakeTransport()
        result = write_state_variable(transport, variable_id, value)
        assert result.status is WriteStatus.FAILED
        assert transport.calls == []
