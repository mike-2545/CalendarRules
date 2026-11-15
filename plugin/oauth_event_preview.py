"""Read-only Google event evaluation on the test eisy.

No calendar mutation occurs. Private activity details remain outside logs and
generic result representations; bounded titles reach only the authenticated
eisy-ui notice when the owner explicitly enables that display setting.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from threading import Event, Lock
from time import monotonic
from typing import Any, Callable, Mapping

from eisy_google_calendar.activity import ActivityResult, calculate_activity
from eisy_google_calendar.calendar_api import FetchPage, fetch_snapshot
from eisy_google_calendar.cached_schedule import CachedScheduleEngine
from eisy_google_calendar.google_events_http import GoogleEventsHttp
from eisy_google_calendar.models import SnapshotStatus
from eisy_google_calendar.preview import preview_snapshot
from eisy_google_calendar.snapshot_store import SnapshotStorage
from eisy_google_calendar.ui_configuration import UiConfiguration
from oauth_event_bridge import OAuthEventStatus
from oauth_readonly_discovery import ReadOnlyOAuthDiscoveryProbe


MAX_REFRESH_SECONDS = 30


@dataclass(frozen=True)
class EventPreviewSummary:
    """Safe-to-display counts; no private calendar or rule identifiers."""

    status: str
    events_checked: int = 0
    active_variable_count: int = 0
    total_variable_count: int = 0


@dataclass(frozen=True)
class PeriodicPreviewSummary:
    """Sanitized result of one serialized cached-schedule tick."""

    status: str
    refresh_attempted: bool = False
    refresh_succeeded: bool | None = None
    evaluated: bool = False
    events_checked: int = 0
    active_variable_count: int = 0
    total_variable_count: int = 0
    write_status: str = "disabled"
    last_successful_sync_at: datetime | None = None
    next_refresh_at: datetime | None = None
    manual_refresh_pending: bool = False
    # Titles are private owner-facing data. Keep them out of dataclass reprs so
    # generic diagnostics cannot accidentally include an activity payload.
    activity: ActivityResult | None = field(default=None, repr=False)


class ReadOnlyOAuthEventPreview(ReadOnlyOAuthDiscoveryProbe):
    """An explicitly opted-in event preview backed by saved UD OAuth.

    Shares the verified source check, protected OAuth events, exact two-scope
    validation, and effective INFO+ guard with the calendar-list probe.
    One preview attempt may request multiple pages/calendars but never writes.
    """

    def __init__(
        self,
        sdk_oauth: Any,
        *,
        safe_logging: Callable[[], bool],
        clock: Callable[[], datetime] | None = None,
        fetch_events_factory: Callable[[Callable[[], str]], FetchPage] | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        refresh_timeout_seconds: int = MAX_REFRESH_SECONDS,
        trusted_state_consumer: Callable[[Mapping[int, int]], Any] | None = None,
        snapshot_storage: SnapshotStorage | None = None,
    ):
        # The inherited discovery method is intentionally never called.
        super().__init__(
            sdk_oauth,
            safe_logging=safe_logging,
            fetch_page=lambda _token, _params: {},
        )
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        if (
            not callable(monotonic_clock)
            or type(refresh_timeout_seconds) is not int
            or not 1 <= refresh_timeout_seconds <= MAX_REFRESH_SECONDS
        ):
            raise ValueError("schedule worker configuration is invalid")
        self._monotonic_clock = monotonic_clock
        self._refresh_timeout_seconds = refresh_timeout_seconds
        self._events_factory = (
            fetch_events_factory
            if fetch_events_factory is not None
            else lambda provider: GoogleEventsHttp(get_access_token=provider)
        )
        self._preview_attempted = False
        self._schedule_engine: CachedScheduleEngine | None = None
        self._schedule_lock = Lock()
        self._worker_lock = Lock()
        self._worker = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="calendar-readonly-refresh",
        )
        self._worker_future: Future[PeriodicPreviewSummary] | None = None
        self._worker_generation = 0
        self._worker_future_generation = 0
        self._worker_cancel: Event | None = None
        self._worker_future_forced = False
        self._force_refresh_pending = False
        self._invalidate_pending = False
        self._active_cancel: Event | None = None
        self._active_deadline: float | None = None
        self._fetch_page: FetchPage | None = None
        self._trusted_state_consumer = trusted_state_consumer
        self._snapshot_storage = snapshot_storage

    def _guarded_token(self) -> str:
        """Recheck the effective logger before *each* token read."""

        if not self._logging_allowed():
            raise RuntimeError("read-only preview logging gate is closed")
        return self._sdk_oauth.getAccessToken()

    def preview_once(self, configuration: UiConfiguration | None) -> EventPreviewSummary:
        """Evaluate a complete snapshot without exporting source event details."""

        if self._preview_attempted:
            return EventPreviewSummary("already_attempted")
        self._preview_attempted = True
        if not self._logging_allowed():
            return EventPreviewSummary("blocked_logging")
        if not isinstance(configuration, UiConfiguration):
            return EventPreviewSummary("configuration_required")
        if self._configuration is None or not self._configuration.supports_calendar_discovery:
            return EventPreviewSummary("missing_scopes")
        if self.status is not OAuthEventStatus.GRANT_RECEIVED:
            return EventPreviewSummary("authentication_required")

        try:
            now = self._clock()
            fetch_page = self._events_factory(self._guarded_token)
            snapshot = fetch_snapshot(
                configuration.calendar,
                fetch_page=fetch_page,
                now=now,
                coverage_horizon=configuration.policy.coverage_horizon,
            )
            if snapshot.status is not SnapshotStatus.VALID:
                return EventPreviewSummary("api_error")

            preview = preview_snapshot(
                configuration.calendar,
                snapshot,
                now=now,
                max_snapshot_age=configuration.policy.max_snapshot_age,
                require_coverage=True,
            )
            if not preview.trusted:
                return EventPreviewSummary("untrusted")
            active_count = sum(value == 1 for value in preview.desired_states.values())
            return EventPreviewSummary(
                status="ready",
                events_checked=preview.source_event_count or 0,
                active_variable_count=active_count,
                total_variable_count=len(preview.desired_states),
            )
        except Exception:
            # In particular: no raw SDK, HTTP, calendar ID, or event exception.
            return EventPreviewSummary("api_error")

    def invalidate_schedule(self) -> None:
        """Cancel the current generation without waiting for network I/O."""

        with self._worker_lock:
            self._worker_generation += 1
            self._invalidate_pending = True
            self._force_refresh_pending = False
            self._worker_future_forced = False
            if self._worker_cancel is not None:
                self._worker_cancel.set()
            if self._worker_future is not None:
                self._worker_future.cancel()

    def _consume_invalidation(self) -> bool:
        with self._worker_lock:
            pending = self._invalidate_pending
            self._invalidate_pending = False
            return pending

    def _bounded_fetch_page(
        self,
        calendar_id: str,
        params: Any,
    ) -> Any:
        """Stop between pages after cancellation or the whole-refresh limit."""

        cancel = self._active_cancel
        deadline = self._active_deadline
        if (
            cancel is not None and cancel.is_set()
        ) or (
            deadline is not None and self._monotonic_clock() >= deadline
        ):
            raise RuntimeError("calendar refresh was cancelled")
        if self._fetch_page is None:
            raise RuntimeError("calendar refresh is unavailable")
        response = self._fetch_page(calendar_id, params)
        if (
            cancel is not None and cancel.is_set()
        ) or (
            deadline is not None and self._monotonic_clock() >= deadline
        ):
            raise RuntimeError("calendar refresh was cancelled")
        return response

    def _run_schedule_tick(
        self,
        configuration: UiConfiguration,
        cancel: Event,
        force_refresh: bool,
    ) -> PeriodicPreviewSummary:
        if cancel.is_set():
            return PeriodicPreviewSummary("cancelled")
        self._active_cancel = cancel
        self._active_deadline = (
            self._monotonic_clock() + self._refresh_timeout_seconds
        )
        try:
            return self.schedule_tick(configuration, force_refresh=force_refresh)
        finally:
            self._active_cancel = None
            self._active_deadline = None

    def request_schedule_tick(
        self,
        configuration: UiConfiguration | None,
    ) -> PeriodicPreviewSummary:
        """Harvest a completed tick and queue at most one serialized successor."""

        return self._request_schedule_tick(configuration, force_refresh=False)

    def request_refresh(
        self,
        configuration: UiConfiguration | None,
    ) -> PeriodicPreviewSummary:
        """Coalesce an immediate complete refresh onto the serialized worker."""

        return self._request_schedule_tick(configuration, force_refresh=True)

    def _request_schedule_tick(
        self,
        configuration: UiConfiguration | None,
        *,
        force_refresh: bool,
    ) -> PeriodicPreviewSummary:
        """Queue periodic or forced work without allowing concurrent fetches."""

        blocked = self._schedule_precondition(configuration)
        if blocked is not None:
            return PeriodicPreviewSummary(blocked)

        completed: PeriodicPreviewSummary | None = None
        with self._worker_lock:
            future = self._worker_future
            if (
                future is not None
                and self._worker_future_generation != self._worker_generation
            ):
                # The old generation is already cancelled. Stop tracking it
                # so one successor can queue behind it on the single worker.
                self._worker_future = None
                self._worker_cancel = None
                self._worker_future_forced = False
                future = None
            if future is not None and future.done():
                generation = self._worker_future_generation
                self._worker_future = None
                self._worker_cancel = None
                self._worker_future_forced = False
                if generation == self._worker_generation and not future.cancelled():
                    try:
                        completed = future.result()
                    except Exception:
                        completed = PeriodicPreviewSummary("api_error")

            if self._worker_future is not None and force_refresh:
                # A boolean deliberately coalesces any number of button
                # presses into at most one follow-up refresh.
                self._force_refresh_pending = True

            if (
                self._worker_future is None
                and isinstance(configuration, UiConfiguration)
            ):
                run_forced = force_refresh or self._force_refresh_pending
                self._force_refresh_pending = False
                cancel = Event()
                self._worker_cancel = cancel
                self._worker_future_generation = self._worker_generation
                self._worker_future_forced = run_forced
                self._worker_future = self._worker.submit(
                    self._run_schedule_tick,
                    configuration,
                    cancel,
                    run_forced,
                )

            manual_pending = (
                self._force_refresh_pending or self._worker_future_forced
            )

        if completed is not None:
            return replace(completed, manual_refresh_pending=manual_pending)
        return PeriodicPreviewSummary(
            "pending",
            manual_refresh_pending=manual_pending,
        )

    def _schedule_precondition(
        self,
        configuration: UiConfiguration | None,
    ) -> str | None:
        if not self._logging_allowed():
            return "blocked_logging"
        if not isinstance(configuration, UiConfiguration):
            return "configuration_required"
        if self._configuration is None or not self._configuration.supports_calendar_discovery:
            return "missing_scopes"
        if self.status is not OAuthEventStatus.GRANT_RECEIVED:
            return "authentication_required"
        return None

    def schedule_tick(
        self,
        configuration: UiConfiguration | None,
        *,
        force_refresh: bool = False,
    ) -> PeriodicPreviewSummary:
        """Refresh when due and evaluate trusted cached events once per minute.

        Lifecycle callbacks may arrive concurrently and in any order. The
        lock makes engine creation/reconfiguration/ticking atomic, while the
        engine's due times make duplicate callbacks idempotent. No IoX write
        transport is imported or invoked here.
        """

        blocked = self._schedule_precondition(configuration)
        if blocked is not None:
            return PeriodicPreviewSummary(blocked)

        try:
            with self._schedule_lock:
                if self._consume_invalidation() and self._schedule_engine is not None:
                    self._schedule_engine.invalidate_configuration()
                if self._schedule_engine is None:
                    self._fetch_page = self._events_factory(self._guarded_token)
                    self._schedule_engine = CachedScheduleEngine(
                        configuration.calendar,
                        policy=configuration.policy,
                        fetch_page=self._bounded_fetch_page,
                        clock=self._clock,
                        storage=self._snapshot_storage,
                    )
                else:
                    self._schedule_engine.reconfigure(
                        configuration.calendar,
                        configuration.policy,
                    )
                if force_refresh:
                    self._schedule_engine.request_refresh()
                tick = self._schedule_engine.tick()
        except Exception:
            # Never surface token, HTTP, calendar, event, or SDK exceptions.
            return PeriodicPreviewSummary("api_error")

        snapshot = self._schedule_engine.snapshot
        last_sync = (
            snapshot.fetched_at
            if snapshot is not None and snapshot.status is SnapshotStatus.VALID
            else None
        )
        if tick.preview is None:
            return PeriodicPreviewSummary(
                "idle",
                refresh_attempted=tick.refresh_attempted,
                refresh_succeeded=tick.refresh_succeeded,
                last_successful_sync_at=last_sync,
                next_refresh_at=tick.next_refresh_at,
            )
        if not tick.preview.trusted:
            activity = None
            if snapshot is not None:
                activity = calculate_activity(
                    configuration.calendar,
                    snapshot,
                    now=tick.now,
                    max_snapshot_age=configuration.policy.max_snapshot_age,
                    using_cached_data=self._schedule_engine.consecutive_failures > 0,
                )
            return PeriodicPreviewSummary(
                "invalid_interval"
                if tick.preview.reason == "adjusted event interval is invalid"
                else "untrusted",
                refresh_attempted=tick.refresh_attempted,
                refresh_succeeded=tick.refresh_succeeded,
                evaluated=True,
                last_successful_sync_at=last_sync,
                next_refresh_at=tick.next_refresh_at,
                activity=activity,
            )

        active_count = sum(
            value == 1 for value in tick.preview.desired_states.values()
        )
        write_status = "disabled"
        if (
            self._trusted_state_consumer is not None
        ):
            cancel = self._active_cancel
            if cancel is not None and cancel.is_set():
                return PeriodicPreviewSummary("cancelled")
            try:
                outcome = self._trusted_state_consumer(
                    dict(tick.preview.desired_states)
                )
                candidate = getattr(outcome, "status", None)
                write_status = candidate if candidate in {
                    "blocked", "noop", "written", "failed"
                } else "failed"
            except Exception:
                write_status = "failed"
        try:
            activity = calculate_activity(
                configuration.calendar,
                snapshot,
                now=tick.now,
                max_snapshot_age=configuration.policy.max_snapshot_age,
                using_cached_data=self._schedule_engine.consecutive_failures > 0,
            ) if snapshot is not None else None
        except Exception:
            activity = None
        return PeriodicPreviewSummary(
            "ready",
            refresh_attempted=tick.refresh_attempted,
            refresh_succeeded=tick.refresh_succeeded,
            evaluated=True,
            events_checked=tick.preview.source_event_count or 0,
            active_variable_count=active_count,
            total_variable_count=len(tick.preview.desired_states),
            write_status=write_status,
            last_successful_sync_at=last_sync,
            next_refresh_at=tick.next_refresh_at,
            activity=activity,
        )
