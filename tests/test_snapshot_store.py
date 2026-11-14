import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import eisy_google_calendar.snapshot_store as snapshot_store_module

from eisy_google_calendar.config_identity import configuration_fingerprint
from eisy_google_calendar.models import (
    CalendarConfig,
    CalendarEvent,
    CalendarSnapshot,
    Rule,
    SnapshotStatus,
)
from eisy_google_calendar.snapshot_store import (
    CACHE_FORMAT,
    CACHE_VERSION,
    LEGACY_CACHE_VERSION,
    FileSnapshotStorage,
    SnapshotCacheError,
    decode_snapshot,
    encode_snapshot,
)


UTC = timezone.utc
FETCHED = datetime(2025, 1, 1, 9, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]
PRIVATE_CONFIG = CalendarConfig(
    ("private-calendar@example.com",),
    "America/New_York",
    60,
    (Rule("private-rule", "Confidential matcher", 101),),
)
PRIVATE_FINGERPRINT = configuration_fingerprint(PRIVATE_CONFIG)


def trusted_snapshot():
    return CalendarSnapshot(
        SnapshotStatus.VALID,
        events=(
            CalendarEvent(
                "private-calendar@example.com:provider-event-id",
                "Workshop setup",
                FETCHED + timedelta(hours=1),
                FETCHED + timedelta(hours=2),
                location="Private Workshop East",
            ),
        ),
        fetched_at=FETCHED,
        coverage_start=FETCHED,
        coverage_end=FETCHED + timedelta(hours=24),
        configuration_fingerprint=PRIVATE_FINGERPRINT,
    )


def test_versioned_snapshot_round_trip_hashes_provider_identifiers():
    encoded = encode_snapshot(trusted_snapshot())
    envelope = json.loads(encoded)
    restored = decode_snapshot(encoded)

    assert envelope["format"] == CACHE_FORMAT
    assert envelope["version"] == CACHE_VERSION
    assert b"private-calendar@example.com" not in encoded
    assert b"Confidential matcher" not in encoded
    assert b"provider-event-id" not in encoded
    assert b"Workshop setup" in encoded
    assert restored.status is SnapshotStatus.VALID
    assert restored.fetched_at == FETCHED
    assert restored.coverage_start == FETCHED
    assert restored.coverage_end == FETCHED + timedelta(hours=24)
    assert restored.configuration_fingerprint == PRIVATE_FINGERPRINT
    assert restored.events[0].title == "Workshop setup"
    assert restored.events[0].location == "Private Workshop East"
    assert restored.location_data_complete is True
    assert restored.events[0].event_id.startswith("cached:")


def test_cache_contains_no_token_or_oauth_fields():
    encoded = encode_snapshot(trusted_snapshot()).lower()
    for forbidden in (b"access_token", b"refresh_token", b"client_secret", b"oauth"):
        assert forbidden not in encoded


def test_configuration_fingerprint_is_deterministic_and_non_secret():
    first = configuration_fingerprint(PRIVATE_CONFIG)
    second = configuration_fingerprint(PRIVATE_CONFIG)

    assert first == second
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")
    assert "private-calendar" not in first
    assert "Confidential" not in first


@pytest.mark.parametrize(
    "changed",
    [
        CalendarConfig(
            ("different-calendar@example.com",),
            "America/New_York",
            60,
            PRIVATE_CONFIG.rules,
        ),
        CalendarConfig(
            PRIVATE_CONFIG.calendar_ids,
            "America/Chicago",
            60,
            PRIVATE_CONFIG.rules,
        ),
        CalendarConfig(
            PRIVATE_CONFIG.calendar_ids,
            PRIVATE_CONFIG.timezone,
            60,
            (Rule("private-rule", "Different matcher", 101),),
        ),
        CalendarConfig(
            PRIVATE_CONFIG.calendar_ids,
            PRIVATE_CONFIG.timezone,
            60,
            (
                Rule(
                    "private-rule",
                    "Confidential matcher",
                    101,
                    start_offset_minutes=-30,
                    end_offset_minutes=45,
                ),
            ),
        ),
    ],
)
def test_schedule_affecting_configuration_changes_fingerprint(changed):
    assert configuration_fingerprint(changed) != PRIVATE_FINGERPRINT


