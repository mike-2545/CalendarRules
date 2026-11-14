#!/usr/bin/env python3
"""Build a deterministic, root-layout CalendarRules eisy-ui store archive."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile


FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
INTERNAL_MANIFEST = "PACKAGE_MANIFEST.sha256"
FORBIDDEN_PARTS = {
    ".calendar-cache",
    ".git",
    ".github",
    ".iox-trust",
    ".pytest_cache",
    "__pycache__",
    "fixtures",
    "logs",
    "runtime",
    "scripts",
    "tests",
}
FORBIDDEN_SUFFIXES = {
    ".calendar-cache.json",
    ".cert",
    ".key",
    ".log",
    ".p12",
    ".pem",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".db-wal",
    ".db-shm",
    ".sqlite-wal",
    ".sqlite-shm",
    ".bak",
    ".dump",
}
# Cover every current runtime opt-in marker (see plugin/main.py), including
# renamed/disabled backups. A store package must never activate a mode simply
# by supplying a marker file, even when it was accidentally Git-tracked.
RUNTIME_MARKER_PREFIXES = (
    ".oauth-event-probe-enabled",
    ".oauth-readonly-discovery-enabled",
    ".oauth-readonly-events-enabled",
    ".calendar-readonly-events-enabled",  # historical marker spelling
    ".iox-state-writes-enabled",
)
PRIVATE_CONFIGURATION_BASENAMES = {
    ".calendar-rules-approved-target.json",
    "calendar-rules-approved-target.json",
}
REQUIRED_ARCHIVE_PATHS = {
    "LICENSE",
    "POLYGLOT_CONFIG.md",
    "README.md",
    "controller.py",
    "eisy_google_calendar.iox_plugin.json",
    "install.sh",
    "main.py",
    "profile/editor/editors.xml",
    "profile/nls/en_us.txt",
    "profile/nodedef/nodedefs.xml",
    "profile/version.txt",
    "requirements.txt",
}


class PackageError(RuntimeError):
    """Fail-closed packaging validation error."""


def _run_git(source: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(source), *args],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PackageError(f"git inspection failed: {exc}") from exc


def _validate_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or "\\" in name or "\r" in name or "\n" in name:
        raise PackageError(f"invalid archive path: {name!r}")
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PackageError(f"unsafe archive path: {name!r}")
    lowered = {part.casefold() for part in path.parts}
    if lowered & FORBIDDEN_PARTS:
        raise PackageError(f"forbidden archive path: {name}")
    lower_name = name.casefold()
    if any(lower_name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        raise PackageError(f"forbidden archive file type: {name}")
    basename = path.name.casefold()
    if basename in PRIVATE_CONFIGURATION_BASENAMES:
        raise PackageError(f"private configuration artifact is forbidden: {name}")
    if any(
        basename == marker or basename.startswith(marker + ".")
        for marker in RUNTIME_MARKER_PREFIXES
    ):
        raise PackageError(f"runtime activation marker is forbidden: {name}")
    return path


def _tracked_inputs(source: Path) -> list[tuple[Path, str]]:
    raw = _run_git(source, "ls-files", "-z", "--", "plugin", "LICENSE")
    tracked = [item.decode("utf-8") for item in raw.split(b"\0") if item]
    result: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for relative in tracked:
        source_path = source / relative
        rel_path = PurePosixPath(relative)
        if rel_path == PurePosixPath("LICENSE"):
            archive_name = "LICENSE"
        elif rel_path.parts and rel_path.parts[0] == "plugin":
            archive_name = PurePosixPath(*rel_path.parts[1:]).as_posix()
        else:  # pragma: no cover - guarded by git pathspec
            raise PackageError(f"unexpected tracked input: {relative}")
        _validate_archive_path(archive_name)
        if archive_name in seen:
            raise PackageError(f"duplicate archive path: {archive_name}")
        if source_path.is_symlink() or not source_path.is_file():
            raise PackageError(f"package input must be a regular non-symlink file: {relative}")
        seen.add(archive_name)
        result.append((source_path, archive_name))
    missing = REQUIRED_ARCHIVE_PATHS - seen
    if missing:
        raise PackageError(f"required package files are missing: {', '.join(sorted(missing))}")
    return sorted(result, key=lambda item: item[1])


def _read_version(source: Path) -> str:
    metadata = json.loads((source / "plugin/eisy_google_calendar.iox_plugin.json").read_text(encoding="utf-8"))
    version = metadata["plugin"]["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise PackageError("plugin metadata has an invalid version")
    launcher = (source / "plugin/main.py").read_text(encoding="utf-8")
    if f'polyglot.start({{"version": "{version}"' not in launcher:
        raise PackageError("runtime and metadata versions do not agree")
    return version


def _ensure_clean(source: Path) -> None:
    status = _run_git(source, "status", "--porcelain", "--", "plugin", "LICENSE")
    if status:
        raise PackageError("tracked package inputs are not clean")


def _manifest(entries: list[tuple[str, bytes]]) -> bytes:
    lines = [f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in entries]
    return "".join(lines).encode("utf-8")


def _zip_entry(name: str, data: bytes) -> tuple[zipfile.ZipInfo, bytes]:
    info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
    info.create_system = 3
    mode = 0o755 if name == "install.sh" else 0o644
    info.external_attr = (0o100000 | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info, data


def build(source: Path, output_dir: Path, *, require_clean: bool = False) -> tuple[Path, Path]:
    source = source.resolve()
    if require_clean:
        _ensure_clean(source)
    version = _read_version(source)
    inputs = _tracked_inputs(source)
    entries = [(name, path.read_bytes()) for path, name in inputs]
    manifest = _manifest(entries)
    _validate_archive_path(INTERNAL_MANIFEST)

    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"CalendarRules-{version}.zip"
    manifest_path = output_dir / f"CalendarRules-{version}.manifest.sha256"
    with zipfile.ZipFile(archive, "w", strict_timestamps=True) as bundle:
        archive_entries = sorted(entries + [(INTERNAL_MANIFEST, manifest)], key=lambda item: item[0])
        for name, data in archive_entries:
            info, payload = _zip_entry(name, data)
            bundle.writestr(info, payload, compresslevel=9)
    manifest_path.write_bytes(manifest)
    return archive, manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args(argv)
    try:
        archive, manifest = build(args.source, args.output_dir, require_clean=args.require_clean)
    except (PackageError, json.JSONDecodeError, OSError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"ARCHIVE={archive}")
    print(f"ARCHIVE_SHA256={hashlib.sha256(archive.read_bytes()).hexdigest()}")
    print(f"MANIFEST={manifest}")
    print(f"MANIFEST_SHA256={hashlib.sha256(manifest.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
