#!/usr/bin/env python3
"""eisy-ui CalendarRules launcher with guarded Google and IoX integration."""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import sys
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import udi_interface

from controller import CalendarController
from oauth_event_bridge import TokenBlindOAuthSink, UdiOAuthEventBridge
from oauth_readonly_discovery import ReadOnlyOAuthDiscoveryProbe
from oauth_event_preview import ReadOnlyOAuthEventPreview
from iox_write_runtime import (
    LIVE_IOX_TRANSPORT_AVAILABLE,
    WriteActivationGate,
    TrustedStateWriter,
)
from iox_transport import (
    BoundedStateVariableTransport,
    ConfiguredTargetRegistry,
    ConfiguredVariablesReadOnlyProbe,
    EisyUiIoxAuthorization,
)
from eisy_google_calendar.google_calendar_http import GoogleCalendarListHttp
from native_loopback_tls import native_loopback_connection
from eisy_google_calendar.snapshot_store import FileSnapshotStorage


LOGGER = logging.getLogger("eisy_google_calendar.plugin")

# Legacy supervised/test modes use owner-only markers; no markers enable
# Google read-only scheduling, never IoX writes. Markers never enter Git.
PROBE_MARKER_FILE = ".oauth-event-probe-enabled"
PROBE_MARKER_CONTENT = b"CALENDARRULES_TOKEN_BLIND_OAUTH_PROBE_V1\n"
READONLY_MARKER_FILE = ".oauth-readonly-discovery-enabled"
READONLY_MARKER_CONTENT = b"CALENDARRULES_READONLY_CALENDAR_LIST_V1\n"
EVENT_PREVIEW_MARKER_FILE = ".oauth-readonly-events-enabled"
EVENT_PREVIEW_MARKER_CONTENT = b"CALENDARRULES_READONLY_EVENTS_PREVIEW_V1\n"
IOX_WRITE_MARKER_FILE = ".iox-state-writes-enabled"
IOX_WRITE_MARKER_CONTENT = b"CALENDARRULES_IOX_STATE_WRITES_V1\n"
# Supervised legacy mode retains its verified cache path for rollback.
CACHE_FILE = ".calendar-cache/schedule.calendar-cache.json"
# Fresh native installs keep their private cache in eisy-ui's backed-up persist tree.
NATIVE_CACHE_FILE = "persist/calendarrules/schedule.calendar-cache.json"
APPROVED_OAUTH_SOURCE_BLOB = "743b5e5fc0c2214c17e1d00e00b79d8a22ddd447"


def _git_blob_id(data: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(data)).encode("ascii") + b"\0" + data
    ).hexdigest()


def _owner_only_marker_valid(path: Path, content: bytes) -> bool:
    """Fail closed on absent, writable-by-others, or symlinked opt-in files."""

    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.geteuid()
            or info.st_size != len(content)
        ):
            return False
        return path.read_bytes() == content
    except (OSError, ValueError):
        return False


def _any_runtime_mode_marker_present(plugin_directory: Path) -> bool:
    """Block default mode on any legacy opt-in path, even if malformed.

    Missing marker => native read-only mode. An unreadable, symlinked,
    or incorrectly owned legacy marker must never silently switch modes.
    """
    for marker in (
        PROBE_MARKER_FILE,
        READONLY_MARKER_FILE,
        EVENT_PREVIEW_MARKER_FILE,
        IOX_WRITE_MARKER_FILE,
    ):
        try:
            (plugin_directory / marker).lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        return True
    return False


def _probe_flag_valid(plugin_directory: Path) -> bool:
    return _owner_only_marker_valid(
        plugin_directory / PROBE_MARKER_FILE, PROBE_MARKER_CONTENT
    )


def _readonly_flag_valid(plugin_directory: Path) -> bool:
    return _owner_only_marker_valid(
        plugin_directory / READONLY_MARKER_FILE, READONLY_MARKER_CONTENT
    )


def _event_preview_flag_valid(plugin_directory: Path) -> bool:
    return _owner_only_marker_valid(
        plugin_directory / EVENT_PREVIEW_MARKER_FILE, EVENT_PREVIEW_MARKER_CONTENT
    )


