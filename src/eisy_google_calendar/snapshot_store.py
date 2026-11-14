"""Versioned, atomic persistence for complete trusted calendar snapshots."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from datetime import datetime
from pathlib import Path
import tempfile
from typing import Any, Protocol

from .models import CalendarEvent, CalendarSnapshot, SnapshotStatus
from .validation import ConfigurationError, normalize_utc, validate_event


CACHE_FORMAT = "eisy-google-calendar-snapshot"
CACHE_VERSION = 3
LEGACY_CACHE_VERSION = 2
MAX_CACHE_BYTES = 16 * 1024 * 1024


class SnapshotCacheError(Exception):
    """Raised when a trusted snapshot cannot be safely stored or restored."""


class SnapshotStorage(Protocol):
    """Dependency-injected persistence boundary used by the offline planner."""

    def load(self) -> CalendarSnapshot | None: ...

    def save(self, snapshot: CalendarSnapshot) -> None: ...


def _require_owner_only(path: Path, *, directory: bool) -> None:
    """Reject group/other access on POSIX; Windows ACLs are out of scope here."""

    if os.name == "nt":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise SnapshotCacheError(
            "snapshot cache permissions could not be read"
        ) from exc
    if mode & 0o077:
        kind = "directory" if directory else "file"
        raise SnapshotCacheError(f"snapshot cache {kind} must be owner-only")


def _utc_text(value: Any, path: str) -> str:
    try:
        normalized = normalize_utc(value, path)
    except ConfigurationError as exc:
        raise SnapshotCacheError(str(exc)) from None
    return normalized.isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any, path: str):
    if not isinstance(value, str):
        raise SnapshotCacheError(f"{path} must be an RFC3339 string")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
        return normalize_utc(parsed, path)
    except (ValueError, ConfigurationError):
        raise SnapshotCacheError(f"{path} must be a valid aware timestamp") from None


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _event_reference(event_id: str) -> str:
    return hashlib.sha256(event_id.encode("utf-8")).hexdigest()


def _configuration_fingerprint(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SnapshotCacheError("snapshot configuration fingerprint is invalid")
    return value


def encode_snapshot(snapshot: CalendarSnapshot) -> bytes:
    """Encode only a complete covered snapshot; provider IDs are hashed."""

    if snapshot.status is not SnapshotStatus.VALID:
        raise SnapshotCacheError("only valid snapshots may be persisted")
    if snapshot.location_data_complete is not True:
        raise SnapshotCacheError(
            "only snapshots with complete location data may use cache version 3"
        )
    if (
        snapshot.fetched_at is None
        or snapshot.coverage_start is None
        or snapshot.coverage_end is None
    ):
        raise SnapshotCacheError("snapshot completion and coverage are required")
    configuration_fingerprint = _configuration_fingerprint(
        snapshot.configuration_fingerprint
    )

    fetched_at = _utc_text(snapshot.fetched_at, "snapshot.fetched_at")
    coverage_start = _utc_text(snapshot.coverage_start, "snapshot.coverage_start")
    coverage_end = _utc_text(snapshot.coverage_end, "snapshot.coverage_end")
    fetched_value = _parse_utc(fetched_at, "snapshot.fetched_at")
    coverage_start_value = _parse_utc(
        coverage_start, "snapshot.coverage_start"
    )
    coverage_end_value = _parse_utc(coverage_end, "snapshot.coverage_end")
    if coverage_end_value < coverage_start_value or not (
        coverage_start_value <= fetched_value <= coverage_end_value
    ):
        raise SnapshotCacheError("snapshot coverage is invalid")
    events = []
    for raw_event in snapshot.events:
        try:
            event = validate_event(raw_event)
        except ConfigurationError as exc:
            raise SnapshotCacheError(f"snapshot event is invalid: {exc}") from None
        events.append(
            {
                "ref": _event_reference(event.event_id),
                "title": event.title,
                "location": event.location,
                "start": _utc_text(event.start, "event.start"),
                "end": _utc_text(event.end, "event.end"),
                "all_day": event.all_day,
                "cancelled": event.cancelled,
            }
        )

    payload = {
        "configuration_fingerprint": configuration_fingerprint,
        "fetched_at": fetched_at,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "events": events,
    }
    checksum = hashlib.sha256(_canonical(payload)).hexdigest()
    envelope = {
        "format": CACHE_FORMAT,
        "version": CACHE_VERSION,
        "checksum": checksum,
        "snapshot": payload,
    }
    return _canonical(envelope) + b"\n"


def _require_keys(value: Any, keys: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise SnapshotCacheError(f"{path} has an invalid structure")
    return value


def decode_snapshot(data: bytes) -> CalendarSnapshot:
    """Validate and decode a versioned snapshot without trusting file content."""

    if not isinstance(data, bytes) or len(data) > MAX_CACHE_BYTES:
        raise SnapshotCacheError("snapshot cache size is invalid")
    try:
        envelope = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SnapshotCacheError("snapshot cache is not valid JSON") from None
    envelope = _require_keys(
        envelope,
        {"format", "version", "checksum", "snapshot"},
        "snapshot cache",
    )
    version = envelope["version"]
    if (
        envelope["format"] != CACHE_FORMAT
        or type(version) is not int
        or version not in (LEGACY_CACHE_VERSION, CACHE_VERSION)
    ):
        raise SnapshotCacheError("snapshot cache format is unsupported")
    if not isinstance(envelope["checksum"], str):
        raise SnapshotCacheError("snapshot cache checksum is invalid")
    payload = _require_keys(
        envelope["snapshot"],
        {
            "configuration_fingerprint",
            "fetched_at",
            "coverage_start",
            "coverage_end",
            "events",
        },
        "snapshot",
    )
    actual_checksum = hashlib.sha256(_canonical(payload)).hexdigest()
    if not hmac.compare_digest(envelope["checksum"], actual_checksum):
        raise SnapshotCacheError("snapshot cache checksum does not match")

    fetched_at = _parse_utc(payload["fetched_at"], "snapshot.fetched_at")
    configuration_fingerprint = _configuration_fingerprint(
        payload["configuration_fingerprint"]
    )
    coverage_start = _parse_utc(
        payload["coverage_start"], "snapshot.coverage_start"
    )
    coverage_end = _parse_utc(payload["coverage_end"], "snapshot.coverage_end")
    if coverage_end < coverage_start or not (
        coverage_start <= fetched_at <= coverage_end
    ):
        raise SnapshotCacheError("snapshot coverage is invalid")
    if not isinstance(payload["events"], list):
        raise SnapshotCacheError("snapshot.events must be a list")

    events = []
    seen_refs = set()
    for raw_event in payload["events"]:
        event_keys = {"ref", "title", "start", "end", "all_day", "cancelled"}
        if version == CACHE_VERSION:
            event_keys.add("location")
        raw_event = _require_keys(
            raw_event,
            event_keys,
            "snapshot event",
        )
        reference = raw_event["ref"]
        if (
            not isinstance(reference, str)
            or len(reference) != 64
            or any(character not in "0123456789abcdef" for character in reference)
            or reference in seen_refs
        ):
            raise SnapshotCacheError("snapshot event reference is invalid")
        if not isinstance(raw_event["title"], str):
            raise SnapshotCacheError("snapshot event title is invalid")
        location = raw_event.get("location", "")
        if not isinstance(location, str):
            raise SnapshotCacheError("snapshot event location is invalid")
        if type(raw_event["all_day"]) is not bool or type(raw_event["cancelled"]) is not bool:
            raise SnapshotCacheError("snapshot event flags are invalid")
        event = CalendarEvent(
            event_id=f"cached:{reference}",
            title=raw_event["title"],
            start=_parse_utc(raw_event["start"], "event.start"),
            end=_parse_utc(raw_event["end"], "event.end"),
            all_day=raw_event["all_day"],
            cancelled=raw_event["cancelled"],
            location=location,
        )
        try:
            events.append(validate_event(event))
        except ConfigurationError as exc:
            raise SnapshotCacheError(f"snapshot event is invalid: {exc}") from None
        seen_refs.add(reference)

    return CalendarSnapshot(
        status=SnapshotStatus.VALID,
        events=tuple(events),
        fetched_at=fetched_at,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        configuration_fingerprint=configuration_fingerprint,
        location_data_complete=version == CACHE_VERSION,
    )


class FileSnapshotStorage:
    """Store one cache file atomically with owner-only file permissions."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self) -> CalendarSnapshot | None:
        try:
            if not self.path.exists():
                return None
            _require_owner_only(self.path.parent, directory=True)
            _require_owner_only(self.path, directory=False)
            if self.path.stat().st_size > MAX_CACHE_BYTES:
                raise SnapshotCacheError("snapshot cache is too large")
            return decode_snapshot(self.path.read_bytes())
        except SnapshotCacheError:
            raise
        except OSError as exc:
            raise SnapshotCacheError("snapshot cache could not be read") from exc

    def save(self, snapshot: CalendarSnapshot) -> None:
        encoded = encode_snapshot(snapshot)
        temporary_path = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _require_owner_only(self.path.parent, directory=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            else:
                os.chmod(temporary_path, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
            os.chmod(self.path, 0o600)
        except (OSError, SnapshotCacheError) as exc:
            raise SnapshotCacheError("snapshot cache could not be written") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
