"""Validation for evaluator configuration and event inputs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    DEFAULT_MATCH_FIELD,
    DEFAULT_TITLE_MATCH_TYPE,
    MATCH_FIELDS,
    TITLE_MATCH_TYPES,
    CalendarConfig,
    CalendarEvent,
    Rule,
)


class ConfigurationError(ValueError):
    """Raised when evaluator configuration or input data is invalid."""


def normalize_variable_id(value: Any) -> int:
    """Return a positive integer IoX state-variable ID; reject bool explicitly."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError("target_variable_id must be a positive integer")
    if value <= 0:
        raise ConfigurationError("target_variable_id must be a positive integer")
    return value


def normalize_utc(value: datetime, label: str) -> datetime:
    """Reject naive timestamps and return an aware UTC timestamp."""

    if not isinstance(value, datetime):
        raise ConfigurationError(f"{label} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ConfigurationError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def validate_rule(rule: Rule) -> Rule:
    """Validate and normalize one rule."""

    if not isinstance(rule.name, str) or not rule.name.strip():
        raise ConfigurationError("rule name must be a non-empty string")
    if not isinstance(rule.title_contains, str) or not rule.title_contains:
        raise ConfigurationError("title_contains must be a non-empty string")
    if (
        not isinstance(rule.title_match_type, str)
        or rule.title_match_type not in TITLE_MATCH_TYPES
    ):
        raise ConfigurationError(
            "title_match_type must be contains, exact, starts_with, or ends_with"
        )
    if not isinstance(rule.match_field, str) or rule.match_field not in MATCH_FIELDS:
        raise ConfigurationError("match_field must be title or location")
    if not isinstance(rule.start_offset_minutes, int) or isinstance(rule.start_offset_minutes, bool):
        raise ConfigurationError("start_offset_minutes must be an integer")
    if not isinstance(rule.end_offset_minutes, int) or isinstance(rule.end_offset_minutes, bool):
        raise ConfigurationError("end_offset_minutes must be an integer")
    if not isinstance(rule.case_sensitive, bool):
        raise ConfigurationError("case_sensitive must be boolean")
    if not isinstance(rule.ignore_all_day, bool):
        raise ConfigurationError("ignore_all_day must be boolean")
    if not isinstance(rule.ignore_cancelled, bool):
        raise ConfigurationError("ignore_cancelled must be boolean")

    variable_id = normalize_variable_id(rule.target_variable_id)
    if variable_id == rule.target_variable_id:
        return rule
    return Rule(
        name=rule.name,
        title_contains=rule.title_contains,
        target_variable_id=variable_id,
        start_offset_minutes=rule.start_offset_minutes,
        end_offset_minutes=rule.end_offset_minutes,
        case_sensitive=rule.case_sensitive,
        ignore_all_day=rule.ignore_all_day,
        ignore_cancelled=rule.ignore_cancelled,
        title_match_type=rule.title_match_type,
        match_field=rule.match_field,
    )


def validate_event(event: CalendarEvent) -> CalendarEvent:
    """Validate an event using actual UTC instants for ordering."""

    if not isinstance(event.title, str):
        raise ConfigurationError("event title must be a string")
    if not isinstance(event.location, str):
        raise ConfigurationError("event location must be a string")
    start_utc = normalize_utc(event.start, f"event {event.event_id} start")
    end_utc = normalize_utc(event.end, f"event {event.event_id} end")
    if end_utc <= start_utc:
        raise ConfigurationError("event end must be after event start")
    if not isinstance(event.all_day, bool) or not isinstance(event.cancelled, bool):
        raise ConfigurationError("event flags must be boolean")
    return CalendarEvent(
        event_id=event.event_id,
        title=event.title,
        start=start_utc,
        end=end_utc,
        all_day=event.all_day,
        cancelled=event.cancelled,
        location=event.location,
    )


_MISSING = object()


def _setting_error(path: str, message: str) -> ConfigurationError:
    return ConfigurationError(f"{path}: {message}")


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _setting_error(path, "must be an object")
    return value


def _required(mapping: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in mapping:
        raise _setting_error(path, "is required")
    return mapping[key]


def _reject_unknown(mapping: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise _setting_error(f"{path}.{unknown[0]}", "unknown field")


def _string(value: Any, path: str, *, non_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise _setting_error(path, "must be a string")
    if non_empty and not value.strip():
        raise _setting_error(path, "must be a non-empty string")
    return value


def _strict_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _setting_error(path, "must be an integer")
    return value


def _strict_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise _setting_error(path, "must be a boolean")
    return value


def _calendar_ids(value: Any) -> tuple[str, ...]:
    path = "calendar_ids"
    if not isinstance(value, list):
        raise _setting_error(path, "must be a non-empty list of strings")
    if not value:
        raise _setting_error(path, "must be a non-empty list")
    result = []
    for index, calendar_id in enumerate(value):
        result.append(_string(calendar_id, f"{path}[{index}]"))
    return tuple(result)


def _timezone_name(value: Any) -> str:
    path = "timezone"
    name = _string(value, path)
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise _setting_error(path, "must be a valid IANA timezone name") from None
    return name


def _load_rule(value: Any, index: int) -> Rule:
    path = f"rules[{index}]"
    mapping = _mapping(value, path)
    _reject_unknown(
        mapping,
        {
            "name",
            "match",
            "isy_state_variable_id",
            "start_offset_minutes",
            "end_offset_minutes",
            "ignore_all_day",
            "ignore_cancelled",
        },
        path,
    )

    name = _string(_required(mapping, "name", f"{path}.name"), f"{path}.name")
    match_path = f"{path}.match"
    match = _mapping(_required(mapping, "match", match_path), match_path)
    _reject_unknown(
        match,
        {"title_contains", "title_match_type", "match_field", "case_sensitive"},
        match_path,
    )
    title_contains = _string(
        _required(match, "title_contains", f"{match_path}.title_contains"),
        f"{match_path}.title_contains",
    )
    case_sensitive = _strict_bool(
        match.get("case_sensitive", False), f"{match_path}.case_sensitive"
    )
    title_match_type = _string(
        match.get("title_match_type", DEFAULT_TITLE_MATCH_TYPE),
        f"{match_path}.title_match_type",
    )
    match_field = _string(
        match.get("match_field", DEFAULT_MATCH_FIELD),
        f"{match_path}.match_field",
    )

    variable_path = f"{path}.isy_state_variable_id"
    variable_id = _strict_int(_required(mapping, "isy_state_variable_id", variable_path), variable_path)
    if variable_id <= 0:
        raise _setting_error(variable_path, "must be a positive integer")

    start_path = f"{path}.start_offset_minutes"
    end_path = f"{path}.end_offset_minutes"
    start_offset = _strict_int(mapping.get("start_offset_minutes", 0), start_path)
    end_offset = _strict_int(mapping.get("end_offset_minutes", 0), end_path)
    ignore_all_day = _strict_bool(mapping.get("ignore_all_day", True), f"{path}.ignore_all_day")
    ignore_cancelled = _strict_bool(mapping.get("ignore_cancelled", True), f"{path}.ignore_cancelled")

    return validate_rule(
        Rule(
            name=name,
            title_contains=title_contains,
            target_variable_id=variable_id,
            start_offset_minutes=start_offset,
            end_offset_minutes=end_offset,
            case_sensitive=case_sensitive,
            ignore_all_day=ignore_all_day,
            ignore_cancelled=ignore_cancelled,
            title_match_type=title_match_type,
            match_field=match_field,
        )
    )


def load_config_data(data: Mapping[str, Any]) -> CalendarConfig:
    """Parse and validate a configuration mapping into ``CalendarConfig``."""

    root = _mapping(data, "config")
    _reject_unknown(root, {"note", "calendar_ids", "timezone", "poll_seconds", "rules"}, "config")

    calendar_ids = _calendar_ids(_required(root, "calendar_ids", "calendar_ids"))
    timezone_name = _timezone_name(_required(root, "timezone", "timezone"))
    poll_seconds = _strict_int(_required(root, "poll_seconds", "poll_seconds"), "poll_seconds")
    if poll_seconds <= 0:
        raise _setting_error("poll_seconds", "must be a positive integer")

    rules_value = _required(root, "rules", "rules")
    if not isinstance(rules_value, list):
        raise _setting_error("rules", "must be a non-empty list")
    if not rules_value:
        raise _setting_error("rules", "must be a non-empty list")

    rules = []
    names = set()
    for index, value in enumerate(rules_value):
        parsed = _load_rule(value, index)
        folded_name = parsed.name.casefold()
        if folded_name in names:
            raise _setting_error(f"rules[{index}].name", "duplicates another rule name")
        names.add(folded_name)
        rules.append(parsed)

    return CalendarConfig(
        calendar_ids=calendar_ids,
        timezone=timezone_name,
        poll_seconds=poll_seconds,
        rules=tuple(rules),
    )


def load_config(path: str | Path) -> CalendarConfig:
    """Read a JSON configuration file and return validated configuration."""

    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        raise ConfigurationError(f"config: file not found: {source}") from None
    except OSError as exc:
        raise ConfigurationError(f"config: cannot read file {source}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"config: invalid JSON at line {exc.lineno}, column {exc.colno}") from None
    return load_config_data(data)
