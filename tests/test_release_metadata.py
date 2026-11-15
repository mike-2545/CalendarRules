"""Contract tests for the maintenance-only 0.4.14 release candidate."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"


def test_maintenance_release_has_matching_versions_and_public_links():
    metadata = json.loads((PLUGIN / "eisy_google_calendar.iox_plugin.json").read_text(encoding="utf-8"))["plugin"]
    launcher = (PLUGIN / "main.py").read_text(encoding="utf-8")
    assert metadata["version"] == "0.4.14"
    assert 'polyglot.start({"version": "0.4.14", "requestId": False})' in launcher
    assert metadata["documentationLink"] == "https://calendarrules.org/"
    assert metadata["licenseLink"] == "https://github.com/mike-2545/CalendarRules/blob/main/LICENSE"


def test_platform_identity_and_install_contract_are_unchanged():
    metadata = json.loads((PLUGIN / "eisy_google_calendar.iox_plugin.json").read_text(encoding="utf-8"))["plugin"]
    assert metadata["publisher"] == "eisy-google-calendar"
    assert metadata["executableName"] == "main.py"
    assert metadata["installScript"] == "install.sh"
    assert metadata["enableOAUTH2"] is True
    assert metadata["requiresIoXAccess"] is True
    assert metadata["shortPoll"] == 60
    assert metadata["longPoll"] == 900
    assert metadata["profileVersion"] == "3.0.0"
    assert (PLUGIN / "requirements.txt").read_text(encoding="utf-8").strip() == "udi_interface==3.4.9"
