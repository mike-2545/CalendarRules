"""Offline native-store first-install smoke tests.

These tests exercise the *packaged files* extracted into a fresh, arbitrary
plugin home. SDK/eisy-ui lifecycle events are faked: no credentials, network
connection, installed device, or IoX write is used. Passing these checks does
not certify a live eisy-ui store installation.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "scripts" / "build_store_package.py"


def _builder():
    spec = importlib.util.spec_from_file_location(
        "calendar_rules_native_store_builder", BUILDER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def fresh_native_home(tmp_path: Path) -> Path:
    """Simulate a first install, with no supervised home-eisy environment."""

    archive, _manifest = _builder().build(ROOT, tmp_path / "package")
    destination = tmp_path / "new-user" / "store-nsid-unknown" / "slot-27"
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(destination)
    return destination


def _run_isolated(home: Path, script: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(home)],
        cwd=home,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, (
        "Synthetic fresh-install process failed: "
        + result.stderr[-1600:].replace("SECRET-SDK-VALUE", "[redacted]")
    )
    return json.loads(result.stdout.strip())


def test_fresh_store_home_has_no_supervised_activation_or_private_state(
    fresh_native_home: Path,
) -> None:
    home = fresh_native_home
    metadata = json.loads(
        (home / "eisy_google_calendar.iox_plugin.json").read_text(encoding="utf-8")
    )
    assert metadata["plugin"]["version"] == "0.4.12"
    assert metadata["plugin"]["installScript"] == "install.sh"
    assert metadata["plugin"]["executableName"] == "main.py"
    assert metadata["plugin"]["enableOAUTH2"] is True
    assert metadata["plugin"]["requiresIoXAccess"] is True

    for relative in (
        ".oauth-event-probe-enabled",
        ".oauth-readonly-discovery-enabled",
        ".oauth-readonly-events-enabled",
        ".iox-state-writes-enabled",
        ".calendar-cache",
        ".iox-trust",
        "config",
        "persist",
        "data",
    ):
        assert not (home / relative).exists(), relative
    assert not (home / "PACKAGE_MANIFEST.sha256").is_dir()
    assert (home / "PACKAGE_MANIFEST.sha256").is_file()


def test_unapproved_sdk_cannot_activate_packaged_google_event_mode(
    fresh_native_home: Path,
) -> None:
    # Run the actual packaged main.py in a separate Python process with only
    # a synthetic host SDK. This does not use the developer source checkout.
    evidence = _run_isolated(
        fresh_native_home,
        r'''
import importlib
import json
import logging
from pathlib import Path
import sys
from types import ModuleType

home = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(home))
events = []

fake_sdk = ModuleType("udi_interface")
fake_sdk.__version__ = "3.4.9"
fake_sdk.LOGGER = logging.getLogger("simulated-udi")

class HostInterface:
    def start(self, identity):
        events.append(("start", identity))
    def ready(self):
        events.append("ready")
    def updateProfile(self):
        events.append("updateProfile")
    def runForever(self):
        events.append("runForever")

fake_sdk.Interface = lambda args: HostInterface() if args == [] else None
sys.modules["udi_interface"] = fake_sdk

fake_controller = ModuleType("controller")
class StubController:
    def __init__(self, poly, **kwargs):
        assert kwargs == {"oauth_bridge": None}
        events.append("controller")
    def register_node(self):
        events.append("register_node")
fake_controller.CalendarController = StubController
sys.modules["controller"] = fake_controller

launcher = importlib.import_module("main")
assert launcher.PLUGIN_DIR == home
assert not launcher._event_preview_flag_valid(home)
assert not launcher._readonly_flag_valid(home)
assert not launcher._probe_flag_valid(home)
assert not launcher._iox_write_flag_valid(home)
launcher.main()

assert events == [
    "controller",
    ("start", {"version": "0.4.12", "requestId": False}),
    "ready",
    "updateProfile",
    "register_node",
    "runForever",
]
print(json.dumps({
    "plugin_path_derived": True,
    "host_lifecycle_stub_completed": True,
    "google_event_bridge": "disabled_without_runtime_marker",
    "iox_writes": "disabled",
}))
''',
    )
    assert evidence == {
        "plugin_path_derived": True,
        "host_lifecycle_stub_completed": True,
        "google_event_bridge": "disabled_without_runtime_marker",
        "iox_writes": "disabled",
    }


def test_first_install_has_no_trusted_cache_or_device_tls_anchor(
    fresh_native_home: Path,
) -> None:
    evidence = _run_isolated(
        fresh_native_home,
        r'''
import json
from pathlib import Path
import sys

home = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(home))
from eisy_google_calendar.snapshot_store import FileSnapshotStorage
from iox_transport import IoxTransportError, _verified_loopback_context

cache_path = home / ".calendar-cache" / "schedule.calendar-cache.json"
assert FileSnapshotStorage(cache_path).load() is None
assert not cache_path.exists()

try:
    _verified_loopback_context(home)
except IoxTransportError as exc:
    assert exc.category == "trust-anchor"
    trust = "missing_anchor_fails_closed"
else:
    raise AssertionError("fresh native install unexpectedly enabled IoX TLS")

print(json.dumps({"trusted_cache": "absent", "iox_tls": trust}))
''',
    )
    assert evidence == {
        "trusted_cache": "absent",
        "iox_tls": "missing_anchor_fails_closed",
    }


def test_packaged_native_boot_enables_google_only_with_approved_sdk(
    fresh_native_home: Path,
) -> None:
    # Simulate a byte-for-byte approved SDK, not a real eisy-ui service.
    # The expected blob is patched in the isolated process for synthetic
    # fixture bytes. SDK/eisy-ui source approval remains mandatory in runtime.
    evidence = _run_isolated(
        fresh_native_home,
        r'''
import importlib
import json
import logging
from pathlib import Path
import sys
from types import ModuleType

home = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(home))
sdk_root = home.parent / "fake-udi-interface"
sdk_root.mkdir()
(sdk_root / "__init__.py").write_text("__version__ = '3.4.9'\n")
(sdk_root / "oauth.py").write_bytes(b"synthetic SDK OAuth code for offline test")
events = []

sdk = ModuleType("udi_interface")
sdk.__version__ = "3.4.9"
sdk.__file__ = str(sdk_root / "__init__.py")
sdk.LOGGER = logging.getLogger("native-test-sdk")
sdk.LOGGER.setLevel(logging.INFO)
class FakeOAuth:
    def __init__(self, poly):
        events.append("OAuth constructed")
    def getAccessToken(self):
        raise AssertionError("token read before valid owner config and grant")
sdk.OAuth = FakeOAuth

class FakeHost:
    ISY = "ISY"
    def __init__(self):
        self.sent = []
        self.subscriptions = []
    def subscribe(self, topic, callback):
        self.subscriptions.append(topic)
    def send(self, message, destination):
        self.sent.append((message, destination))
    def getLogLevel(self):
        return "INFO"
    def start(self, options):
        events.append(("start", options))
    def ready(self):
        events.append("ready")
    def updateProfile(self):
        events.append("updateProfile")
    def runForever(self):
        events.append("runForever")

sdk.Interface = lambda args: FakeHost() if args == [] else None
sys.modules["udi_interface"] = sdk
controller = ModuleType("controller")
class FakeController:
    def __init__(self, poly, **kwargs):
        bridge = kwargs.get("oauth_bridge")
        assert type(bridge).__name__ == "ReadOnlyOAuthEventPreview"
        assert bridge._trusted_state_consumer is not None
        assert bridge._snapshot_storage.path == home / "persist" / "calendarrules" / "schedule.calendar-cache.json"
        assert not (home / ".calendar-cache").exists()
        gate = kwargs.get("write_gate")
        assert gate is bridge.write_gate
        assert gate.enabled is False
        assert gate.permits({38: 1}) is False
        probe = kwargs.get("iox_read_only_probe")
        assert probe is not None
        assert probe.ready is False
        assert poly.sent == [({"getIsyInfo": {}}, "system")]
        assert poly.subscriptions == ["ISY"]
        assert probe._transport._native_writable is True
        assert probe._transport._native_loopback is True
        events.append("Google and enabled-rule IoX controller")
    def register_node(self):
        events.append("register_node")
controller.CalendarController = FakeController
sys.modules["controller"] = controller

launcher = importlib.import_module("main")
launcher.APPROVED_OAUTH_SOURCE_BLOB = launcher._git_blob_id(
    (sdk_root / "oauth.py").read_bytes()
)
assert launcher._any_runtime_mode_marker_present(home) is False
launcher.main()
assert events == [
    "OAuth constructed",
    "Google and enabled-rule IoX controller",
    ("start", {"version": "0.4.12", "requestId": False}),
    "ready", "updateProfile", "register_node", "runForever",
]
assert not (home / ".iox-state-writes-enabled").exists()
assert not (home / ".iox-trust").exists()
print(json.dumps({"approved_sdk": True, "google_readonly": True, "iox": "grant_and_rules_required"}))
''',
    )
    assert evidence == {
        "approved_sdk": True,
        "google_readonly": True,
        "iox": "grant_and_rules_required",
    }
