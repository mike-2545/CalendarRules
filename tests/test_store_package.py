import hashlib
import importlib.util
import os
import re
from pathlib import Path
import stat
import subprocess
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "scripts" / "build_store_package.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_store_package", BUILDER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


builder = _load_builder()


def _manifest_entries(raw: bytes) -> dict[str, str]:
    result = {}
    assert b"\r" not in raw
    for line in raw.decode("utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        assert separator == "  "
        assert len(digest) == 64
        assert name and name not in result
        result[name] = digest
    return result


def test_store_package_is_root_layout_deterministic_and_self_verifying(tmp_path):
    first_archive, first_manifest = builder.build(ROOT, tmp_path / "one")
    second_archive, second_manifest = builder.build(ROOT, tmp_path / "two")

    assert first_archive.read_bytes() == second_archive.read_bytes()
    assert first_manifest.read_bytes() == second_manifest.read_bytes()

    with zipfile.ZipFile(first_archive) as bundle:
        names = bundle.namelist()
        assert names == sorted(names)
        assert all(not name.startswith("plugin/") for name in names)
        assert "main.py" in names
        assert "install.sh" in names
        assert "LICENSE" in names
        assert "eisy_google_calendar/activity.py" in names
        assert "profile/version.txt" in names
        assert builder.INTERNAL_MANIFEST in names

        manifest = bundle.read(builder.INTERNAL_MANIFEST)
        assert manifest == first_manifest.read_bytes()
        expected = _manifest_entries(manifest)
        assert set(expected) == set(names) - {builder.INTERNAL_MANIFEST}
        for name, digest in expected.items():
            assert hashlib.sha256(bundle.read(name)).hexdigest() == digest


def test_store_package_excludes_private_runtime_and_development_material(tmp_path):
    archive, _ = builder.build(ROOT, tmp_path)
    with zipfile.ZipFile(archive) as bundle:
        lowered = [name.casefold() for name in bundle.namelist()]
    forbidden_fragments = (
        "tests/",
        "fixtures/",
        "scripts/",
        "__pycache__",
        ".calendar-cache",
        ".iox-trust",
        ".iox-state-writes-enabled",
        ".oauth-readonly-events-enabled",
        ".oauth-readonly-discovery-enabled",
        ".oauth-event-probe-enabled",
        "calendar-rules-approved-target.json",
    )
    assert not any(fragment in name for name in lowered for fragment in forbidden_fragments)
    assert not any(
        name.endswith((".cert", ".key", ".pem", ".p12", ".sqlite", ".sqlite3", ".db", ".bak", ".dump"))
        for name in lowered
    )


@pytest.mark.parametrize(
    "name",
    [
        "../secret",
        "/absolute",
        "tests/test_secret.py",
        "profile\\nodedef.xml",
        "private.key",
        "device.cert",
        ".calendar-cache/schedule.calendar-cache.json",
        ".iox-trust/eisyui.cert",
        ".iox-state-writes-enabled",
        ".iox-state-writes-enabled.disabled",
        ".iox-state-writes-enabled.disabled.20261114T020000Z",
        ".oauth-readonly-events-enabled",
        ".oauth-readonly-events-enabled.disabled",
        ".oauth-readonly-discovery-enabled",
        ".oauth-event-probe-enabled",
        ".calendar-readonly-events-enabled",
        "secrets/.calendar-rules-approved-target.json",
        "calendar-rules-approved-target.json",
        "private.db",
        "private.bak",
        "private.dump",
        "eisy.db-wal",
        "eisy.sqlite-shm",
        "bad\rname",
        "bad\nname",
    ],
)
def test_store_package_rejects_unsafe_paths(name):
    with pytest.raises(builder.PackageError):
        builder._validate_archive_path(name)


def test_every_current_runtime_opt_in_marker_is_forbidden():
    # Future plugin modes must not bypass the packaging guard when a new marker
    # name is introduced in main.py. Test actual application constants.
    source = (ROOT / "plugin" / "main.py").read_text(encoding="utf-8")
    markers = set(
        re.findall(
            r'^[A-Z_]+_MARKER_FILE\s*=\s*"([^"]+)"',
            source,
            flags=re.MULTILINE,
        )
    )
    assert markers, "expected explicit runtime activation markers"
    for marker in markers:
        with pytest.raises(builder.PackageError, match="runtime activation marker"):
            builder._validate_archive_path(marker)
        with pytest.raises(builder.PackageError, match="runtime activation marker"):
            builder._validate_archive_path(marker + ".disabled")


def test_store_package_uses_only_git_tracked_plugin_inputs(tmp_path):
    untracked = ROOT / "plugin" / "should-never-package.secret"
    try:
        untracked.write_text("not a credential, only a regression sentinel", encoding="utf-8")
        archive, _ = builder.build(ROOT, tmp_path)
        with zipfile.ZipFile(archive) as bundle:
            assert "should-never-package.secret" not in bundle.namelist()
    finally:
        untracked.unlink(missing_ok=True)


def test_install_hook_contract_is_fail_closed():
    source = (ROOT / "plugin" / "install.sh").read_text(encoding="utf-8")
    assert source.startswith("#!/usr/bin/env sh\nset -eu\n")
    assert "python3 -m pip install" in source
    assert "--user" in source
    assert "--requirement" in source
    assert "sudo" not in source.casefold()
    assert "curl" not in source.casefold()
    assert "wget" not in source.casefold()
    assert (ROOT / "plugin" / "requirements.txt").read_text(encoding="utf-8").strip() == "udi_interface==3.4.9"


@pytest.mark.skipif(os.name == "nt", reason="POSIX install-hook execution is validated on Ubuntu CI")
def test_install_hook_passes_only_supported_pip_arguments(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    hook = package / "install.sh"
    hook.write_bytes((ROOT / "plugin" / "install.sh").read_bytes())
    (package / "requirements.txt").write_text("udi_interface==3.4.9\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    arguments = tmp_path / "arguments"
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$ARGUMENTS_FILE\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["ARGUMENTS_FILE"] = str(arguments)
    subprocess.run(["sh", str(hook)], cwd=tmp_path, env=env, check=True)
    assert arguments.read_text(encoding="utf-8").splitlines() == [
        "-m",
        "pip",
        "install",
        "--user",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        "--requirement",
        str(package / "requirements.txt"),
    ]


def test_public_security_document_discloses_native_contract_and_limits():
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    getting_started = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    assert "127.0.0.1:443" in security
    assert "certificate" in security
    assert "type-2" in security
    assert "previously ON variable" in security
    assert "read-only" in security
    assert "enabled rule" in security
    assert "type-2" in getting_started
    assert "Google" in getting_started