def _iox_write_flag_valid(plugin_directory: Path) -> bool:
    return _owner_only_marker_valid(
        plugin_directory / IOX_WRITE_MARKER_FILE, IOX_WRITE_MARKER_CONTENT
    )


def _approved_interface() -> bool:
    """Read-only exact-release check. Never import an OAuth helper or data."""

    if getattr(udi_interface, "__version__", None) != "3.4.9":
        return False
    origin = getattr(udi_interface, "__file__", None)
    if not isinstance(origin, str):
        return False
    try:
        module_path = Path(origin).resolve(strict=True)
        if module_path.name != "__init__.py":
            return False
        return (
            _git_blob_id(module_path.with_name("oauth.py").read_bytes())
            == APPROVED_OAUTH_SOURCE_BLOB
        )
    except (OSError, ValueError):
        return False


def _build_test_oauth_bridge(plugin_directory: Path):
    """Enable only token-blind event observation after both safety gates."""

    if _readonly_flag_valid(plugin_directory) or _event_preview_flag_valid(plugin_directory):
        return None  # Each test mode is mutually exclusive.
    if not _probe_flag_valid(plugin_directory):
        return None
    if not _approved_interface():
        LOGGER.warning("OAuth event probe disabled: interface source check failed")
        return None
    return UdiOAuthEventBridge(TokenBlindOAuthSink())


def _safe_runtime_log_level(polyglot) -> bool:
    """Both eisy-ui's effective setting and UD's logger must be INFO+."""

    try:
        level = polyglot.getLogLevel()
        sdk_level = udi_interface.LOGGER.getEffectiveLevel()
        return (
            isinstance(level, str)
            and level.upper() in ("INFO", "WARNING", "ERROR", "CRITICAL")
            and isinstance(sdk_level, int)
            and sdk_level >= logging.INFO
        )
    except Exception:
        return False


def _build_readonly_discovery(plugin_directory: Path, polyglot):
    """Only explicit second opt-in can instantiate the live SDK OAuth helper."""

    if not _readonly_flag_valid(plugin_directory):
        return None
    if _probe_flag_valid(plugin_directory) or _event_preview_flag_valid(plugin_directory):
        LOGGER.warning("Read-only OAuth discovery disabled: multiple mode flags exist")
        return None
    if not _approved_interface():
        LOGGER.warning("Read-only OAuth discovery disabled: SDK source rejected")
        return None
    # SDK OAuth is constructed without loading credentials. Later CUSTOMNS
    # and OAUTH callbacks are refused until INFO+ is confirmed by the bridge.
    sdk_oauth = udi_interface.OAuth(polyglot)
    return ReadOnlyOAuthDiscoveryProbe(
        sdk_oauth,
        safe_logging=lambda: _safe_runtime_log_level(polyglot),
        fetch_page=GoogleCalendarListHttp(),
    )


def _build_event_preview(plugin_directory: Path, polyglot):
    """Only an exclusive, owner-protected third marker can read events."""

    if not _event_preview_flag_valid(plugin_directory):
        return None
    if _probe_flag_valid(plugin_directory) or _readonly_flag_valid(plugin_directory):
        LOGGER.warning("Google event preview disabled: multiple mode flags exist")
        return None
    if not _approved_interface():
        LOGGER.warning("Google event preview disabled: SDK source rejected")
        return None
    safe_logging = lambda: _safe_runtime_log_level(polyglot)
    authorization = EisyUiIoxAuthorization(polyglot)
    targets = ConfiguredTargetRegistry()
    transport = BoundedStateVariableTransport(
        authorization,
        targets,
        plugin_directory=plugin_directory,
    )
    read_only_probe = ConfiguredVariablesReadOnlyProbe(
        authorization,
        transport,
        targets,
    )
    write_gate = WriteActivationGate(
        marker_check=lambda: _iox_write_flag_valid(plugin_directory),
        transport_ready=lambda: (
            LIVE_IOX_TRANSPORT_AVAILABLE and read_only_probe.ready
        ),
        configured_ids=lambda: targets.snapshot().variable_ids,
    )
    writer = TrustedStateWriter(
        transport,
        write_gate,
        safe_logging=safe_logging,
    )
    sdk_oauth = udi_interface.OAuth(polyglot)
    preview = ReadOnlyOAuthEventPreview(
        sdk_oauth,
        safe_logging=safe_logging,
        trusted_state_consumer=writer,
        snapshot_storage=FileSnapshotStorage(plugin_directory / CACHE_FILE),
    )
    preview.write_gate = write_gate
    preview.iox_read_only_probe = read_only_probe
    return preview


