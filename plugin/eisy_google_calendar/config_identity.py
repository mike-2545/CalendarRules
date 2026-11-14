"""Deterministic, non-secret identity for schedule-affecting configuration."""

from __future__ import annotations

import hashlib
import json

from .models import DEFAULT_MATCH_FIELD, DEFAULT_TITLE_MATCH_TYPE, CalendarConfig, Rule
from .validation import load_config_data


CONFIG_FINGERPRINT_VERSION = 1


def _canonical_rule(rule: Rule) -> dict:
    identity = {
        "name": rule.name,
        "title_contains": rule.title_contains,
        "target_variable_id": rule.target_variable_id,
        "start_offset_minutes": rule.start_offset_minutes,
        "end_offset_minutes": rule.end_offset_minutes,
        "case_sensitive": rule.case_sensitive,
        "ignore_all_day": rule.ignore_all_day,
        "ignore_cancelled": rule.ignore_cancelled,
    }
    # Omitting the default preserves the exact v1 identity of every legacy
    # Contains rule, including configurations that now state it explicitly.
    if rule.title_match_type != DEFAULT_TITLE_MATCH_TYPE:
        identity["title_match_type"] = rule.title_match_type
    # Title is the historical implicit field. Omitting it preserves the exact
    # v1 fingerprint of semantically unchanged legacy title rules.
    if rule.match_field != DEFAULT_MATCH_FIELD:
        identity["match_field"] = rule.match_field
    return identity


def _validated_identity(config: CalendarConfig) -> dict:
    raw = {
        "calendar_ids": list(config.calendar_ids),
        "timezone": config.timezone,
        "poll_seconds": config.poll_seconds,
        "rules": [
            {
                "name": rule.name,
                "match": {
                    "title_contains": rule.title_contains,
                    "title_match_type": rule.title_match_type,
                    "match_field": rule.match_field,
                    "case_sensitive": rule.case_sensitive,
                },
                "isy_state_variable_id": rule.target_variable_id,
                "start_offset_minutes": rule.start_offset_minutes,
                "end_offset_minutes": rule.end_offset_minutes,
                "ignore_all_day": rule.ignore_all_day,
                "ignore_cancelled": rule.ignore_cancelled,
            }
            for rule in config.rules
        ],
    }
    validated = load_config_data(raw)
    return {
        "version": CONFIG_FINGERPRINT_VERSION,
        "calendar_ids": list(validated.calendar_ids),
        "timezone": validated.timezone,
        "poll_seconds": validated.poll_seconds,
        "rules": [_canonical_rule(rule) for rule in validated.rules],
    }


def configuration_fingerprint(config: CalendarConfig) -> str:
    """Hash canonical validated configuration without returning private fields."""

    canonical = json.dumps(
        _validated_identity(config),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
