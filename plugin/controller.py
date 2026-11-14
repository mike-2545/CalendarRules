"""Minimal offline-safe eisy-ui controller wrapper."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import udi_interface

from eisy_google_calendar.activity import ActivityResult, format_activity_notices
from eisy_google_calendar.config_identity import configuration_fingerprint
from eisy_google_calendar.ui_configuration import (
    UiConfiguration,
    load_typed_configuration,
    typed_parameter_schema,
)
from eisy_google_calendar.validation import ConfigurationError


LOGGER = logging.getLogger("eisy_google_calendar.plugin")
MIN_SHORT_POLL_SECONDS = 45
MAX_SHORT_POLL_SECONDS = 90
MONTH_ABBREVIATIONS = (
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
REFRESH_DRIVER_VALUES = {
    "idle": 0,
    "pending": 1,
    "in_progress": 1,
    "succeeded": 2,
    "failed_using_cache": 3,
    "failed": 4,
    "unavailable": 4,
    "cancelled": 0,
}
CACHE_DRIVER_VALUES = {
    "unavailable": 0,
    "untrusted": 0,
    "trusted": 1,
}
IOX_DRIVER_VALUES = {
    "ready": 0,
    "inactive": 1,
    "unauthorized": 1,
    "failed": 2,
    "pending": 3,
    "unconfigured": 3,
}
MAX_STATUS_COUNT = 10000
ACTIVITY_NOTICE_KEYS = (
    "calendar_activity",
    "calendar_activity_active",
    *(f"calendar_activity_upcoming_{index:02d}" for index in range(1, 11)),
)


class CalendarController(udi_interface.Node):
    """Controller node; optional read-only network work stays off its thread."""

    id = "calctrl"
    drivers = [
        {"driver": "ST", "value": 0, "uom": 25},
        {"driver": "GV0", "value": 0, "uom": 25},
        {"driver": "GV1", "value": 0, "uom": 25},
        {"driver": "GV2", "value": 0, "uom": 56},
        {"driver": "GV3", "value": 0, "uom": 56},
        {"driver": "GV4", "value": 1, "uom": 25},
    ]
    children = []

    def __init__(
        self,
        polyglot: Any,
        controller: str = "calctrl",
        address: str = "calctrl",
        name: str = "Google Calendar",
        schedule_engine: Any = None,
        available_state_variable_ids: set[int] | None = None,
        oauth_bridge: Any = None,
        poll_clock: Any = monotonic,
        write_gate: Any = None,
        iox_read_only_probe: Any = None,
    ) -> None:
        super().__init__(polyglot, controller, address, name)
        self.poly = polyglot
        self.configuration: Mapping[str, Any] = {}
        self.custom_params: Mapping[str, Any] = {}
        self.schedule_engine = schedule_engine
        self.available_state_variable_ids = available_state_variable_ids
        # Optional OAuth/calendar behavior is supplied only by main.py after
        # its exclusive, owner-protected runtime marker and SDK checks pass.
        self.oauth_bridge = oauth_bridge
        self.write_gate = write_gate
        self.iox_read_only_probe = iox_read_only_probe
        self._discovery_attempted = False
        self._poll_clock = poll_clock
        self._minute_poll_requested = False
        self._minute_poll_verified = False
        self._last_short_poll_at: float | None = None
        self._valid_short_poll_intervals = 0
        self.active_configuration: UiConfiguration | None = None
        self.google_auth_status = (
            "disabled" if oauth_bridge is None else "not_configured"
        )
        self.configuration_status = "not_configured"
        self.cache_status = "unavailable"
        self.refresh_status = "idle"
        self.events_evaluated = 0
        self.targets_desired_on = 0
        self.iox_status = (
            "pending" if iox_read_only_probe is not None else "unauthorized"
        )
        self.last_successful_sync_at: datetime | None = None
        self._manual_refresh_pending = False
        self.config_received = False
        self.custom_params_received = False
        self.config_done = False
        self.start_requested = False
        self.started = False
        self.stopped = False
        self.lifecycle: list[str] = []
        self._registered = False
        # These are SDK queue requests, not receiver acknowledgements.  They
        # suppress ordinary duplicate reports only after the post-registration
        # recovery snapshot has been requested from a real short-poll callback.
        self._requested_status_values: dict[str, int] = {}
        self._forced_status_snapshot_pending = False
        self._activity_notice_values: dict[str, str] = {}
        self._activity_notices_initialized = False

        self.TypedParams = udi_interface.Custom(polyglot, "customtypedparams")
        self.TypedData = udi_interface.Custom(polyglot, "customtypeddata")
        self.TypedParams.load(typed_parameter_schema(), save=True)

        self.poly.subscribe(self.poly.CONFIG, self.processConfig)
        self.poly.subscribe(self.poly.CUSTOMPARAMS, self.parameterHandler)
        self.poly.subscribe(
            self.poly.CUSTOMTYPEDDATA,
            self.typedConfigurationHandler,
        )
        self.poly.subscribe(self.poly.CONFIGDONE, self._config_done)
        self.poly.subscribe(self.poly.START, self._start_event, address)
        self.poly.subscribe(self.poly.STOP, self._stop_event)
        if self.oauth_bridge is not None:
            self.poly.subscribe(self.poly.CUSTOMNS, self._custom_namespace_handler)
            self.poly.subscribe(self.poly.OAUTH, self._authorization_handler)
            if (
                callable(getattr(self.oauth_bridge, "discover_once", None))
                or callable(getattr(self.oauth_bridge, "preview_once", None))
                or callable(getattr(self.oauth_bridge, "schedule_tick", None))
                or callable(getattr(self.oauth_bridge, "request_schedule_tick", None))
            ):
                self.poly.subscribe(self.poly.POLL, self._discovery_poll_handler)
        self._notice(
            "google_auth",
            "Google authorization is disabled in this offline milestone."
            if self.oauth_bridge is None
            else "Google authorization event bridge is inactive until publisher setup.",
        )
        if self.iox_read_only_probe is not None:
            self._notice(
                "iox_access",
                "IoX target validation is awaiting valid CalendarRules configuration.",
            )

    def register_node(self) -> bool:
        """Register the controller once, after the Polyglot ready handshake."""
        if self._registered:
            return True
        # PG3/eisy-ui watches this driver for the controller connection state.
        self.poly.addNode(self, conn_status="ST")
        self._registered = True
        self._forced_status_snapshot_pending = True
        self._report_status_snapshot()
        return True

    def _publish_status_driver(
        self, driver: str, value: int, *, force: bool = False
    ) -> bool:
        """Report one sanitized native status without affecting core work."""

        if not self._registered:
            return True
        if not force and self._requested_status_values.get(driver) == value:
            return True
        try:
            # SDK 3.4.9 queues this asynchronously.  A normal return confirms
            # only a local request, never delivery to eisy-ui or IoX.
            self.setDriver(driver, value, report=True, force=force)
        except Exception:
            LOGGER.warning("Unable to publish CalendarRules status driver %s", driver)
            return False
        self._requested_status_values[driver] = value
        return True

    def _set_refresh_status(self, status: str) -> bool:
        safe_status = status if status in REFRESH_DRIVER_VALUES else "failed"
        self.refresh_status = safe_status
        return self._publish_status_driver(
            "GV0", REFRESH_DRIVER_VALUES[safe_status]
        )

    def _set_cache_status(self, status: str) -> bool:
        safe_status = status if status in CACHE_DRIVER_VALUES else "unavailable"
        self.cache_status = safe_status
        return self._publish_status_driver("GV1", CACHE_DRIVER_VALUES[safe_status])

    def _set_evaluation_counts(self, events: int, active: int) -> bool:
        if (
            type(events) is not int
            or type(active) is not int
            or not 0 <= events <= MAX_STATUS_COUNT
            or not 0 <= active <= MAX_STATUS_COUNT
        ):
            return False
        self.events_evaluated = events
        self.targets_desired_on = active
        events_ok = self._publish_status_driver("GV2", events)
        active_ok = self._publish_status_driver("GV3", active)
        return events_ok and active_ok

    def _set_iox_status(self, status: str) -> bool:
        safe_status = status if status in IOX_DRIVER_VALUES else "failed"
        self.iox_status = safe_status
        return self._publish_status_driver("GV4", IOX_DRIVER_VALUES[safe_status])

    def _report_status_snapshot(self, *, force: bool = False) -> bool:
        """Publish all current aggregate states after node registration."""

        results = (
            self._publish_status_driver(
                "GV0", REFRESH_DRIVER_VALUES[self.refresh_status], force=force
            ),
            self._publish_status_driver(
                "GV1", CACHE_DRIVER_VALUES[self.cache_status], force=force
            ),
            self._publish_status_driver("GV2", self.events_evaluated, force=force),
            self._publish_status_driver(
                "GV3", self.targets_desired_on, force=force
            ),
            self._publish_status_driver(
                "GV4", IOX_DRIVER_VALUES[self.iox_status], force=force
            ),
        )
        return all(results)

    def _complete_forced_status_snapshot(self) -> bool:
        """Request one complete snapshot after the receiver registration race."""

        if not self._forced_status_snapshot_pending:
            return True
        completed = self._report_status_snapshot(force=True)
        if completed:
            # End-to-end delivery remains the deployment verifier's job.  This
            # flag records only that every local SDK request returned normally.
            self._forced_status_snapshot_pending = False
        return completed

    def _update_oauth_status(self, accepted: bool) -> bool:
        """Expose only a fixed, non-secret event status."""

        status = getattr(getattr(self.oauth_bridge, "status", None), "value", None)
        allowed = {
            "not_configured",
            "configuration_received",
            "grant_received",
            "event_error",
        }
        self.google_auth_status = (
            status if accepted and status in allowed else "event_error"
        )
        messages = {
            "not_configured": "Google publisher authorization is not configured.",
            "configuration_received": (
                "Google publisher authorization settings received; "
                "Google access is not yet verified."
            ),
            "grant_received": (
                "Google authorization event received; credentials and "
                "Google access have not yet been verified."
            ),
            "event_error": "Google authorization event could not be processed.",
        }
        self._notice("google_auth", messages[self.google_auth_status])
        if self.google_auth_status == "event_error":
            self._publish_activity_unavailable()
        return accepted and self.google_auth_status != "event_error"

    def _custom_namespace_handler(self, key: Any, data: Any) -> bool:
        """Only an explicitly injected OAuth adapter can receive namespace data."""

        if self.oauth_bridge is None:
            return False
        try:
            accepted = self.oauth_bridge.custom_namespace(key, data)
        except Exception:
            accepted = False
        updated = self._update_oauth_status(bool(accepted))
        if updated:
            self._periodic_schedule_handler()
        return updated

    def _authorization_handler(self, data: Any) -> bool:
        """Forward grants without retaining, logging, or displaying tokens."""

        if self.oauth_bridge is None:
            return False
        try:
            accepted = self.oauth_bridge.authorization_event(data)
        except Exception:
            accepted = False
        updated = self._update_oauth_status(bool(accepted))
        if updated:
            self._periodic_schedule_handler()
        return updated

    def _discovery_poll_handler(self, *_args: Any) -> bool:
        """Run the selected read-only poll behavior after controller startup.

        No calendar IDs, event titles, tokens, or HTTP responses are logged.
        Polyglot Connected remains independent of Google health.
        """

        if (
            callable(getattr(self.oauth_bridge, "request_schedule_tick", None))
            or callable(getattr(self.oauth_bridge, "schedule_tick", None))
        ):
            if not self.started or not self.config_done:
                return False
            if _args and _args[0] != "shortPoll":
                return True
            if _args:
                self._observe_short_poll()
            self._update_iox_probe_status()
            result = self._periodic_schedule_handler()
            if _args:
                # Apply the latest completed probe and trusted schedule result
                # first, then recover from status messages that raced addNode.
                self._complete_forced_status_snapshot()
            return result
        if not self.started or not self.config_done:
            return False
        if self._discovery_attempted:
            return True
        self._discovery_attempted = True
        if callable(getattr(self.oauth_bridge, "preview_once", None)):
            return self._event_preview_poll_handler()
        try:
            outcome = self.oauth_bridge.discover_once()
            status = outcome.status
            count = outcome.calendar_count
        except Exception:
            status = "api_error"
            count = 0

        messages = {
            "blocked_logging": "Read-only Google discovery blocked: log level must be Info or higher.",
            "missing_scopes": "Read-only Google discovery blocked: required scopes are unavailable.",
            "authentication_required": "Google authorization is required before calendar discovery.",
            "refresh_failed": "Google authorization could not be refreshed.",
            "revoked": "Google authorization was rejected; authenticate again.",
            "api_error": "Google Calendar discovery failed; no schedules or variables were changed.",
            "missing_configuration": "Google publisher authorization configuration is incomplete.",
        }
        if status == "ready" and type(count) is int and 0 <= count <= 10000:
            self.google_auth_status = "ready"
            self._notice(
                "google_auth",
                f"Read-only Google Calendar discovery succeeded: {count} calendars found."
            )
            return True
        self.google_auth_status = "discovery_failed"
        self._notice(
            "google_auth",
            messages.get(status, "Read-only Google Calendar discovery is unavailable."),
        )
        return False

    def _periodic_schedule_handler(self) -> bool:
        """Queue one cached-schedule evaluation when fully ready."""

        # A transient local SDK exception must not leave a stale native status.
        # Successfully requested values are suppressed during normal operation;
        # the first real short poll separately forces a complete snapshot.
        self._report_status_snapshot()
        schedule_tick = getattr(self.oauth_bridge, "request_schedule_tick", None)
        if not callable(schedule_tick):
            schedule_tick = getattr(self.oauth_bridge, "schedule_tick", None)
        if (
            not callable(schedule_tick)
            or not self.started
            or not self.config_done
            or self.configuration_status != "valid"
            or self.active_configuration is None
        ):
            return False
        try:
            outcome = schedule_tick(self.active_configuration)
            status = outcome.status
        except Exception:
            status = "api_error"
            outcome = None

        sync_time = getattr(outcome, "last_successful_sync_at", None)
        if isinstance(sync_time, datetime) and sync_time.tzinfo is not None:
            self.last_successful_sync_at = sync_time.astimezone(timezone.utc)

        if status in ("pending", "cancelled"):
            if self._manual_refresh_pending or bool(
                getattr(outcome, "manual_refresh_pending", False)
            ):
                self._set_refresh_status("in_progress")
                self._refresh_notice("Refresh Now is in progress.")
            return True
        if status == "idle":
            if self.cache_status == "trusted":
                self.google_auth_status = "schedule_ready"
            return True
        if status == "ready" and outcome is not None:
            events = outcome.events_checked
            active = outcome.active_variable_count
            total = outcome.total_variable_count
            valid_counts = (
                type(events) is int and 0 <= events <= 10000
                and type(total) is int and 0 <= total <= 10000
                and type(active) is int and 0 <= active <= total
            )
            if valid_counts:
                write_status = getattr(outcome, "write_status", "disabled")
                self.google_auth_status = "schedule_ready"
                self._set_cache_status("trusted")
                self._set_evaluation_counts(events, active)
                refresh = (
                    "refresh succeeded"
                    if outcome.refresh_succeeded is True
                    else "refresh failed; retained trusted cache"
                    if outcome.refresh_succeeded is False
                    else "cached evaluation"
                )
                message = (
                    f"Calendar schedule {refresh}: {events} events checked; "
                    f"{active} of {total} state variables should be ON. "
                    + (
                        "IoX state verified without a change."
                        if write_status == "noop"
                        else "IoX state write and readback verified."
                        if write_status == "written"
                        else "IoX write failed closed; it will be retried from trusted data."
                        if write_status == "failed"
                        else "No IoX writes performed; writes remain disabled."
                    )
                )
                self._notice("google_auth", message)
                self._publish_activity_notice(getattr(outcome, "activity", None))
                LOGGER.info(
                    "Schedule tick: refresh=%s events=%d on=%d total=%d",
                    outcome.refresh_succeeded,
                    events,
                    active,
                    total,
                )
                if outcome.refresh_succeeded is not None:
                    if bool(getattr(outcome, "manual_refresh_pending", False)):
                        self._set_refresh_status("in_progress")
                        self._refresh_notice(
                            "Refresh Now completed one request; a coalesced refresh is in progress."
                        )
                    else:
                        self._manual_refresh_pending = False
                        if outcome.refresh_succeeded:
                            self._set_refresh_status("succeeded")
                            self._refresh_notice("Google Calendar refresh succeeded.")
                        else:
                            self._set_refresh_status("failed_using_cache")
                            self._refresh_notice(
                                "Google Calendar refresh failed; using the last trusted cache."
                            )
                return True
            status = "untrusted"

        self.google_auth_status = "schedule_unavailable"
        self._set_cache_status("unavailable")
        self._set_evaluation_counts(0, 0)
        messages = {
            "blocked_logging": "Calendar schedule blocked: log level must be Info or higher.",
            "configuration_required": "Calendar schedule needs valid calendar and rule settings.",
            "missing_scopes": "Calendar schedule blocked: required read-only scopes are unavailable.",
            "authentication_required": "Google authentication is required before calendar refresh.",
            "untrusted": "Calendar schedule is untrusted; no state decisions were produced.",
            "api_error": "Calendar refresh could not complete; no state decisions were produced.",
        }
        self._notice(
            "google_auth",
            messages.get(status, "Calendar schedule is unavailable."),
        )
        self._publish_activity_unavailable()
        if self._manual_refresh_pending:
            self._manual_refresh_pending = False
            self._set_refresh_status("failed")
            self._refresh_notice(
                "Refresh Now failed; the last trusted cache was preserved."
            )
        elif getattr(outcome, "refresh_succeeded", None) is False:
            self._set_refresh_status("failed")
        return False

    def _publish_activity_notice(self, activity: Any) -> bool:
        """Publish bounded activity only through authenticated eisy-ui notices."""

        if not isinstance(activity, ActivityResult) or self.active_configuration is None:
            self._publish_activity_unavailable()
            return False
        try:
            current_fingerprint = configuration_fingerprint(
                self.active_configuration.calendar
            )
        except Exception:
            self._publish_activity_unavailable()
            return False
        # A completed worker from an older schedule generation must never
        # republish event details under a newer privacy/configuration state.
        if (
            not activity.trusted
            or activity.configuration_fingerprint != current_fingerprint
        ):
            self._publish_activity_unavailable()
            return False
        messages = format_activity_notices(
            activity,
            timezone_name=self.active_configuration.calendar.timezone,
            show_event_titles=self.active_configuration.show_event_titles,
            upcoming_event_limit=self.active_configuration.upcoming_event_limit,
        )
        self._replace_activity_notices(messages)
        return True

    def _replace_activity_notices(self, messages: Mapping[str, str]) -> None:
        """Synchronize the fixed activity key set without accumulating rows."""

        desired = {
            key: value
            for key, value in messages.items()
            if key in ACTIVITY_NOTICE_KEYS and isinstance(value, str)
        }
        for key, value in desired.items():
            if self._activity_notice_values.get(key) != value:
                self._notice(key, value)
        for key in ACTIVITY_NOTICE_KEYS:
            if key not in desired and (
                not self._activity_notices_initialized
                or key in self._activity_notice_values
            ):
                self.poly.Notices.delete(key)
        self._activity_notice_values = desired
        self._activity_notices_initialized = True

    def _publish_activity_unavailable(self) -> None:
        self._replace_activity_notices(
            {
                "calendar_activity": (
                    "Calendar activity unavailable: no trusted, compatible schedule."
                )
            }
        )

    def _publish_activity_awaiting(self) -> None:
        self._replace_activity_notices(
            {
                "calendar_activity": (
                    "Calendar activity is awaiting the next trusted schedule evaluation."
                )
            }
        )

    def _update_iox_probe_status(self) -> str:
        """Advance configured-target read-only validation without blocking."""

        request = getattr(self.iox_read_only_probe, "request", None)
        if not callable(request):
            return "disabled"
        try:
            status = request()
        except Exception:
            status = "failed"
        messages = {
            "inactive": "IoX configured-target access check is inactive.",
            "unconfigured": (
                "IoX target validation is awaiting valid CalendarRules configuration."
            ),
            "unauthorized": (
                "IoX access is not authorized; configured state variables were not read or changed."
            ),
            "pending": "IoX configured-target read-only validation is in progress.",
            "ready": "",
            "failed": (
                "IoX configured-target read-only validation failed; writes remain blocked."
            ),
        }
        safe_status = status if status in messages else "failed"
        self._set_iox_status(safe_status)
        message = messages[safe_status]
        target_ids = getattr(self.iox_read_only_probe, "target_ids", frozenset())
        if safe_status == "ready":
            if (
                isinstance(target_ids, frozenset)
                and target_ids
                and all(type(item) is int and item > 0 for item in target_ids)
            ):
                displayed = ", ".join(str(item) for item in sorted(target_ids)[:12])
                omitted = len(target_ids) - min(len(target_ids), 12)
                suffix = f" (+{omitted} more)" if omitted else ""
                gate_state = "open" if self.iox_writes_allowed else "closed"
                message = (
                    f"IoX type-2 variable {displayed} is readable; "
                    f"IoX write gate is {gate_state}."
                    if len(target_ids) == 1
                    else f"IoX type-2 variables {displayed}{suffix} are readable; "
                    f"IoX write gate is {gate_state}."
                )
            else:
                safe_status = "failed"
                self._set_iox_status(safe_status)
                message = messages["failed"]
        if safe_status == "failed":
            category = getattr(
                self.iox_read_only_probe, "failure_category", None
            )
            labels = {
                "authorization": "authorization rejected",
                "connection-refused": "connection refused",
                "connection-timeout": "connection timed out",
                "connection": "connection failed",
                "http-status": "unexpected HTTP status",
                "response-encoding": "response encoding invalid",
                "response-size": "response size invalid",
                "response-validation": "response validation failed",
                "tls": "TLS connection failed",
                "tls-certificate-validation": "TLS certificate validation failed",
                "trust-anchor": "TLS trust anchor invalid",
                "transport": "transport failed",
            }
            if category in labels:
                failed_variable_id = getattr(
                    self.iox_read_only_probe, "failed_variable_id", None
                )
                variable_label = (
                    f" for configured variable {failed_variable_id}"
                    if type(failed_variable_id) is int
                    and failed_variable_id > 0
                    and failed_variable_id.bit_length() <= 63
                    else ""
                )
                message = (
                    "IoX configured-target read-only validation failed"
                    f"{variable_label} "
                    f"({labels[category]}); writes remain blocked."
                )
        self._notice("iox_access", message)
        return safe_status

    def refresh_now(self, _command: Any = None) -> bool:
        """Request a complete refresh through the supported node command UI."""

        request = getattr(self.oauth_bridge, "request_refresh", None)
        if (
            not callable(request)
            or not self.started
            or self.stopped
            or not self.config_done
            or self.configuration_status != "valid"
            or self.active_configuration is None
        ):
            self._set_refresh_status("failed")
            self._refresh_notice(
                "Refresh Now is unavailable until startup, configuration, and Google authorization are ready."
            )
            return False
        try:
            outcome = request(self.active_configuration)
            status = outcome.status
        except Exception:
            status = "api_error"
        blocked = {
            "blocked_logging",
            "configuration_required",
            "missing_scopes",
            "authentication_required",
            "api_error",
        }
        if status in blocked:
            self._set_refresh_status("failed")
            self._refresh_notice(
                "Refresh Now could not be requested; the last trusted cache was preserved."
            )
            return False
        self._manual_refresh_pending = True
        self._set_refresh_status("pending")
        self._refresh_notice("Refresh Now requested; Google synchronization is pending.")
        LOGGER.info("Read-only Google Calendar refresh requested")
        return True

    def _refresh_notice(self, message: str) -> None:
        """Publish fixed status text plus a sanitized local synchronization time."""

        if self.last_successful_sync_at is None:
            suffix = "Last successful Google sync: not available."
        else:
            timezone_name = "UTC"
            if self.active_configuration is not None:
                timezone_name = self.active_configuration.calendar.timezone
            try:
                display_zone = ZoneInfo(timezone_name)
            except (ZoneInfoNotFoundError, TypeError, ValueError):
                # Active configuration is normally already validated. Keep a
                # deterministic, sanitized fallback if state is incomplete or
                # unexpectedly incompatible; never echo a raw timezone value.
                display_zone = timezone.utc
            local_time = self.last_successful_sync_at.astimezone(display_zone)
            hour = local_time.hour % 12 or 12
            meridiem = "AM" if local_time.hour < 12 else "PM"
            zone_label = local_time.tzname() or "UTC"
            suffix = (
                "Last successful Google sync: "
                f"{MONTH_ABBREVIATIONS[local_time.month]} {local_time.day}, "
                f"{local_time.year}, {hour}:{local_time:%M:%S} "
                f"{meridiem} {zone_label}."
            )
        self._notice("calendar_refresh", f"{message} {suffix}")

    @property
    def minute_poll_verified(self) -> bool:
        """Whether actual short-poll callbacks demonstrate a minute cadence."""

        return self._minute_poll_verified

    @property
    def iox_writes_allowed(self) -> bool:
        """Whether every runtime gate currently permits trusted writes."""

        return bool(self.write_gate is not None and self.write_gate.enabled)

    def _update_write_gate(self) -> None:
        update = getattr(self.write_gate, "update_runtime", None)
        if callable(update):
            update(
                started=self.started and not self.stopped,
                minute_poll_verified=self._minute_poll_verified,
                last_short_poll_at=self._last_short_poll_at,
            )

    def _observe_short_poll(self) -> None:
        """Verify behavior from callback timestamps, never from setPoll()."""

        try:
            now = float(self._poll_clock())
        except Exception:
            self._last_short_poll_at = None
            self._valid_short_poll_intervals = 0
            self._minute_poll_verified = False
            self._update_write_gate()
            return
        previous = self._last_short_poll_at
        self._last_short_poll_at = now
        if previous is None or now <= previous:
            self._valid_short_poll_intervals = 0
            self._minute_poll_verified = False
            self._update_write_gate()
            self._notice(
                "schedule_poll",
                "Minute polling was requested and is awaiting observed cadence. "
                "IoX writes remain disabled.",
            )
            return
        elapsed = now - previous
        if MIN_SHORT_POLL_SECONDS <= elapsed <= MAX_SHORT_POLL_SECONDS:
            self._valid_short_poll_intervals += 1
        else:
            self._valid_short_poll_intervals = 0
        self._minute_poll_verified = self._valid_short_poll_intervals >= 2
        self._update_write_gate()
        if self._minute_poll_verified:
            self.poly.Notices.delete("schedule_poll")
        elif self._valid_short_poll_intervals:
            self._notice(
                "schedule_poll",
                "Minute polling was requested and is awaiting observed cadence. "
                "IoX writes remain disabled.",
            )
        else:
            self._notice(
                "schedule_poll",
                "Minute polling has not been observed reliably. "
                "IoX writes remain disabled.",
            )

    def _event_preview_poll_handler(self) -> bool:
        """Publish aggregate would-be state counts without writing variables."""

        try:
            outcome = self.oauth_bridge.preview_once(self.active_configuration)
            status = outcome.status
            events = outcome.events_checked
            active = outcome.active_variable_count
            total = outcome.total_variable_count
        except Exception:
            status, events, active, total = "api_error", 0, 0, 0

        if (
            status == "ready"
            and type(events) is int and 0 <= events <= 10000
            and type(total) is int and 0 <= total <= 10000
            and type(active) is int and 0 <= active <= total
        ):
            self.google_auth_status = "preview_ready"
            self._notice(
                "google_auth",
                f"Read-only event preview: {events} events checked; "
                f"{active} of {total} state variables would be ON. "
                "No IoX writes performed.",
            )
            return True

        messages = {
            "blocked_logging": "Google event preview blocked: log level must be Info or higher.",
            "configuration_required": "Google event preview needs valid calendar and rule settings.",
            "missing_scopes": "Google event preview blocked: required read-only scopes are unavailable.",
            "authentication_required": "Google authentication is required before event preview.",
            "untrusted": "Google event preview is untrusted; no state decisions were produced.",
            "api_error": "Google event preview could not complete; no state decisions were produced.",
        }
        self.google_auth_status = "preview_unavailable"
        self._notice(
            "google_auth",
            messages.get(status, "Google event preview is unavailable."),
        )
        return False

    def processConfig(self, config: Any) -> bool:
        self.lifecycle.append("CONFIG")
        if config is None:
            self.configuration = {}
        elif isinstance(config, Mapping):
            self.configuration = dict(config)
        else:
            self._notice("config", "Calendar configuration must be an object.")
            return False
        self.config_received = True
        return True

    def parameterHandler(self, params: Any) -> bool:
        self.lifecycle.append("CUSTOMPARAMS")
        if params is None:
            self.custom_params = {}
        elif isinstance(params, Mapping):
            self.custom_params = dict(params)
        else:
            self._notice("config", "Calendar custom parameters must be an object.")
            return False
        self.custom_params_received = True
        return True

    def typedConfigurationHandler(self, data: Any) -> bool:
        """Validate and activate one complete eisy-ui typed-data payload."""

        self.lifecycle.append("CUSTOMTYPEDDATA")
        self.TypedData.load(data)
        if data is None or data == {}:
            self._configure_iox_targets(frozenset())
            self.active_configuration = None
            self.configuration_status = "not_configured"
            self._set_cache_status("unavailable")
            self._set_evaluation_counts(0, 0)
            self._suspend_schedule()
            self._notice(
                "calendar_config",
                "Add at least one Google calendar and one enabled rule.",
            )
            self._publish_activity_unavailable()
            return False
        try:
            candidate = load_typed_configuration(
                data,
                available_state_variable_ids=self.available_state_variable_ids,
            )
        except ConfigurationError as exc:
            self._configure_iox_targets(frozenset())
            self.active_configuration = None
            self.configuration_status = "invalid"
            self._set_cache_status("unavailable")
            self._set_evaluation_counts(0, 0)
            self._suspend_schedule()
            self._notice(
                "calendar_config",
                f"Calendar configuration is invalid: {exc}",
            )
            self._publish_activity_unavailable()
            return False

        target_ids = frozenset(
            rule.target_variable_id for rule in candidate.calendar.rules
        )
        if not self._configure_iox_targets(target_ids):
            self.active_configuration = None
            self.configuration_status = "invalid"
            self._set_cache_status("unavailable")
            self._set_evaluation_counts(0, 0)
            self._suspend_schedule()
            self._notice(
                "calendar_config",
                "Calendar configuration target validation could not be initialized.",
            )
            self._publish_activity_unavailable()
            return False

        schedule_changed = (
            self.active_configuration is not None
            and (
                candidate.calendar != self.active_configuration.calendar
                or candidate.policy != self.active_configuration.policy
            )
        )
        if schedule_changed:
            invalidate = getattr(self.oauth_bridge, "invalidate_schedule", None)
            if callable(invalidate):
                invalidate()
            self._manual_refresh_pending = False
            self._set_refresh_status("cancelled")
        self.active_configuration = candidate
        self.configuration_status = "valid"
        self._set_cache_status("unavailable")
        self._set_evaluation_counts(0, 0)
        # Remove any title-bearing prior notice synchronously. The next
        # trusted worker result will repopulate it under the new privacy and
        # schedule settings.
        self._publish_activity_awaiting()
        if self.schedule_engine is not None:
            self.schedule_engine.reconfigure(candidate.calendar, candidate.policy)
        self.poly.Notices.delete("calendar_config")
        self._periodic_schedule_handler()
        return True

    def _configure_iox_targets(self, target_ids: frozenset[int]) -> bool:
        configure = getattr(self.iox_read_only_probe, "configure_targets", None)
        if not callable(configure):
            return self.iox_read_only_probe is None
        try:
            configure(target_ids)
            self._update_iox_probe_status()
            return True
        except Exception:
            self._set_iox_status("failed")
            self._notice(
                "iox_access",
                "IoX configured-target read-only validation failed; writes remain blocked.",
            )
            return False

    def _suspend_schedule(self) -> None:
        self._manual_refresh_pending = False
        self._set_refresh_status("cancelled")
        if self.schedule_engine is not None:
            self.schedule_engine.invalidate_configuration()
        invalidate = getattr(self.oauth_bridge, "invalidate_schedule", None)
        if callable(invalidate):
            invalidate()

    def _config_done(self, *_args: Any) -> bool:
        self.lifecycle.append("CONFIGDONE")
        self.config_done = True
        return self._initialize_if_ready()

    def _start_event(self, *_args: Any) -> bool:
        self.lifecycle.append("START")
        self.start_requested = True
        return self._initialize_if_ready()

    def _stop_event(self, *_args: Any) -> bool:
        self.lifecycle.append("STOP")
        self.start_requested = False
        self.config_done = False
        return self.stop()

    def _initialize_if_ready(self) -> bool:
        if not self.start_requested or not self.config_done:
            return True
        if self.started:
            return True
        return self.start()

    def start(self) -> bool:
        """Start with every external integration independently fail-closed."""
        self.lifecycle.append("start")
        if not self.start_requested or not self.config_done:
            self._notice("config", "Calendar configuration is not ready.")
            self.started = False
            return False
        # Polyglot connection health is independent of calendar configuration.
        # Clear the configuration notice only after both payloads validated.
        if self.config_received and self.custom_params_received:
            self.poly.Notices.delete("config")
        if not self._publish_connection_status(True):
            self.started = False
            self.stopped = True
            return False
        self.started = True
        self.stopped = False
        probe_start = getattr(self.iox_read_only_probe, "start", None)
        if callable(probe_start):
            try:
                probe_start()
            except Exception:
                pass
            self._update_iox_probe_status()
        self._update_write_gate()
        if (
            callable(getattr(self.oauth_bridge, "request_schedule_tick", None))
            or callable(getattr(self.oauth_bridge, "schedule_tick", None))
        ):
            try:
                self.poly.setPoll(short=60)
                self._minute_poll_requested = True
                self._notice(
                    "schedule_poll",
                    "Minute polling was requested and is awaiting observed cadence. "
                    "IoX writes remain disabled.",
                )
            except Exception:
                self._minute_poll_requested = False
                self._notice(
                    "schedule_poll",
                    "Minute polling could not be requested. IoX writes remain disabled.",
                )
                LOGGER.warning("Minute schedule polling could not be requested")
            self._minute_poll_verified = False
            self._last_short_poll_at = None
            self._valid_short_poll_intervals = 0
            self._update_write_gate()
            self._periodic_schedule_handler()
        LOGGER.info("Google Calendar controller started in offline-safe mode")
        return True

    def stop(self) -> bool:
        """Close runtime gates and cancel future polling work."""
        self.lifecycle.append("stop")
        status_published = self._publish_connection_status(False)
        self.started = False
        self.stopped = True
        self._minute_poll_requested = False
        self._minute_poll_verified = False
        self._last_short_poll_at = None
        self._valid_short_poll_intervals = 0
        self._manual_refresh_pending = False
        self._set_refresh_status("cancelled")
        probe_stop = getattr(self.iox_read_only_probe, "stop", None)
        if callable(probe_stop):
            try:
                probe_stop()
            except Exception:
                pass
        self._update_write_gate()
        self._update_iox_probe_status()
        invalidate = getattr(self.oauth_bridge, "invalidate_schedule", None)
        if callable(invalidate):
            invalidate()
        LOGGER.info("Google Calendar controller stopped")
        return status_published

    def _publish_connection_status(self, connected: bool) -> bool:
        """Publish the PG3-managed controller connection driver."""
        value = 1 if connected else 0
        try:
            self.setDriver("ST", value, report=True, force=True)
        except Exception:
            LOGGER.exception(
                "Unable to publish CalendarRules connection status ST=%s", value
            )
            self._notice(
                "connection",
                "CalendarRules could not publish its connection status.",
            )
            return False
        self.poly.Notices.delete("connection")
        return True

    def _notice(self, key: str, message: str) -> None:
        self.poly.Notices[key] = message

    commands = {"REFRESH": refresh_now}