def _build_native_readonly_preview(plugin_directory: Path, polyglot):
    """Native store: enabled rules authorize guarded IoX reconciliation.

    No second operation-mode switch or SSH write marker is needed. The
    controller activates only enabled, validated rule targets; the existing
    trusted-schedule, owner-authorized type-2 probe, safe logging, minute
    cadence, and checked read/compare/write/readback gates still apply.
    Legacy supervised mode retains its explicit marker and verified TLS.
    """
    if _any_runtime_mode_marker_present(plugin_directory):
        return None
    if not _approved_interface():
        LOGGER.warning("CalendarRules native startup disabled: SDK source rejected")
        return None
    safe_logging = lambda: _safe_runtime_log_level(polyglot)
    authorization = EisyUiIoxAuthorization(polyglot)
    targets = ConfiguredTargetRegistry()
    transport = BoundedStateVariableTransport(
        authorization,
        targets,
        connection_factory=native_loopback_connection,
        native_writable=True,
    )
    probe = ConfiguredVariablesReadOnlyProbe(
        authorization, transport, targets
    )
    # For a native store install, 'Enabled' on a validated rule is the
    # deliberate action. No store ZIP or install hook creates an activation
    # marker, and an empty/disabled target set never opens this gate.
    write_gate = WriteActivationGate(
        marker_check=lambda: bool(targets.snapshot().variable_ids),
        transport_ready=lambda: (
            LIVE_IOX_TRANSPORT_AVAILABLE and probe.ready
        ),
        configured_ids=lambda: targets.snapshot().variable_ids,
    )
    writer = TrustedStateWriter(
        transport,
        write_gate,
        safe_logging=safe_logging,
    )
    preview = ReadOnlyOAuthEventPreview(
        udi_interface.OAuth(polyglot),
        safe_logging=safe_logging,
        trusted_state_consumer=writer,
        snapshot_storage=FileSnapshotStorage(plugin_directory / NATIVE_CACHE_FILE),
    )
    preview.write_gate = write_gate
    preview.iox_read_only_probe = probe
    return preview


def _publish_static_profile(polyglot) -> bool:
    """Ask eisy-ui to install this release's static profile after readiness."""

    update_profile = getattr(polyglot, "updateProfile", None)
    if not callable(update_profile):
        LOGGER.warning("IoX profile publication is unavailable")
        return False
    try:
        update_profile()
    except Exception:
        LOGGER.warning("IoX profile publication request failed")
        return False
    LOGGER.info("IoX profile publication requested")
    return True


def main() -> None:
    polyglot = udi_interface.Interface([])
    preview = _build_event_preview(PLUGIN_DIR, polyglot)
    discovery = _build_readonly_discovery(PLUGIN_DIR, polyglot)
    legacy_probe = _build_test_oauth_bridge(PLUGIN_DIR)
    bridge = (
        preview if preview is not None
        else discovery if discovery is not None
        else legacy_probe if legacy_probe is not None
        else _build_native_readonly_preview(PLUGIN_DIR, polyglot)
    )
    controller_kwargs = {"oauth_bridge": bridge}
    # The selected bridge may be the native SDK-loopback preview, not the
    # legacy marker-gated preview. Both present their own optional probe.
    write_gate = getattr(bridge, "write_gate", None)
    if write_gate is not None:
        controller_kwargs["write_gate"] = write_gate
    read_only_probe = getattr(bridge, "iox_read_only_probe", None)
    if read_only_probe is not None:
        controller_kwargs["iox_read_only_probe"] = read_only_probe
    controller = CalendarController(polyglot, **controller_kwargs)
    polyglot.start({"version": "0.4.11", "requestId": False})
    polyglot.ready()
    _publish_static_profile(polyglot)
    controller.register_node()
    polyglot.runForever()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        LOGGER.info("exiting")
