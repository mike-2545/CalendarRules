"""Offline tests for the read-only eisy OAuth source preflight."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugin" / "oauth_preflight.py"


@pytest.fixture
def check_module():
    spec = importlib.util.spec_from_file_location("calendar_oauth_preflight", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_interface(tmp_path, *, version="3.4.9", oauth=b"known UD OAuth code"):
    pkg = tmp_path / "udi_interface"
    pkg.mkdir()
    init = pkg / "__init__.py"
    init.write_text(f"__version__ = {version!r}\n", encoding="utf-8")
    (pkg / "oauth.py").write_bytes(oauth)
    return init


def test_preflight_is_read_only_and_does_not_import_oauth(check_module):
    source = SCRIPT.read_text()
    assert "from udi_interface" not in source
    assert "import udi_interface" not in source
    assert "requests" not in source
    assert "subprocess" not in source
    assert "oauthTokens" not in source
    assert "token_endpoint" not in source
    assert check_module.APPROVED_VERSION == "3.4.9"
    assert len(check_module.APPROVED_OAUTH_BLOB) == 40


def test_valid_exact_version_and_source_fingerprint_pass(tmp_path, check_module, monkeypatch):
    init = create_interface(tmp_path)
    blob = check_module.git_blob_sha((init.parent / "oauth.py").read_bytes())
    monkeypatch.setattr(check_module, "APPROVED_OAUTH_BLOB", blob)
    ok, version, reason = check_module.inspect_interface(init)
    assert ok is True
    assert version == "3.4.9"
    assert "matches" in reason


@pytest.mark.parametrize("version", ("3.4.7", "3.4.8", "3.6.7", "3.4.10"))
def test_other_versions_are_not_assumed_patched(tmp_path, check_module, monkeypatch, version):
    init = create_interface(tmp_path, version=version)
    blob = check_module.git_blob_sha((init.parent / "oauth.py").read_bytes())
    monkeypatch.setattr(check_module, "APPROVED_OAUTH_BLOB", blob)
    ok, observed, _reason = check_module.inspect_interface(init)
    assert not ok
    assert observed == version


def test_same_version_but_modified_oauth_is_rejected(tmp_path, check_module):
    init = create_interface(tmp_path)
    ok, version, reason = check_module.inspect_interface(init)
    assert not ok
    assert version == "3.4.9"
    assert "does not match" in reason


def test_missing_or_invalid_files_fail_closed(tmp_path, check_module):
    init = create_interface(tmp_path)
    (init.parent / "oauth.py").unlink()
    ok, _, _ = check_module.inspect_interface(init)
    assert not ok
    init.write_text("__version__ = calculate_version()\n")
    (init.parent / "oauth.py").write_bytes(b"")
    ok, version, _ = check_module.inspect_interface(init)
    assert not ok
    assert version == "unknown"


def test_preflight_checks_path_and_never_executes_initialization(tmp_path, check_module):
    init = create_interface(tmp_path)
    init.write_text("__version__ = '3.4.9'\nraise RuntimeError('must never run')\n")
    assert check_module.package_version(init.read_bytes()) == "3.4.9"
    ok, _, _ = check_module.inspect_interface(init.parent / "oauth.py")
    assert not ok


def test_cli_reports_separate_log_level_gate(tmp_path, check_module, monkeypatch, capsys):
    init = create_interface(tmp_path)
    blob = check_module.git_blob_sha((init.parent / "oauth.py").read_bytes())
    monkeypatch.setattr(check_module, "APPROVED_OAUTH_BLOB", blob)
    monkeypatch.setattr(check_module.importlib.util, "find_spec", lambda name: SimpleNamespace(origin=str(init)))
    assert check_module.main() == 0
    output = capsys.readouterr().out
    assert "PASS" in output
    assert "INFO or higher" in output
    assert "DEBUG can expose credentials" in output


def test_metadata_enables_store_oauth_without_embedding_credentials_and_defaults_info():
    metadata = json.loads(
        (ROOT / "plugin" / "eisy_google_calendar.iox_plugin.json").read_text()
    )["plugin"]
    assert metadata["enableOAUTH2"] is True
    assert "oauthParams" not in metadata
    assert metadata["initialLogLevel"] == "INFO"
