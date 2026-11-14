#!/usr/bin/env python3
"""Fail-closed checks for obvious private data in the tracked public snapshot.

This is an aid to manual review, not a substitute for secret scanning or a
review of reachable Git history, issues, actions artifacts, or release ZIPs.
Findings print only path, line number, and category (never matched secrets).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "private IPv4 address": re.compile(r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"),
    "production facility identifier": re.compile(r"(?i)(?:577foundation|577FS2|THE577|Perrysburg|mmummert)"),
    "private key header": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "Google API key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "GitHub personal token": re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    "Google OAuth client ID": re.compile(r"[0-9]{10,}-[a-z0-9_-]{12,}\.apps\.googleusercontent\.com"),
    "Google OAuth refresh token": re.compile(r"1//[0-9A-Za-z_-]{30,}"),
}
FORBIDDEN_NAMES = [
    re.compile(r"(?i)(?:^|/)(?:credentials(?:\.[^/]+)?|client_secret[^/]*\.json|token\.json|\.env(?:\..*)?)$"),
    re.compile(r"(?i)\.(?:key|pem|p12|pfx|sqlite3?|db|bak|dump)$"),
]
SELF = "scripts/audit_public_source.py"


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT, check=True, stdout=subprocess.PIPE,
    )
    return sorted(p.decode("utf-8") for p in result.stdout.split(b"\x00") if p)


def main() -> int:
    findings: list[tuple[str, int, str]] = []
    for name in tracked_files():
        if name == SELF:
            continue
        for rule in FORBIDDEN_NAMES:
            if rule.search(name):
                findings.append((name, 0, "private file type/name"))
                break
        raw = (ROOT / name).read_bytes()
        if b"\x00" in raw:
            findings.append((name, 0, "binary tracked file requires manual review"))
            continue
        try:
            data = raw.decode("utf-8")
        except UnicodeDecodeError:
            findings.append((name, 0, "non-UTF8 tracked file"))
            continue
        for lineno, line in enumerate(data.splitlines(), 1):
            for label, rule in PATTERNS.items():
                if rule.search(line):
                    findings.append((name, lineno, label))
    if findings:
        print(f"Source privacy audit blocked: {len(findings)} finding(s).")
        for name, line, category in findings:
            print(f"{name}:{line}: {category}")
        return 1
    print(f"Tracked-source privacy heuristics: PASS ({len(tracked_files())-1} files inspected).")
    print("Still required: manual review, Git history review, and dedicated secret scanner.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
