"""Deterministic reconciliation of calendar evaluation results to IoX variables."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
from typing import Mapping, Sequence, Tuple

from .evaluator import evaluate_snapshot
from .iox_variables import VariableTransport, WriteResult, WriteStatus, write_state_variable
from .models import CalendarSnapshot, EvaluationResult, Rule


class ReconciliationStatus(str, Enum):
    COMPLETE = "complete"
    UNTRUSTED = "untrusted"
    PARTIAL_FAILURE = "partial_failure"
    ERROR = "error"


@dataclass(frozen=True)
class ReconciliationResult:
    status: ReconciliationStatus
    evaluation: EvaluationResult | None
    processed: Mapping[int, WriteResult] = field(default_factory=dict)
    failed_variable_id: int | None = None
    unattempted_variable_ids: Tuple[int, ...] = ()
    error: str | None = None


def _targets(rules: Sequence[Rule]) -> Tuple[int, ...]:
    return tuple(sorted({rule.target_variable_id for rule in rules}))


def _error_result(message: str, evaluation: EvaluationResult | None = None) -> ReconciliationResult:
    return ReconciliationResult(
        status=ReconciliationStatus.ERROR,
        evaluation=evaluation,
        error=message,
    )


def reconcile_snapshot(
    snapshot: CalendarSnapshot,
    rules: Sequence[Rule],
    now: datetime,
    transport: VariableTransport,
) -> ReconciliationResult:
    """Evaluate and reconcile configured calendar targets exactly once per pass."""

    try:
        target_ids = _targets(rules)
        evaluation = evaluate_snapshot(snapshot, rules, now)
    except Exception as exc:
        return _error_result(f"evaluation failed: {type(exc).__name__}")

    if not evaluation.trusted:
        return ReconciliationResult(
            status=ReconciliationStatus.UNTRUSTED,
            evaluation=evaluation,
            error=evaluation.diagnostics[0] if evaluation.diagnostics else None,
        )

    desired_keys = set(evaluation.desired_states)
    target_keys = set(target_ids)
    if desired_keys != target_keys:
        return _error_result("trusted evaluation desired-state keys do not match configured targets", evaluation)

    for variable_id in target_ids:
        value = evaluation.desired_states[variable_id]
        if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1):
            return _error_result("trusted evaluation contains an invalid desired state", evaluation)

    writes = reconcile_desired_states(evaluation.desired_states, transport)
    return ReconciliationResult(
        status=writes.status,
        evaluation=evaluation,
        processed=writes.processed,
        failed_variable_id=writes.failed_variable_id,
        unattempted_variable_ids=writes.unattempted_variable_ids,
        error=writes.error,
    )


def reconcile_desired_states(
    desired_states: Mapping[int, int],
    transport: VariableTransport,
    *,
    allowed_variable_ids: frozenset[int] | None = None,
) -> ReconciliationResult:
    """Reconcile an already trusted desired-state map.

    The caller is responsible for producing this map only from a complete,
    current and compatible snapshot.  This entry point deliberately accepts
    no snapshot, so a failed refresh cannot accidentally be converted to OFF.
    """

    if not isinstance(desired_states, Mapping) or not desired_states:
        return _error_result("trusted desired states are missing")
    target_ids = tuple(sorted(desired_states))
    if any(type(variable_id) is not int or variable_id <= 0 for variable_id in target_ids):
        return _error_result("trusted desired states contain an invalid variable ID")
    if allowed_variable_ids is not None and not set(target_ids).issubset(allowed_variable_ids):
        return _error_result("trusted desired states contain a variable outside the allowlist")
    for variable_id in target_ids:
        value = desired_states[variable_id]
        if type(value) is not int or value not in (0, 1):
            return _error_result("trusted desired states contain an invalid value")

    processed: dict[int, WriteResult] = {}
    for index, variable_id in enumerate(target_ids):
        try:
            write_result = write_state_variable(
                transport,
                variable_id,
                desired_states[variable_id],
            )
        except Exception as exc:
            return ReconciliationResult(
                status=ReconciliationStatus.ERROR,
                evaluation=None,
                processed=processed,
                failed_variable_id=variable_id,
                unattempted_variable_ids=target_ids[index + 1 :],
                error=f"integration failed for variable {variable_id}: {type(exc).__name__}",
            )
        if not isinstance(write_result, WriteResult):
            return ReconciliationResult(
                status=ReconciliationStatus.ERROR,
                evaluation=None,
                processed=processed,
                failed_variable_id=variable_id,
                unattempted_variable_ids=target_ids[index + 1 :],
                error=f"invalid write result for variable {variable_id}",
            )
        if write_result.status not in (WriteStatus.NOOP, WriteStatus.WRITTEN, WriteStatus.FAILED):
            return ReconciliationResult(
                status=ReconciliationStatus.ERROR,
                evaluation=None,
                processed=processed,
                failed_variable_id=variable_id,
                unattempted_variable_ids=target_ids[index + 1 :],
                error=f"unexpected write status for variable {variable_id}",
            )
        processed[variable_id] = write_result
        if write_result.status is WriteStatus.FAILED:
            return ReconciliationResult(
                status=ReconciliationStatus.PARTIAL_FAILURE,
                evaluation=None,
                processed=processed,
                failed_variable_id=variable_id,
                unattempted_variable_ids=target_ids[index + 1 :],
                error="variable write failed",
            )

    return ReconciliationResult(
        status=ReconciliationStatus.COMPLETE,
        evaluation=None,
        processed=processed,
    )