@pytest.mark.parametrize(
    "snapshot",
    [
        CalendarSnapshot(SnapshotStatus.FAILED),
        CalendarSnapshot(SnapshotStatus.VALID, fetched_at=FETCHED),
    ],
)
def test_only_complete_covered_valid_snapshots_can_be_persisted(snapshot):
    with pytest.raises(SnapshotCacheError):
        encode_snapshot(snapshot)


def test_checksum_detects_tampering():
    envelope = json.loads(encode_snapshot(trusted_snapshot()))
    envelope["snapshot"]["events"][0]["title"] = "Changed privately"
    tampered = json.dumps(envelope).encode()
    with pytest.raises(SnapshotCacheError, match="checksum"):
        decode_snapshot(tampered)


@pytest.mark.parametrize(
    "data",
    [
        b"not json",
        b"{}",
        json.dumps(
            {
                "format": CACHE_FORMAT,
                "version": CACHE_VERSION + 1,
                "checksum": "unused",
                "snapshot": {},
            }
        ).encode(),
    ],
)
def test_malformed_and_unknown_cache_formats_are_rejected(data):
    with pytest.raises(SnapshotCacheError):
        decode_snapshot(data)


@pytest.mark.parametrize("version", (True, 2.0, "2"))
def test_cache_version_must_be_a_strict_integer(version):
    envelope = json.loads(encode_snapshot(trusted_snapshot()))
    envelope["version"] = version
    with pytest.raises(SnapshotCacheError, match="unsupported"):
        decode_snapshot(snapshot_store_module._canonical(envelope) + b"\n")


def _legacy_v2(snapshot=None):
    envelope = json.loads(encode_snapshot(snapshot or trusted_snapshot()))
    envelope["version"] = LEGACY_CACHE_VERSION
    for event in envelope["snapshot"]["events"]:
        event.pop("location")
    envelope["checksum"] = snapshot_store_module.hashlib.sha256(
        snapshot_store_module._canonical(envelope["snapshot"])
    ).hexdigest()
    return snapshot_store_module._canonical(envelope) + b"\n"


def test_previous_v2_cache_decodes_only_as_location_incomplete():
    restored = decode_snapshot(_legacy_v2())

    assert restored.status is SnapshotStatus.VALID
    assert restored.location_data_complete is False
    assert restored.events[0].location == ""
    assert restored.events[0].title == "Workshop setup"


def test_v2_and_v3_event_structures_remain_strict():
    envelope = json.loads(encode_snapshot(trusted_snapshot()))
    envelope["version"] = LEGACY_CACHE_VERSION
    envelope["checksum"] = snapshot_store_module.hashlib.sha256(
        snapshot_store_module._canonical(envelope["snapshot"])
    ).hexdigest()

    with pytest.raises(SnapshotCacheError, match="invalid structure"):
        decode_snapshot(json.dumps(envelope).encode())

    legacy = json.loads(_legacy_v2())
    legacy["snapshot"]["events"][0]["unexpected"] = "private"
    legacy["checksum"] = snapshot_store_module.hashlib.sha256(
        snapshot_store_module._canonical(legacy["snapshot"])
    ).hexdigest()
    with pytest.raises(SnapshotCacheError, match="invalid structure"):
        decode_snapshot(snapshot_store_module._canonical(legacy))


def test_location_tampering_is_detected_and_incomplete_data_cannot_encode_v3():
    envelope = json.loads(encode_snapshot(trusted_snapshot()))
    envelope["snapshot"]["events"][0]["location"] = "Changed room"
    with pytest.raises(SnapshotCacheError, match="checksum"):
        decode_snapshot(json.dumps(envelope).encode())

    incomplete = CalendarSnapshot(
        **{**trusted_snapshot().__dict__, "location_data_complete": False}
    )
    with pytest.raises(SnapshotCacheError, match="complete location"):
        encode_snapshot(incomplete)


