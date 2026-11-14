"""Data models for the offline calendar rule evaluator."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping, Optional, Tuple


DEFAULT_TITLE_MATCH_TYPE = "contains"
TITLE_MATCH_TYPES = ("contains", "exact", "starts_with", "ends_with")
DEFAULT_MATCH_FIELD = "title"
MATCH_FIELDS = ("title", "location")


class SnapshotStatus(str, Enum):
    """Status supplied by the calendar snapshot provider."""

    VALID = "valid"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    OUT_OF_COVERAGE = "out_of_coverage"


@dataclass(frozen=True)
class CalendarEvent:
    """A single, already-expanded timed calendar event instance."""

    event_id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    cancelled: bool = False
    location: str = ""


@dataclass(frozen=True)
class Rule:
    """An event-field rule mapping matches to an IoX state-variable ID."""

    name: str
    title_contains: str
    target_variable_id: int
    start_offset_minutes: int = 0
    end_offset_minutes: int = 0
    case_sensitive: bool = False
    ignore_all_day: bool = True
    ignore_cancelled: bool = True
    title_match_type: str = DEFAULT_TITLE_MATCH_TYPE
    match_field: str = DEFAULT_MATCH_FIELD


@dataclass(frozen=True)
class CalendarConfig:
    """Validated calendar configuration shared by all configured rules."""

    calendar_ids: Tuple[str, ...]
    timezone: str
    poll_seconds: int
    rules: Tuple[Rule, ...]


@dataclass(frozen=True)
class CalendarSnapshot:
    """Provider result with optional completion and evaluation coverage."""

    status: SnapshotStatus
    events: Tuple[CalendarEvent, ...] = ()
    fetched_at: Optional[datetime] = None
    error: Optional[str] = None
    coverage_start: Optional[datetime] = None
    coverage_end: Optional[datetime] = None
    configuration_fingerprint: Optional[str] = None
    location_data_complete: bool = True


@dataclass(frozen=True)
class EvaluationResult:
    """Desired states plus provider status, trust, and diagnostics."""

    desired_states: Mapping[int, int]
    status: SnapshotStatus
    trusted: bool
    active_matches: Mapping[int, Tuple[str, ...]] = field(default_factory=dict)
    ignored_event_count: int = 0
    invalid_interval_count: int = 0
    diagnostics: Tuple[str, ...] = ()
    matched_rules: Mapping[int, Tuple[str, ...]] = field(default_factory=dict)
    ignored_reasons: Mapping[str, int] = field(default_factory=dict)
