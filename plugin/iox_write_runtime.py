"""Explicitly gated IoX state-variable integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from threading import Lock
from time import monotonic
from typing import Callable, Mapping

from eisy_google_calendar.iox_variables import VariableTransport, WriteStatus
from eisy_google_calendar.reconciler import (
    ReconciliationStatus,
    reconcile_desired_states,
)


MAX_VERIFIED_POLL_AGE_SECONDS = 90
LIVE_IOX_TRANSPORT_AVAILABLE = True
LOGGER = logging.getLogger("eisy_google_calendar.iox")


class WriteActivationGate:
    """Thread-safe runtime gates in addition to the protected marker."""

    def __init__(
        self,
        *,
        marker_check: Callable[[], bool],
        transport_ready: Callable[[], bool],
        configured_ids: Callable[[], frozenset[int]],
        monotonic_clock: Callable[[], float] = monotonic,
        max_poll_age_seconds: int = MAX_VERIFIED_POLL_AGE_SECONDS,
    ):
        if type(max_poll_age_seconds) is not int or max_poll_age_seconds <= 0:
            raise ValueError("poll gate lifetime must be a positive integer")
        self._marker_check = marker_check
        self._transport_ready = transport_ready
        self._configured_ids = configured_ids
        self._monotonic_clock = monotonic_clock
        self._max_poll_age_seconds = max_poll_age_seconds
        self._started = False
        self._minute_poll_verified = False
        self._last_short_poll_at: float | None = None
        self._lock = Lock()

    @property
    def enabled(self) -> bool:
        with self._lock:
            started = self._started
            verified = self._minute_poll_verified
            last_poll = self._last_short_poll_at
        try:
            now = float(self._monotonic_clock())
            poll_fresh = (
                last_poll is not None
                and now >= last_poll
                and now - last_poll <= self._max_poll_age_seconds
            )
            return (
                started
                and verified
                and poll_fresh
                and self._marker_check() is True
                and self._transport_ready() is True
            )
        except Exception:
            return False

    def update_runtime(
        self,
        *,
        started: bool,
        minute_poll_verified: bool,
        last_short_poll_at: float | None,
    ) -> None:
        with self._lock:
            self._started = bool(started)
            self._minute_poll_verified = bool(minute_poll_verified)
            self._last_short_poll_at = (
                float(last_short_poll_at)
                if type(last_short_poll_at) in (int, float)
                else None
            )

    def permits(self, desired_states: Mapping[int, int]) -> bool:
        try:
            configured_ids = self._configured_ids()
            targets_allowed = (
                isinstance(configured_ids, frozenset)
                and bool(configured_ids)
                and set(desired_states) == set(configured_ids)
            )
        except Exception:
            targets_allowed = False
        return targets_allowed and self.enabled

    @property
    def configured_ids(self) -> frozenset[int]:
        try:
            value = self._configured_ids()
            return value if isinstance(value, frozenset) else frozenset()
        except Exception:
            return frozenset()


@dataclass(frozen=True)
class TrustedWriteSummary:
    status: str


class TrustedStateWriter:
    """Write only complete trusted decisions supplied by the schedule engine."""

    def __init__(
        self,
        transport: VariableTransport,
        gate: WriteActivationGate,
        *,
        safe_logging: Callable[[], bool],
    ):
        self._transport = transport
        self._gate = gate
        self._safe_logging = safe_logging

    def __call__(self, desired_states: Mapping[int, int]) -> TrustedWriteSummary:
        outer = self

        class GuardedTransport:
            """Recheck STOP, cadence and logging before every IoX operation."""

            @staticmethod
            def _permitted() -> bool:
                return outer._safe_logging() and outer._gate.permits(desired_states)

            def get(self, path: str) -> str:
                if not self._permitted():
                    raise RuntimeError("IoX write gate closed")
                return outer._transport.get(path)

            def set(self, path: str) -> str:
                if not self._permitted():
                    raise RuntimeError("IoX write gate closed")
                return outer._transport.set(path)

        try:
            if not self._safe_logging() or not self._gate.permits(desired_states):
                return TrustedWriteSummary("blocked")
            result = reconcile_desired_states(
                desired_states,
                GuardedTransport(),
                allowed_variable_ids=self._gate.configured_ids,
            )
        except Exception:
            return TrustedWriteSummary("failed")
        if result.status is not ReconciliationStatus.COMPLETE:
            if result.failed_variable_id is not None:
                if result.failed_variable_id.bit_length() <= 63:
                    LOGGER.warning(
                        "IoX reconciliation failed closed for configured variable %d",
                        result.failed_variable_id,
                    )
                else:
                    LOGGER.warning(
                        "IoX reconciliation failed closed for a configured variable"
                    )
            return TrustedWriteSummary("failed")
        statuses = {item.status for item in result.processed.values()}
        if WriteStatus.WRITTEN in statuses:
            written_ids = sorted(
                variable_id
                for variable_id, item in result.processed.items()
                if item.status is WriteStatus.WRITTEN
            )
            LOGGER.info(
                "IoX write and readback verified for configured variables %s",
                ",".join(str(variable_id) for variable_id in written_ids),
            )
            return TrustedWriteSummary("written")
        return TrustedWriteSummary("noop")