def test_file_storage_writes_and_reloads_atomically(tmp_path):
    path = tmp_path / "private" / "snapshot.json"
    storage = FileSnapshotStorage(path)
    assert storage.load() is None

    storage.save(trusted_snapshot())
    restored = storage.load()

    assert restored is not None
    assert restored.events[0].title == "Workshop setup"
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_interrupted_atomic_replace_preserves_prior_trusted_cache(
    tmp_path, monkeypatch
):
    path = tmp_path / ".calendar-cache" / "schedule.calendar-cache.json"
    storage = FileSnapshotStorage(path)
    storage.save(trusted_snapshot())
    original = path.read_bytes()

    def interrupted_replace(source, destination):
        raise OSError("simulated interruption")

    monkeypatch.setattr(snapshot_store_module.os, "replace", interrupted_replace)
    with pytest.raises(SnapshotCacheError, match="could not be written"):
        storage.save(trusted_snapshot())

    assert path.read_bytes() == original
    assert decode_snapshot(path.read_bytes()).status is SnapshotStatus.VALID
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


@pytest.mark.parametrize(
    "directory,mode,kind",
    [
        (True, stat.S_IFDIR | 0o755, "directory"),
        (False, stat.S_IFREG | 0o644, "file"),
    ],
)
def test_posix_permission_check_rejects_group_or_other_access(
    monkeypatch, directory, mode, kind
):
    class FakePath:
        def stat(self):
            return type("FakeStat", (), {"st_mode": mode})()

    monkeypatch.setattr(snapshot_store_module.os, "name", "posix")
    with pytest.raises(SnapshotCacheError, match=f"{kind} must be owner-only"):
        snapshot_store_module._require_owner_only(
            FakePath(), directory=directory
        )


def test_runtime_cache_names_are_excluded_from_git():
    gitignore = (ROOT / ".gitignore").read_text()
    assert "*.calendar-cache.json" in gitignore
    assert ".calendar-cache/" in gitignore


def test_corrupt_file_is_never_loaded_as_an_empty_snapshot(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text("corrupt private data")
    storage = FileSnapshotStorage(path)
    with pytest.raises(SnapshotCacheError):
        storage.load()


def test_native_persist_cache_is_private_and_survives_simulated_code_update(
    tmp_path,
):
    # A clean store plugin gets its own home; source code can be replaced
    # independently without touching the owner-only persist subtree.
    home = tmp_path / "different-account" / "slot-29"
    home.mkdir(parents=True)
    cache = home / "persist" / "calendarrules" / "schedule.calendar-cache.json"
    first = FileSnapshotStorage(cache)
    assert first.load() is None
    assert not (home / ".calendar-cache").exists()

    first.save(trusted_snapshot())
    assert cache.is_file()
    assert not (home / ".calendar-cache").exists()
    if os.name != "nt":
        assert stat.S_IMODE(cache.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(cache.stat().st_mode) == 0o600

    # Recreate the storage object as a new app version would on restart.
    second = FileSnapshotStorage(cache)
    restored = second.load()
    assert restored is not None
    assert restored.configuration_fingerprint == PRIVATE_FINGERPRINT
    assert restored.events[0].location == "Private Workshop East"
    assert restored.location_data_complete is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-only file enforcement")
def test_native_persist_cache_rejects_exposed_permissions(tmp_path):
    private = tmp_path / "persist" / "calendarrules"
    cache = private / "schedule.calendar-cache.json"
    store = FileSnapshotStorage(cache)
    store.save(trusted_snapshot())

    private.chmod(0o755)
    with pytest.raises(SnapshotCacheError, match="directory must be owner-only"):
        store.load()
    private.chmod(0o700)
    cache.chmod(0o644)
    with pytest.raises(SnapshotCacheError, match="file must be owner-only"):
        store.load()
