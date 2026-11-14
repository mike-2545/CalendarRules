"""Offline checks for the first token-blind eisy OAuth integration test."""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


PLUGIN = Path(__file__).resolve().parents[1] / "plugin"


def load_launcher(monkeypatch, tmp_path):
    """Load main without importing the real UD interface or accessing a token."""

    source = tmp_path / "udi_interface"
    source.mkdir()
    (source / "__init__.py").write_text("__version__ = '3.4.9'\n")
    (source / "oauth.py").write_bytes(b"synthetic, non-secret test content")
    (source / "isy.py").write_bytes(b"synthetic, non-secret ISY helper")

    events = []

    class FakeInterface:
        ISY = "ISY"

        def __init__(self):
            self.subscriptions = []
            self.sent = []

        def subscribe(self, topic, callback, address=None):
            self.subscriptions.append((topic, callback, address))

        def send(self, message, destination):
            self.sent.append((message, destination))

        def start(self, _options):
            events.append("start")

        def ready(self):
            events.append("ready")

        def runForever(self):
            events.append("runForever")

    fake_udi = SimpleNamespace(
        __version__="3.4.9",
        __file__=str(source / "__init__.py"),
        Interface=lambda _args: FakeInterface(),
    )

    def build_controller(poly, *, oauth_bridge=None, **kwargs):
        events.append(("controller", oauth_bridge))
        controller = SimpleNamespace(register_node=lambda: events.append("register_node"))
        controller.options = kwargs
        return controller

    monkeypatch.setitem(sys.modules, "udi_interface", fake_udi)
    monkeypatch.setitem(
        sys.modules, "controller",
        SimpleNamespace(CalendarController=build_controller),
    )

    spec = importlib.util.spec_from_file_location(
        "calendar_test_oauth_probe_launcher", PLUGIN / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.PLUGIN_DIR = tmp_path
    return module, fake_udi, events


def test_probe_is_disabled_without_an_explicit_marker(monkeypatch, tmp_path):
    launcher, _, events = load_launcher(monkeypatch, tmp_path)
    launcher.main()
    assert events == [
        ("controller", None), "start", "ready", "register_node", "runForever"
    ]
    assert launcher._probe_flag_valid(tmp_path) is False


def test_probe_marker_requires_exact_bytes_owner_and_permissions(monkeypatch, tmp_path):
    launcher, _, _ = load_launcher(monkeypatch, tmp_path)
    flag = tmp_path / launcher.PROBE_MARKER_FILE
    flag.write_bytes(launcher.PROBE_MARKER_CONTENT)
    flag.chmod(0o600)
    assert launcher._probe_flag_valid(tmp_path) is True

    flag.chmod(0o644)
    assert launcher._probe_flag_valid(tmp_path) is False
    flag.chmod(0o600)
    flag.write_bytes(launcher.PROBE_MARKER_CONTENT + b"extra")
    assert launcher._probe_flag_valid(tmp_path) is False
    flag.write_bytes(b"invalid")
    assert launcher._probe_flag_valid(tmp_path) is False
    flag.write_bytes(launcher.PROBE_MARKER_CONTENT)
    monkeypatch.setattr(launcher.os, "geteuid", lambda: os.getuid() + 100000)
    assert launcher._probe_flag_valid(tmp_path) is False


def test_probe_marker_symlink_is_rejected(monkeypatch, tmp_path):
    launcher, _, _ = load_launcher(monkeypatch, tmp_path)
    payload = tmp_path / "payload.txt"
    payload.write_bytes(launcher.PROBE_MARKER_CONTENT)
    payload.chmod(0o600)
    (tmp_path / launcher.PROBE_MARKER_FILE).symlink_to(payload)
    assert launcher._probe_flag_valid(tmp_path) is False


def test_interface_source_is_pinned_to_exact_approved_build(monkeypatch, tmp_path):
    launcher, interface, _ = load_launcher(monkeypatch, tmp_path)
    oauth = Path(interface.__file__).with_name("oauth.py")
    good_blob = launcher._git_blob_id(oauth.read_bytes())
    assert len(good_blob) == 40
    monkeypatch.setattr(launcher, "APPROVED_OAUTH_SOURCE_BLOB", good_blob)
    assert launcher._approved_interface() is True

    interface.__version__ = "3.4.8"
    assert launcher._approved_interface() is False
    interface.__version__ = "3.4.9"
    oauth.write_bytes(b"tampered version")
    assert launcher._approved_interface() is False
    oauth.unlink()
    assert launcher._approved_interface() is False


def test_valid_opt_in_instantiates_only_token_blind_probe(monkeypatch, tmp_path):
    launcher, interface, events = load_launcher(monkeypatch, tmp_path)
    flag = tmp_path / launcher.PROBE_MARKER_FILE
    flag.write_bytes(launcher.PROBE_MARKER_CONTENT)
    flag.chmod(0o600)

    oauth = Path(interface.__file__).with_name("oauth.py")
    monkeypatch.setattr(
        launcher, "APPROVED_OAUTH_SOURCE_BLOB",
        launcher._git_blob_id(oauth.read_bytes())
    )

    launcher.main()
    bridge = events[0][1]
    assert bridge is not None
    assert type(bridge._sdk_oauth).__name__ == "TokenBlindOAuthSink"
    assert not hasattr(interface, "OAuth")  # Cannot instantiate SDK OAuth.
    assert bridge.custom_namespace("oauth", {"client_secret": "PRIVATE"}) is True
    assert bridge.authorization_event({"access_token": "PRIVATE"}) is True
    assert "PRIVATE" not in repr(bridge.__dict__)
    assert events[1:] == ["start", "ready", "register_node", "runForever"]


def test_invalid_source_keeps_oauth_events_disabled(monkeypatch, tmp_path):
    launcher, _, events = load_launcher(monkeypatch, tmp_path)
    flag = tmp_path / launcher.PROBE_MARKER_FILE
    flag.write_bytes(launcher.PROBE_MARKER_CONTENT)
    flag.chmod(0o600)
    launcher.main()
    assert events[0] == ("controller", None)


def test_token_blind_probe_never_requests_calendar_or_iox_access():
    source = "\n".join(
        (PLUGIN / name).read_text()
        for name in ("main.py", "oauth_event_bridge.py")
    )
    for disallowed in (
        "getAccessToken(", "googleapiclient",
        "requests.get", "requests.post", "write_state_variable",
        "reconcile_snapshot", "IoX(",
    ):
        assert disallowed not in source
    # The SDK helper is now allowed only inside the second guarded mode.
    assert "udi_interface.OAuth(polyglot)" in (PLUGIN / "main.py").read_text()
    assert "_readonly_flag_valid(plugin_directory)" in source



def test_live_readonly_marker_never_activates_without_explicit_approval(monkeypatch, tmp_path):
    launcher, interface, events = load_launcher(monkeypatch, tmp_path)
    calls = []

    class FakeSdkOAuth:
        def __init__(self, polyglot):
            calls.append("constructed")

    interface.OAuth = FakeSdkOAuth
    # No marker: do not construct the helper.
    launcher.main()
    assert calls == []
    assert events[0] == ("controller", None)


def test_second_marker_is_owner_only_and_cannot_enable_both_modes(monkeypatch, tmp_path):
    launcher, interface, events = load_launcher(monkeypatch, tmp_path)
    readonly = tmp_path / launcher.READONLY_MARKER_FILE
    readonly.write_bytes(launcher.READONLY_MARKER_CONTENT)
    readonly.chmod(0o644)
    assert launcher._readonly_flag_valid(tmp_path) is False
    launcher.main()
    assert events[0] == ("controller", None)

    readonly.chmod(0o600)
    assert launcher._readonly_flag_valid(tmp_path) is True
    blind = tmp_path / launcher.PROBE_MARKER_FILE
    blind.write_bytes(launcher.PROBE_MARKER_CONTENT)
    blind.chmod(0o600)
    assert launcher._build_readonly_discovery(tmp_path, object()) is None
    assert launcher._build_test_oauth_bridge(tmp_path) is None


def test_live_probe_instantiates_sdk_only_with_valid_marker_and_interface(monkeypatch, tmp_path):
    launcher, interface, events = load_launcher(monkeypatch, tmp_path)
    sdk_calls = []
    class FakeSdkOAuth:
        def __init__(self, poly):
            sdk_calls.append("constructed")

    interface.OAuth = FakeSdkOAuth
    interface.LOGGER = SimpleNamespace(getEffectiveLevel=lambda: 20)

    class FakePoly:
        def getLogLevel(self):
            return "INFO"

        def start(self, _options):
            pass

        def ready(self):
            pass

        def runForever(self):
            pass

    instance = FakePoly()
    interface.Interface = lambda _args: instance
    readonly = tmp_path / launcher.READONLY_MARKER_FILE
    readonly.write_bytes(launcher.READONLY_MARKER_CONTENT)
    readonly.chmod(0o600)
    oauth = Path(interface.__file__).with_name("oauth.py")

    # A matching marker with an unapproved source must not instantiate OAuth.
    launcher.main()
    assert sdk_calls == []
    assert events[0] == ("controller", None)

    monkeypatch.setattr(
        launcher, "APPROVED_OAUTH_SOURCE_BLOB",
        launcher._git_blob_id(oauth.read_bytes()),
    )
    assert launcher._safe_runtime_log_level(instance) is True
    launcher.main()
    assert sdk_calls == ["constructed"]
    controller_events = [value for value in events if isinstance(value, tuple) and value[0] == "controller"]
    assert len(controller_events) == 2
    assert type(controller_events[-1][1]).__name__ == "ReadOnlyOAuthDiscoveryProbe"
    assert type(controller_events[-1][1]).__name__ != "UdiOAuthEventBridge"


def test_runtime_logger_checks_both_ud_and_eisyui_level(monkeypatch, tmp_path):
    launcher, interface, _ = load_launcher(monkeypatch, tmp_path)
    value = ["INFO"]
    level = [20]
    interface.LOGGER = SimpleNamespace(getEffectiveLevel=lambda: level[0])
    poly = SimpleNamespace(getLogLevel=lambda: value[0])
    assert launcher._safe_runtime_log_level(poly) is True
    value[0] = "DEBUG"
    assert launcher._safe_runtime_log_level(poly) is False
    value[0] = "INFO"
    level[0] = 10
    assert launcher._safe_runtime_log_level(poly) is False
    level[0] = 30
    assert launcher._safe_runtime_log_level(poly) is True


def test_readonly_marker_symlink_is_rejected(monkeypatch, tmp_path):
    launcher, _, _ = load_launcher(monkeypatch, tmp_path)
    target = tmp_path / "ownerfile"
    target.write_bytes(launcher.READONLY_MARKER_CONTENT)
    target.chmod(0o600)
    (tmp_path / launcher.READONLY_MARKER_FILE).symlink_to(target)
    assert launcher._readonly_flag_valid(tmp_path) is False


def test_event_preview_marker_is_separate_owner_only_and_fail_closed(monkeypatch, tmp_path):
    launcher, interface, events = load_launcher(monkeypatch, tmp_path)
    marker = tmp_path / launcher.EVENT_PREVIEW_MARKER_FILE
    assert launcher._event_preview_flag_valid(tmp_path) is False
    marker.write_bytes(launcher.EVENT_PREVIEW_MARKER_CONTENT)
    marker.chmod(0o644)
    assert launcher._event_preview_flag_valid(tmp_path) is False
    marker.chmod(0o600)
    assert launcher._event_preview_flag_valid(tmp_path) is True
    marker.write_bytes(b"wrong content")
    assert launcher._event_preview_flag_valid(tmp_path) is False


def test_event_preview_requires_exact_udi_source_and_exclusive_marker(monkeypatch, tmp_path):
    launcher, interface, events = load_launcher(monkeypatch, tmp_path)
    sdk_created = []
    interface.OAuth = lambda poly: sdk_created.append(True) or object()
    event_marker = tmp_path / launcher.EVENT_PREVIEW_MARKER_FILE
    event_marker.write_bytes(launcher.EVENT_PREVIEW_MARKER_CONTENT)
    event_marker.chmod(0o600)

    # Real release's source identity is mandatory.
    launcher.main()
    assert sdk_created == []
    assert events[0] == ("controller", None)

    approved_blob = launcher._git_blob_id(
        Path(interface.__file__).with_name("oauth.py").read_bytes()
    )
    monkeypatch.setattr(launcher, "APPROVED_OAUTH_SOURCE_BLOB", approved_blob)
    launcher.main()
    assert sdk_created == [True]
    controllers = [value for value in events if isinstance(value, tuple) and value[0] == "controller"]
    assert type(controllers[-1][1]).__name__ == "ReadOnlyOAuthEventPreview"
    assert controllers[-1][1]._snapshot_storage.path == tmp_path / launcher.CACHE_FILE

    # A second active mode disables the event preview entirely.
    second_marker = tmp_path / launcher.READONLY_MARKER_FILE
    second_marker.write_bytes(launcher.READONLY_MARKER_CONTENT)
    second_marker.chmod(0o600)
    launcher.main()
    assert sdk_created == [True]
    controllers = [value for value in events if isinstance(value, tuple) and value[0] == "controller"]
    assert controllers[-1][1] is None


def test_event_preview_marker_symlink_is_rejected(monkeypatch, tmp_path):
    launcher, _, _ = load_launcher(monkeypatch, tmp_path)
    target = tmp_path / "marker-content"
    target.write_bytes(launcher.EVENT_PREVIEW_MARKER_CONTENT)
    target.chmod(0o600)
    (tmp_path / launcher.EVENT_PREVIEW_MARKER_FILE).symlink_to(target)
    assert launcher._event_preview_flag_valid(tmp_path) is False


def test_write_marker_is_additional_owner_only_gate_and_probe_starts_unready(
    monkeypatch, tmp_path
):
    launcher, interface, _events = load_launcher(monkeypatch, tmp_path)
    write_marker = tmp_path / launcher.IOX_WRITE_MARKER_FILE
    write_marker.write_bytes(launcher.IOX_WRITE_MARKER_CONTENT)
    write_marker.chmod(0o600)
    assert launcher._iox_write_flag_valid(tmp_path) is True

    # The write marker alone cannot activate Google or IoX access.
    assert launcher._build_event_preview(tmp_path, object()) is None

    event_marker = tmp_path / launcher.EVENT_PREVIEW_MARKER_FILE
    event_marker.write_bytes(launcher.EVENT_PREVIEW_MARKER_CONTENT)
    event_marker.chmod(0o600)
    monkeypatch.setattr(
        launcher,
        "APPROVED_OAUTH_SOURCE_BLOB",
        launcher._git_blob_id(Path(interface.__file__).with_name("oauth.py").read_bytes()),
    )
    interface.OAuth = lambda _poly: object()
    interface.LOGGER = SimpleNamespace(getEffectiveLevel=lambda: 20)
    poly = SimpleNamespace(
        ISY="ISY",
        subscriptions=[],
        messages=[],
        getLogLevel=lambda: "INFO",
    )
    poly.subscribe = lambda *args: poly.subscriptions.append(args)
    poly.send = lambda *args: poly.messages.append(args)

    preview = launcher._build_event_preview(tmp_path, poly)
    assert preview.write_gate is not None
    assert preview.write_gate.enabled is False
    assert launcher.LIVE_IOX_TRANSPORT_AVAILABLE is True
    assert preview.iox_read_only_probe.ready is False
    assert poly.subscriptions[0][0] == "ISY"
    assert poly.messages == [({"getIsyInfo": {}}, "system")]


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner and mode validation")
def test_legacy_write_marker_requires_exact_bytes_and_owner_permissions(
    monkeypatch, tmp_path
):
    # Compatibility test for old supervised installations; native 0.4.10
    # does not use a marker, and should not need a private deploy runbook.
    launcher, _interface, _events = load_launcher(monkeypatch, tmp_path)
    marker = tmp_path / launcher.IOX_WRITE_MARKER_FILE
    marker.write_bytes(launcher.IOX_WRITE_MARKER_CONTENT)
    marker.chmod(0o600)
    metadata = marker.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_uid == os.geteuid()
    assert launcher._iox_write_flag_valid(tmp_path) is True

    marker.write_bytes(launcher.IOX_WRITE_MARKER_CONTENT.rstrip(b"\n"))
    marker.chmod(0o600)
    assert launcher._iox_write_flag_valid(tmp_path) is False


def test_native_fresh_install_constructs_google_only_preview_after_sdk_approval(
    monkeypatch, tmp_path
):
    launcher, interface, events = load_launcher(monkeypatch, tmp_path)
    oauth_created = []

    class FakeSdkOAuth:
        def __init__(self, poly):
            oauth_created.append(poly)

        def getAccessToken(self):
            raise AssertionError("fresh startup must not access Google token")

    interface.OAuth = FakeSdkOAuth
    # A valid installed SDK is mandatory even when no opt-in file exists.
    assert launcher._build_native_readonly_preview(tmp_path, object()) is None
    assert oauth_created == []

    oauth = Path(interface.__file__).with_name("oauth.py")
    monkeypatch.setattr(
        launcher,
        "APPROVED_OAUTH_SOURCE_BLOB",
        launcher._git_blob_id(oauth.read_bytes()),
    )
    launcher.main()

    assert len(oauth_created) == 1
    bridge = events[0][1]
    assert type(bridge).__name__ == "ReadOnlyOAuthEventPreview"
    assert bridge._trusted_state_consumer is not None
    assert bridge.write_gate.enabled is False
    assert bridge.iox_read_only_probe is not None
    assert bridge.iox_read_only_probe.ready is False
    assert bridge.iox_read_only_probe._transport._native_writable is True
    assert bridge.iox_read_only_probe._transport._native_loopback is True
    assert bridge._snapshot_storage.path == tmp_path / launcher.NATIVE_CACHE_FILE
    assert not (tmp_path / launcher.CACHE_FILE).exists()
    # Native reconciliation cannot write during startup. It requests only the
    # supported owner IoX grant and waits for enabled rules, authorized type-2
    # validation, trusted schedule and observed minute polling.
    assert oauth_created[0].sent == [({"getIsyInfo": {}}, "system")]
    assert [item[0] for item in oauth_created[0].subscriptions] == ["ISY"]
    assert not list(tmp_path.glob(".iox-state-writes-enabled*"))
    assert events[1:] == ["start", "ready", "register_node", "runForever"]


@pytest.mark.parametrize(
    "marker_constant",
    [
        "PROBE_MARKER_FILE",
        "READONLY_MARKER_FILE",
        "EVENT_PREVIEW_MARKER_FILE",
        "IOX_WRITE_MARKER_FILE",
    ],
)
def test_native_default_does_not_bypass_any_existing_invalid_marker(
    monkeypatch, tmp_path, marker_constant
):
    launcher, interface, events = load_launcher(monkeypatch, tmp_path)
    oauth = Path(interface.__file__).with_name("oauth.py")
    monkeypatch.setattr(
        launcher,
        "APPROVED_OAUTH_SOURCE_BLOB",
        launcher._git_blob_id(oauth.read_bytes()),
    )
    interface.OAuth = lambda _poly: (_ for _ in ()).throw(
        AssertionError("invalid marker must block default OAuth")
    )
    malformed = tmp_path / getattr(launcher, marker_constant)
    malformed.write_bytes(b"bad or tampered runtime marker")
    malformed.chmod(0o644)
    assert launcher._any_runtime_mode_marker_present(tmp_path) is True
    launcher.main()
    assert events[0] == ("controller", None)


def test_native_default_rejects_symlinked_legacy_marker(monkeypatch, tmp_path):
    launcher, interface, events = load_launcher(monkeypatch, tmp_path)
    oauth = Path(interface.__file__).with_name("oauth.py")
    monkeypatch.setattr(
        launcher,
        "APPROVED_OAUTH_SOURCE_BLOB",
        launcher._git_blob_id(oauth.read_bytes()),
    )
    interface.OAuth = lambda _poly: (_ for _ in ()).throw(
        AssertionError("symlinked marker must block default OAuth")
    )
    payload = tmp_path / "untrusted-payload"
    payload.write_bytes(b"not a marker")
    (tmp_path / launcher.EVENT_PREVIEW_MARKER_FILE).symlink_to(payload)
    assert launcher._any_runtime_mode_marker_present(tmp_path) is True
    launcher.main()
    assert events[0] == ("controller", None)
