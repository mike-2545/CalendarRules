"""Pure planner for periodic fetches and minute-by-minute cached evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from .calendar_api import FetchPage, fetch_snapshot
from .config_identity import configuration_fingerprint
from .evaluator import rules_require_location_data, validate_event_time
from .models import CalendarConfig, CalendarSnapshot, SnapshotStatus
from .preview import PreviewResult, preview_snapshot
from .snapshot_store import SnapshotStorage
from .validation import ConfigurationError


ALLOWED_REFRESH_HOURS = (1, 2, 4)
DEFAULT_REFRESH_HOURS = 2
DEFAULT_MAX_SNAPSHOT_AGE = timedelta(days=30)
DEFAULT_COVERAGE_HORIZON = timedelta(days=32)
MAX_COVERAGE_HORIZON = timedelta(days=32)


@dataclass(frozen=True)
class SchedulePolicy:
    """Explicit refresh, retry, freshness, and coverage policy."""

    refresh_hours: int = DEFAULT_REFRESH_HOURS
    max_snapshot_age: timedelta = DEFAULT_MAX_SNAPSHOT_AGE
    coverage_horizon: timedelta = DEFAULT_COVERAGE_HORIZON
    retry_initial: timedelta = timedelta(minutes=15)
    retry_maximum: timedelta = timedelta(hours=1)
    evaluation_interval: timedelta = timedelta(minutes=1)

    def __post_init__(self):
        if type(self.refresh_hours) is not int or self.refresh_hours not in ALLOWED_REFRESH_HOURS:
            raise ConfigurationError("refresh_hours must be 1, 2, or 4")
        positive_fields = {
            "max_snapshot_age": self.max_snapshot_age,
            "coverage_horizon": self.coverage_horizon,
            "retry_initial": self.retry_initial,
            "retry_maximum": self.retry_maximum,
            "evaluation_interval": self.evaluation_interval,
        }
        for name, value in positive_fields.items():
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ConfigurationError(f"{name} must be a positive timedelta")
        if self.coverage_horizon < self.refresh_interval:
            raise ConfigurationError(
                "coverage_horizon must include at least one refresh interval"
            )
        if self.coverage_horizon > MAX_COVERAGE_HORIZON:
            raise ConfigurationError("coverage_horizon must not exceed 32 days")
        if self.retry_initial > self.retry_maximum:
            raise ConfigurationError("retry_initial must not exceed retry_maximum")
        if self.evaluation_interval != timedelta(minutes=1):
            raise ConfigurationError("evaluation_interval must be one minute")

    @property
    def refresh_interval(self) -> timedelta:
        return timedelta(hours=self.refresh_hours)


@dataclass(frozen=True)
class ScheduleTick:
    """Observable result of one manually driven planner iteration."""

    now: datetime
    refresh_attempted: bool
    refresh_succeeded: Optional[bool]
    preview: Optional[PreviewResult]
    next_refresh_at: datetime
    next_evaluation_at: datetime
    warning: Optional[str] = None


class CachedScheduleEngine:
    """Coordinate an injected provider, clock, and snapshot store.

    The engine has no thread, timer, network client, logger, or IoX transport.
    Callers explicitly invoke :meth:`tick` from tests or a future approved
    runtime integration.
    """

    def __init__(
        self,
        config: CalendarConfig,
        *,
        policy: SchedulePolicy,
        fetch_page: FetchPage,
        clock: Callable[[], datetime],
        storage: SnapshotStorage | None = None,
    ):
        self.config = config
        self.policy = policy
        self.fetch_page = fetch_page
        self.clock = clock
        self.storage = storage
        self.configuration_fingerprint = configuration_fingerprint(config)
        self._configuration_active = True
        self._snapshot: CalendarSnapshot | None = None
        self._restored = False
        self._next_refresh_at: datetime | None = None
        self._next_evaluation_at: datetime | None = None
        self._last_observed_at: datetime | None = None
        self._refresh_requested = False
        self._consecutive_failures = 0
        self.last_warning: str | None = None

    @property
    def snapshot(self) -> CalendarSnapshot | None:
        return self._snapshot

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def configuration_active(self) -> bool:
        return self._configuration_active

    def invalidate_configuration(self) -> None:
        """Suspend refresh and evaluation after an invalid/partial UI update."""

        self._configuration_active = False
        self._snapshot = None
        self._restored = True
        self._next_refresh_at = None
        self._next_evaluation_at = None
        self._consecutive_failures = 0
        self._refresh_requested = False
        self.last_warning = "calendar configuration is not valid"

    def reconfigure(
        self,
        config: CalendarConfig,
        policy: SchedulePolicy,
    ) -> bool:
        """Atomically apply validated settings and invalidate incompatible data."""

        fingerprint = configuration_fingerprint(config)
        policy_changed = policy != self.policy
        invalidate = (
            not self._configuration_active
            or fingerprint != self.configuration_fingerprint
        )
        self.config = config
        self.policy = policy
        self.configuration_fingerprint = fingerprint
        self._configuration_active = True
        if invalidate:
            self._snapshot = None
            # A persisted snapshot with the prior identity must not be restored.
            self._restored = True
            self._next_refresh_at = None
            self._next_evaluation_at = None
            self._consecutive_failures = 0
            self._refresh_requested = False
            self.last_warning = (
                "calendar configuration changed; a complete snapshot is required"
            )
        elif policy_changed:
            # A duplicate configuration callback is a no-op, but a real
            # policy change must take effect immediately. The prior snapshot
            # remains trustworthy while the next refresh is attempted.
            self._next_refresh_at = None
            self._next_evaluation_at = None
            self._consecutive_failures = 0
            self.last_warning = "calendar schedule policy changed; refresh required"
        return invalidate or policy_changed

    def request_refresh(self) -> bool:
        """Request one complete refresh without changing trusted data or policy."""

        if not self._configuration_active:
            return False
        self._refresh_requested = True
        return True

    def _clock_time(self) -> datetime:
        return validate_event_time(self.clock())

    def restore(self) -> bool:
        """Load a verified cache once; failures remain private and non-actionable."""

        if not self._configuration_active:
            return False
        if self._restored:
            return self._snapshot is not None
        self._restored = True
        if self.storage is None:
            return False
        try:
            restored = self.storage.load()
        except Exception:
            self.last_warning = "cached calendar snapshot could not be loaded"
            return False
        if restored is None:
            return False
        if restored.status is not SnapshotStatus.VALID:
            self.last_warning = "cached calendar snapshot is not trusted"
            return False
        if restored.configuration_fingerprint != self.configuration_fingerprint:
            self.last_warning = (
                "cached calendar snapshot does not match current configuration"
            )
            return False
        if (
            rules_require_location_data(self.config.rules)
            and not restored.location_data_complete
        ):
            self.last_warning = (
                "cached calendar snapshot lacks complete location data"
            )
            return False
        self._snapshot = restored
        return True

    def _retry_delay(self) -> timedelta:
        multiplier = 2 ** min(max(self._consecutive_failures - 1, 0), 16)
        return min(self.policy.retry_initial * multiplier, self.policy.retry_maximum)

    def _refresh(self, now: datetime) -> tuple[bool, Optional[str]]:
        candidate = fetch_snapshot(
            self.config,
            fetch_page=self.fetch_page,
            now=now,
            coverage_horizon=self.policy.coverage_horizon,
        )
        if candidate.status is not SnapshotStatus.VALID:
            self._consecutive_failures += 1
            self._next_refresh_at = now + self._retry_delay()
            if self._snapshot is None:
                return (
                    False,
                    "calendar refresh failed; no compatible trusted snapshot is available",
                )
            return False, "calendar refresh failed; retained last trusted snapshot"

        self._snapshot = candidate
        self._consecutive_failures = 0
        self._next_refresh_at = now + self.policy.refresh_interval
        if self.storage is not None:
            try:
                self.storage.save(candidate)
            except Exception:
                return True, "trusted snapshot is in memory but could not be persisted"
        return True, None

    def _evaluate(self, now: datetime) -> PreviewResult:
        snapshot = self._snapshot
        if snapshot is None:
            snapshot = CalendarSnapshot(
                status=SnapshotStatus.UNAVAILABLE,
                error="no trusted calendar snapshot is available",
            )
        return preview_snapshot(
            self.config,
            snapshot,
            now=now,
            max_snapshot_age=self.policy.max_snapshot_age,
            require_coverage=True,
        )

    def tick(self) -> ScheduleTick:
        """Perform at most one due refresh and one due local evaluation."""

        self.restore()
        now = self._clock_time()
        warning = self.last_warning if self._last_observed_at is None else None

        if not self._configuration_active:
            self._last_observed_at = now
            if self._next_refresh_at is None or now >= self._next_refresh_at:
                self._next_refresh_at = now + self.policy.refresh_interval
            preview = None
            if self._next_evaluation_at is None or now >= self._next_evaluation_at:
                preview = self._evaluate(now)
                self._next_evaluation_at = now + self.policy.evaluation_interval
            return ScheduleTick(
                now=now,
                refresh_attempted=False,
                refresh_succeeded=None,
                preview=preview,
                next_refresh_at=self._next_refresh_at,
                next_evaluation_at=self._next_evaluation_at,
                warning=warning,
            )

        if self._last_observed_at is not None and now < self._last_observed_at:
            self._next_refresh_at = now
            self._next_evaluation_at = now
            warning = "clock moved backwards; refresh and evaluation were rebased"
        self._last_observed_at = now

        if self._next_refresh_at is None:
            self._next_refresh_at = now
        if self._next_evaluation_at is None:
            self._next_evaluation_at = now

        forced_refresh = self._refresh_requested
        self._refresh_requested = False
        refresh_attempted = forced_refresh or now >= self._next_refresh_at
        refresh_succeeded: Optional[bool] = None
        if refresh_attempted:
            refresh_succeeded, refresh_warning = self._refresh(now)
            if refresh_warning is not None:
                warning = refresh_warning
            if forced_refresh:
                # A user-requested refresh must promptly recalculate desired
                # states from either the replacement snapshot or, on failure,
                # the still-trusted prior snapshot.
                self._next_evaluation_at = now

        preview = None
        if now >= self._next_evaluation_at:
            preview = self._evaluate(now)
            self._next_evaluation_at = now + self.policy.evaluation_interval

        self.last_warning = warning
        return ScheduleTick(
            now=now,
            refresh_attempted=refresh_attempted,
            refresh_succeeded=refresh_succeeded,
            preview=preview,
            next_refresh_at=self._next_refresh_at,
            next_evaluation_at=self._next_evaluation_at,
            warning=warning,
        )
