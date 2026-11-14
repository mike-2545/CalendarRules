"""Supported eisy-ui typed configuration for offline CalendarRules previews."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, Collection, Mapping

from .cached_schedule import ALLOWED_REFRESH_HOURS, SchedulePolicy
from .models import (
    DEFAULT_MATCH_FIELD,
    DEFAULT_TITLE_MATCH_TYPE,
    MATCH_FIELDS,
    TITLE_MATCH_TYPES,
    CalendarConfig,
    CalendarSnapshot,
)
from .preview import PreviewResult, preview_snapshot
from .validation import ConfigurationError, load_config_data


DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_UPCOMING_EVENT_LIMIT = 5
UPCOMING_EVENT_LIMITS = frozenset({5, 10})
MIN_OFFSET_MINUTES = -1440
MAX_OFFSET_MINUTES = 1440


_TYPED_PARAMETER_SCHEMA = [
    {
        "name": "timezone",
        "title": "Timezone (IANA name)",
        "desc": "Timezone used for all-day Google Calendar events.",
        "type": "STRING",
        "isRequired": True,
        "defaultValue": DEFAULT_TIMEZONE,
    },
    {
        "name": "refresh_hours",
        "title": "Google refresh interval in hours (1, 2, or 4)",
        "desc": "Local schedule evaluation remains once per minute.",
        "type": "STRING",
        "isRequired": True,
        "defaultValue": "2",
    },
    {
        "name": "show_event_titles",
        "title": "Show event titles in Calendar Activity (true or false)",
        "desc": (
            "Privacy opt-in. Titles appear only in the authenticated eisy-ui "
            "Calendar Activity notice; default false."
        ),
        "type": "STRING",
        "isRequired": True,
        "defaultValue": "false",
    },
    {
        "name": "upcoming_event_limit",
        "title": "Upcoming events shown (5 or 10)",
        "desc": "Presentation only; does not change calendar evaluation.",
        "type": "STRING",
        "isRequired": True,
        "defaultValue": "5",
    },
    {
        "name": "calendars",
        "title": "Google Calendars",
        "desc": (
            "Calendar IDs used by every enabled rule. OAuth discovery is not "
            "enabled in this offline milestone."
        ),
        "isList": True,
        "params": [
            {
                "name": "calendar_id",
                "title": "Google Calendar ID",
                "type": "STRING",
                "isRequired": True,
            }
        ],
    },
    {
        "name": "rules",
        "title": "Calendar Rules",
        "desc": "Add, edit, or delete offline preview rules.",
        "isList": True,
        "params": [
            {
                "name": "name",
                "title": "Friendly rule name",
                "type": "STRING",
                "isRequired": True,
            },
            {
                "name": "match_field",
                "title": "Match field (title or location)",
                "desc": "Allowed: title, location. Missing legacy values use title.",
                "type": "STRING",
                "isRequired": True,
                "defaultValue": DEFAULT_MATCH_FIELD,
            },
            {
                "name": "title_contains",
                "title": "Event match pattern",
                "type": "STRING",
                "isRequired": True,
            },
            {
                "name": "title_match_type",
                "title": "Match type",
                "desc": "Allowed: contains, exact, starts_with, ends_with.",
                "type": "STRING",
                "isRequired": True,
                "defaultValue": DEFAULT_TITLE_MATCH_TYPE,
            },
            {
                "name": "case_sensitive",
                "title": "Case-sensitive match (true or false)",
                "type": "STRING",
                "isRequired": True,
                "defaultValue": "false",
            },
            {
                "name": "state_variable_id",
                "title": "Existing IoX type-2 state-variable ID",
                "desc": "Positive numeric ID; CalendarRules never creates variables.",
                "type": "STRING",
                "isRequired": True,
            },
            {
                "name": "start_offset_minutes",
                "title": "Start offset in minutes (-1440 to 1440)",
                "type": "STRING",
                "isRequired": True,
                "defaultValue": "0",
            },
            {
                "name": "end_offset_minutes",
                "title": "End offset in minutes (-1440 to 1440)",
                "type": "STRING",
                "isRequired": True,
                "defaultValue": "0",
            },
            {
                "name": "enabled",
                "title": "Rule enabled (true or false)",
                "type": "STRING",
                "isRequired": True,
                "defaultValue": "true",
            },
        ],
    },
]


@dataclass(frozen=True)
class UiConfiguration:
    """Validated typed UI data plus its offline schedule policy."""

    calendar: CalendarConfig
    policy: SchedulePolicy
    show_event_titles: bool = False
    upcoming_event_limit: int = DEFAULT_UPCOMING_EVENT_LIMIT

    def preview(
        self,
        snapshot: CalendarSnapshot,
        *,
        now: datetime,
    ) -> PreviewResult:
        """Evaluate one supplied snapshot without Google or IoX operations."""

        return preview_snapshot(
            self.calendar,
            snapshot,
            now=now,
            max_snapshot_age=self.policy.max_snapshot_age,
            require_coverage=True,
        )


def typed_parameter_schema() -> list[dict[str, Any]]:
    """Return a mutable copy of the documented eisy-ui typed schema."""

    return deepcopy(_TYPED_PARAMETER_SCHEMA)


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{path}: must be an object")
    return value


def _reject_unknown(
    value: Mapping[str, Any], allowed: set[str], path: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigurationError(f"{path}.{unknown[0]}: unknown field")


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{path}: must be a non-empty string")
    return value.strip()


def _integer(value: Any, path: str) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{path}: must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?[0-9]+", text):
            return int(text, 10)
    raise ConfigurationError(f"{path}: must be an integer")


def _boolean(value: Any, path: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().casefold()
        if text == "true":
            return True
        if text == "false":
            return False
    raise ConfigurationError(f"{path}: must be true or false")


def _rows(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{path}: must be a list")
    return value


def load_typed_configuration(
    data: Any,
    *,
    available_state_variable_ids: Collection[int] | None = None,
) -> UiConfiguration:
    """Validate one atomic CUSTOMTYPEDDATA payload.

    A supplied state-variable catalog is an offline validation boundary. When
    absent, IDs are still required to be positive integers. The runtime's
    separate authenticated read-only probe must then verify existence, type,
    and binary value before the write gate can open.
    """

    root = _mapping(data, "configuration")
    _reject_unknown(
        root,
        {
            "timezone",
            "refresh_hours",
            "show_event_titles",
            "upcoming_event_limit",
            "calendars",
            "rules",
        },
        "configuration",
    )

    timezone = _string(root.get("timezone", DEFAULT_TIMEZONE), "timezone")
    refresh_hours = _integer(root.get("refresh_hours", 2), "refresh_hours")
    if refresh_hours not in ALLOWED_REFRESH_HOURS:
        raise ConfigurationError("refresh_hours: must be 1, 2, or 4")
    upcoming_event_limit = _integer(
        root.get("upcoming_event_limit", DEFAULT_UPCOMING_EVENT_LIMIT),
        "upcoming_event_limit",
    )
    if upcoming_event_limit not in UPCOMING_EVENT_LIMITS:
        raise ConfigurationError("upcoming_event_limit: must be 5 or 10")

    calendar_ids = []
    seen_calendars = set()
    for index, item in enumerate(_rows(root.get("calendars"), "calendars")):
        path = f"calendars[{index}]"
        row = _mapping(item, path)
        _reject_unknown(row, {"calendar_id"}, path)
        calendar_id = _string(row.get("calendar_id"), f"{path}.calendar_id")
        if calendar_id in seen_calendars:
            raise ConfigurationError(f"{path}.calendar_id: duplicate calendar")
        seen_calendars.add(calendar_id)
        calendar_ids.append(calendar_id)
    if not calendar_ids:
        raise ConfigurationError("calendars: at least one calendar is required")

    catalog = None
    if available_state_variable_ids is not None:
        catalog = set(available_state_variable_ids)
        if any(isinstance(item, bool) or not isinstance(item, int) for item in catalog):
            raise ConfigurationError("state-variable catalog is invalid")

    rules = []
    names = set()
    for index, item in enumerate(_rows(root.get("rules"), "rules")):
        path = f"rules[{index}]"
        row = _mapping(item, path)
        _reject_unknown(
            row,
            {
                "name",
                "match_field",
                "title_contains",
                "title_match_type",
                "case_sensitive",
                "state_variable_id",
                "start_offset_minutes",
                "end_offset_minutes",
                "enabled",
            },
            path,
        )
        enabled = _boolean(row.get("enabled", True), f"{path}.enabled")
        match_field = _string(
            row.get("match_field", DEFAULT_MATCH_FIELD),
            f"{path}.match_field",
        )
        if match_field not in MATCH_FIELDS:
            raise ConfigurationError(
                f"{path}.match_field: must be title or location"
            )
        title_match_type = _string(
            row.get("title_match_type", DEFAULT_TITLE_MATCH_TYPE),
            f"{path}.title_match_type",
        )
        if title_match_type not in TITLE_MATCH_TYPES:
            raise ConfigurationError(
                f"{path}.title_match_type: must be contains, exact, "
                "starts_with, or ends_with"
            )
        if not enabled:
            continue
        name = _string(row.get("name"), f"{path}.name")
        folded_name = name.casefold()
        if folded_name in names:
            raise ConfigurationError(f"{path}.name: duplicate enabled rule name")
        names.add(folded_name)
        title_contains = _string(
            row.get("title_contains"), f"{path}.title_contains"
        )
        variable_id = _integer(
            row.get("state_variable_id"), f"{path}.state_variable_id"
        )
        if variable_id <= 0:
            raise ConfigurationError(
                f"{path}.state_variable_id: must be a positive integer"
            )
        if catalog is not None and variable_id not in catalog:
            raise ConfigurationError(
                f"{path}.state_variable_id: is not an available type-2 state variable"
            )
        start_offset = _integer(
            row.get("start_offset_minutes", 0),
            f"{path}.start_offset_minutes",
        )
        end_offset = _integer(
            row.get("end_offset_minutes", 0),
            f"{path}.end_offset_minutes",
        )
        for label, value in (
            ("start_offset_minutes", start_offset),
            ("end_offset_minutes", end_offset),
        ):
            if not MIN_OFFSET_MINUTES <= value <= MAX_OFFSET_MINUTES:
                raise ConfigurationError(
                    f"{path}.{label}: must be between "
                    f"{MIN_OFFSET_MINUTES} and {MAX_OFFSET_MINUTES}"
                )
        rules.append(
            {
                "name": name,
                "match": {
                    "title_contains": title_contains,
                    "title_match_type": title_match_type,
                    "match_field": match_field,
                    "case_sensitive": _boolean(
                        row.get("case_sensitive", False),
                        f"{path}.case_sensitive",
                    ),
                },
                "isy_state_variable_id": variable_id,
                "start_offset_minutes": start_offset,
                "end_offset_minutes": end_offset,
                "ignore_all_day": True,
                "ignore_cancelled": True,
            }
        )
    if not rules:
        raise ConfigurationError("rules: at least one enabled rule is required")

    calendar = load_config_data(
        {
            "calendar_ids": calendar_ids,
            "timezone": timezone,
            "poll_seconds": 60,
            "rules": rules,
        }
    )
    return UiConfiguration(
        calendar=calendar,
        policy=SchedulePolicy(refresh_hours=refresh_hours),
        show_event_titles=_boolean(
            root.get("show_event_titles", False), "show_event_titles"
        ),
        upcoming_event_limit=upcoming_event_limit,
    )
