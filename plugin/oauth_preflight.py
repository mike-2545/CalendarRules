#!/usr/bin/env python3
"""Read-only, no-network verification of the UD OAuth interface on a test eisy.

Use the same Python interpreter/import environment that CalendarRules will use.
This script NEVER imports udi_interface, opens OAuth data, or changes packages.
A successful result checks source identity only; also verify INFO-or-higher
logging in eisy-ui before enabling the Authenticate workflow.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
from pathlib import Path

APPROVED_VERSION = "3.4.9"
# Exact Git blob for udi_interface/oauth.py at UD's version_3.4.9 tag.
# https://github.com/UniversalDevicesInc/udi_python_interface/tree/version_3.4.9
APPROVED_OAUTH_BLOB = "743b5e5fc0c2214c17e1d00e00b79d8a22ddd447"


def git_blob_sha(data: bytes) -> str:
    """Calculate Git's object ID for file content, without shelling out."""
    header = b"blob " + str(len(data)).encode("ascii") + b"\0"
    return hashlib.sha1(header + data).hexdigest()


def package_version(init_data: bytes) -> str | None:
    """Read only a literal __version__ assignment, never execute imports."""
    try:
        source = ast.parse(init_data.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError):
        return None
    for node in source.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            ) and isinstance(node.value.value, str):
                return node.value.value
    return None


def inspect_interface(init_path: Path) -> tuple[bool, str, str]:
    """Return verified, version and reason; do not disclose provider data."""
    try:
        init_path = init_path.resolve(strict=True)
        if init_path.name != "__init__.py":
            return False, "unknown", "module origin is not an __init__.py file"
        oauth_path = init_path.with_name("oauth.py")
        version = package_version(init_path.read_bytes()) or "unknown"
        blob = git_blob_sha(oauth_path.read_bytes())
    except (OSError, ValueError):
        return False, "unknown", "interface source files could not be inspected"
    if version != APPROVED_VERSION:
        return False, version, "interface version is not the approved 3.4.9 build"
    if blob != APPROVED_OAUTH_BLOB:
        return False, version, "OAuth source does not match UD's tagged 3.4.9 release"
    return True, version, "approved 3.4.9 OAuth source matches"


def main() -> int:
    spec = importlib.util.find_spec("udi_interface")
    if spec is None or spec.origin is None:
        print("BLOCKED: udi_interface is not importable in this Python environment")
        return 1
    origin = Path(spec.origin)
    ok, version, reason = inspect_interface(origin)
    print(f"Interface: {version}")
    print(f"Source: {origin.resolve()}")
    print(f"Source check: {'PASS' if ok else 'BLOCKED'} — {reason}")
    print(
        "Separate required check: confirm eisy-ui CalendarRules log level "
        "is INFO or higher; DEBUG can expose credentials."
    )
    print("This check does not enable OAuth or prove the running plugin uses this interpreter.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
