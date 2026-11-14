import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"
CORE = ROOT / "src" / "eisy_google_calendar"
DEPLOYED_CORE = PLUGIN / "eisy_google_calendar"


def test_required_plugin_files_exist():
    for name in (
        "main.py",
        "controller.py",
        "eisy_google_calendar.iox_plugin.json",
        "requirements.txt",
        "install.sh",
        "POLYGLOT_CONFIG.md",
        "README.md",
    ):
        assert (PLUGIN / name).is_file(), name


def test_core_package_is_self_contained_and_matches_source():
    source_files = sorted(p.name for p in CORE.glob("*.py"))
    deployed_files = sorted(p.name for p in DEPLOYED_CORE.glob("*.py"))
    assert deployed_files == source_files
    for name in source_files:
        assert hashlib.sha256((DEPLOYED_CORE / name).read_bytes()).digest() == hashlib.sha256((CORE / name).read_bytes()).digest()


def test_launcher_imports_only_deployed_package_location():
    source = (PLUGIN / "main.py").read_text()
    assert "EISY_GOOGLE_CALENDAR_SOURCE" not in source
    assert "PLUGIN_DIR / \"src\"" not in source
    assert "sys.path.insert(0, str(PLUGIN_DIR))" in source


def test_isolated_deployment_imports_core_without_workspace_or_env(tmp_path):
    import os
    import subprocess
    import sys

    deployed = tmp_path / "plugin"
    deployed.mkdir()
    for name in ("main.py", "controller.py"):
        (deployed / name).write_bytes((PLUGIN / name).read_bytes())
    target_core = deployed / "eisy_google_calendar"
    target_core.mkdir()
    for core_file in DEPLOYED_CORE.glob("*.py"):
        (target_core / core_file.name).write_bytes(core_file.read_bytes())
    env = os.environ.copy()
    env.pop("EISY_GOOGLE_CALENDAR_SOURCE", None)
    env["PYTHONPATH"] = ""
    code = "import sys; sys.path.insert(0, sys.argv[1]); import eisy_google_calendar; print(eisy_google_calendar.__file__)"
    result = subprocess.run(
        [sys.executable, "-c", code, str(deployed)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert str(deployed / "eisy_google_calendar") in result.stdout


def test_install_script_is_service_account_scoped_and_location_independent():
    source = (PLUGIN / "install.sh").read_text()
    assert "python3 -m pip install" in source
    assert "--user" in source
    assert '"$script_dir/requirements.txt"' in source
    assert "register" not in source.lower()
    assert "curl" not in source
    assert "wget" not in source
    assert "sudo" not in source.lower()
    assert "pkg install" not in source.lower()


def test_requirements_enforce_eisy_ui_routing_interface():
    assert (PLUGIN / "requirements.txt").read_text().strip() == "udi_interface==3.4.9"
    metadata = json.loads((PLUGIN / "eisy_google_calendar.iox_plugin.json").read_text())
    assert metadata["plugin"]["requirements"] == []
    assert metadata["plugin"]["enableOAUTH2"] is True
    assert metadata["plugin"]["requiresIoXAccess"] is True
    readme = (PLUGIN / "README.md").read_text()
    assert "legacy PG3x routing identity" in readme
    assert "plugin_<slot>" in readme


def test_smoke_and_rollback_documented_without_execution_instructions_for_this_run():
    readme = (PLUGIN / "README.md").read_text()
    assert "Planned smoke test" in readme
    assert "Rollback" in readme
    assert "self-contained" in readme
    assert "/var/eisyui/ns/plugin_<slot>" in readme


def test_controller_status_lifecycle_is_documented():
    readme = (PLUGIN / "README.md").read_text()
    assert "conn_status=\"ST\"" in readme
    assert "explicit" in readme
    assert "connected status only after" in readme
    assert "disconnected status during normal shutdown" in readme
