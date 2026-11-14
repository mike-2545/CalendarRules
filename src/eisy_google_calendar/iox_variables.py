"""Offline IoX state-variable adapter.

This module deliberately contains no ``udi_interface``, HTTP, credentials, or
IoX connection code.  A caller supplies a transport that returns the XML body
for the requested REST path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from xml.etree import ElementTree


STATE_VARIABLE_TYPE = 2


class StateVariableError(ValueError):
    """Raised when an IoX variable request or response is invalid."""

    def __init__(self, message: str, *, category: str = "response-validation"):
        super().__init__(message)
        self.category = category


class WriteStatus(str, Enum):
    """Outcome of a calendar-driven state-variable write."""

    NOOP = "noop"
    WRITTEN = "written"
    FAILED = "failed"


@dataclass(frozen=True)
class WriteResult:
    """Explicit write outcome without retaining response contents."""

    status: WriteStatus
    variable_id: int
    requested_value: int
    previous_value: int | None = None
    verified_value: int | None = None
    error: str | None = None


class VariableTransport(Protocol):
    """Minimal injected transport contract used by this offline adapter."""

    def get(self, path: str) -> str:
        """Return the XML body for a GET-like IoX REST request."""

    def set(self, path: str) -> str:
        """Return the XML body for a set-like IoX REST request."""


def _validate_variable_id(variable_id: object) -> int:
    if isinstance(variable_id, bool) or not isinstance(variable_id, int) or variable_id <= 0:
        raise StateVariableError("variable_id must be a positive integer")
    return variable_id


def _validate_calendar_value(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1):
        raise StateVariableError("calendar state value must be 0 or 1")
    return value


def _parse_xml(body: object, operation: str) -> ElementTree.Element:
    if not isinstance(body, str) or not body.strip():
        raise StateVariableError(f"{operation} response is empty or not text")
    try:
        return ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise StateVariableError(f"{operation} response is malformed XML") from None


def _required_int(element: ElementTree.Element, child_name: str, operation: str) -> int:
    children = element.findall(child_name)
    if not children:
        raise StateVariableError(f"{operation} response is missing {child_name}")
    if len(children) != 1:
        raise StateVariableError(
            f"{operation} response must contain exactly one {child_name}"
        )
    child = children[0]
    if child.text is None or not child.text.strip():
        raise StateVariableError(f"{operation} response is missing {child_name}")
    text = child.text.strip()
    try:
        return int(text)
    except ValueError:
        raise StateVariableError(f"{operation} response {child_name} is not an integer") from None


def _parse_state_read(body: object, variable_id: int) -> int:
    root = _parse_xml(body, "state-variable read")
    if root.tag != "var":
        raise StateVariableError("state-variable read response root must be var")
    if root.get("id") != str(variable_id):
        raise StateVariableError("state-variable read response has the wrong variable ID")
    try:
        response_type = int(root.get("type", ""))
    except ValueError:
        raise StateVariableError("state-variable read response type is not an integer") from None
    if response_type != STATE_VARIABLE_TYPE:
        raise StateVariableError("state-variable read response is not type 2")
    value = _required_int(root, "val", "state-variable read")
    if value not in (0, 1):
        raise StateVariableError("state-variable read value must be 0 or 1")
    return value


def _parse_set_success(body: object) -> None:
    root = _parse_xml(body, "state-variable set")
    if root.tag != "RestResponse":
        raise StateVariableError("state-variable set response root must be RestResponse")
    succeeded = root.get("succeeded")
    if succeeded != "true":
        raise StateVariableError("state-variable set was not successful")
    status = root.findtext("status")
    if status is None or not status.strip():
        raise StateVariableError("state-variable set response is missing status")
    try:
        status_code = int(status.strip())
    except ValueError:
        raise StateVariableError("state-variable set response has an invalid status") from None
    if status_code < 200 or status_code >= 300:
        raise StateVariableError("state-variable set response has an unsuccessful status")


def read_state_variable(transport: VariableTransport, variable_id: int) -> int:
    """Read and validate one existing type-2 IoX state variable."""

    valid_id = _validate_variable_id(variable_id)
    path = f"/rest/vars/get/{STATE_VARIABLE_TYPE}/{valid_id}"
    try:
        body = transport.get(path)
    except Exception as exc:
        category = getattr(exc, "category", "transport")
        raise StateVariableError(
            f"state-variable read failed: {type(exc).__name__}",
            category=category,
        ) from None
    return _parse_state_read(body, valid_id)


def write_state_variable(
    transport: VariableTransport,
    variable_id: int,
    desired_value: int,
) -> WriteResult:
    """Compare, conditionally write, and verify a calendar state value.

    A failed operation returns ``FAILED`` rather than exposing response bodies
    or treating a partially completed write as trusted success.
    """

    try:
        valid_id = _validate_variable_id(variable_id)
        valid_value = _validate_calendar_value(desired_value)
        current = read_state_variable(transport, valid_id)
        if current == valid_value:
            return WriteResult(
                status=WriteStatus.NOOP,
                variable_id=valid_id,
                requested_value=valid_value,
                previous_value=current,
                verified_value=current,
            )
        path = f"/rest/vars/set/{STATE_VARIABLE_TYPE}/{valid_id}/{valid_value}"
        try:
            body = transport.set(path)
        except Exception as exc:
            raise StateVariableError(f"state-variable set failed: {type(exc).__name__}") from None
        _parse_set_success(body)
        verified = read_state_variable(transport, valid_id)
        if verified != valid_value:
            raise StateVariableError("state-variable readback does not match requested value")
        return WriteResult(
            status=WriteStatus.WRITTEN,
            variable_id=valid_id,
            requested_value=valid_value,
            previous_value=current,
            verified_value=verified,
        )
    except StateVariableError as exc:
        safe_id = variable_id if isinstance(variable_id, int) and not isinstance(variable_id, bool) else -1
        safe_value = desired_value if isinstance(desired_value, int) and not isinstance(desired_value, bool) else -1
        return WriteResult(
            status=WriteStatus.FAILED,
            variable_id=safe_id,
            requested_value=safe_value,
            error=str(exc),
        )
