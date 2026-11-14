from datetime import datetime, timedelta, timezone

import pytest

from eisy_google_calendar.models import (
    CalendarConfig,
    CalendarEvent,
    CalendarSnapshot,
    Rule,
    SnapshotStatus,
)
from eisy_google_calendar.preview import (
    format_preview,
    preview_google_responses,
    preview_snapshot,
)
from eisy_google_calendar.validation import ConfigurationError


UTC = timezone.utc
NOW = datetime(2025, 3, 9, 7, 15, tzinfo=UTC)


def _config(*rules):
    return CalendarConfig(
        ("synthetic-primary",),
        "America/New_York",
        60,
        tuple(rules or (Rule("workshop-rule", "Workshop", 101),)),
    )


def _timed(
    event_id,
    summary="Workshop",
    start="2025-03-09T01:30:00-05:00",
    end="2025-03-09T04:00:00-04:00",
    status="confirmed",
    **extra,
):
    return {
        "id": event_id,
        "summary": summary,
        "status": status,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        **extra,
    }


def _one_page(items):
    def fetch_page(calendar_id, params):
        assert calendar_id == "synthetic-primary"
        assert params["singleEvents"] is True
        return {"items": items}

    return fetch_page


def test_preview_handles_dst_overlap_recurring_cancellation_all_day_and_offsets():
    config = _config(
        Rule("workshop-rule", "Workshop", 101),
        Rule("office-rule", "Office", 202, start_offset_minutes=-30),
        Rule("board-rule", "Board", 303),
    )
    items = [
        _timed(
            "recurring-instance",
            recurringEventId="synthetic-series",
            originalStartTime={"dateTime": "2025-03-09T01:30:00-05:00"},
        ),
        _timed(
            "overlap",
            start="2025-03-09T03:00:00-04:00",
            end="2025-03-09T04:30:00-04:00",
        ),
        _timed("cancelled", status="cancelled"),
        {
            "id": "all-day",
            "summary": "Workshop holiday",
            "status": "confirmed",
            "start": {"date": "2025-03-09"},
            "end": {"date": "2025-03-10"},
        },
        _timed(
            "early-offset",
            summary="Office coverage",
            start="2025-03-09T07:30:00Z",
            end="2025-03-09T08:30:00Z",
        ),
        _timed(
            "later",
            summary="Board session",
            start="2025-03-09T09:00:00Z",
            end="2025-03-09T10:00:00Z",
        ),
    ]

    result = preview_google_responses(config, _one_page(items), now=NOW)

    assert result.trusted is True
    assert result.snapshot_status is SnapshotStatus.VALID
    assert result.desired_states == {101: 1, 202: 1, 303: 0}
    assert result.matched_rules == {
        101: ("workshop-rule",),
        202: ("office-rule",),
    }
    assert result.active_match_counts == {101: 2, 202: 1}
    assert result.ignored_reasons == {"cancelled": 1, "all_day": 1}


def test_valid_empty_calendar_is_the_only_empty_input_that_can_turn_rule_off():
    result = preview_google_responses(_config(), _one_page([]), now=NOW)
    assert result.trusted is True
    assert result.desired_states == {101: 0}
    assert result.source_event_count == 0


@pytest.mark.parametrize(
    "status",
    [
        SnapshotStatus.FAILED,
        SnapshotStatus.UNAVAILABLE,
        SnapshotStatus.STALE,
        SnapshotStatus.OUT_OF_COVERAGE,
    ],
)
def test_untrusted_status_never_produces_false_off(status):
    snapshot = CalendarSnapshot(status, error="private provider response")
    result = preview_snapshot(_config(), snapshot, now=NOW)
    assert result.trusted is False
    assert result.desired_states == {}
    assert result.source_event_count is None
    assert "private provider response" not in (result.reason or "")


def test_partial_page_failure_discards_previously_received_events_and_redacts_error():
    def fetch_page(calendar_id, params):
        if "pageToken" not in params:
            return {"items": [_timed("first")], "nextPageToken": "second"}
        raise RuntimeError("secret-token private-calendar-title")

    result = preview_google_responses(_config(), fetch_page, now=NOW)
    assert result.snapshot_status is SnapshotStatus.FAILED
    assert result.trusted is False
    assert result.desired_states == {}
    rendered = format_preview(result)
    assert "secret-token" not in rendered
    assert "private-calendar-title" not in rendered


@pytest.mark.parametrize(
    "page",
    [
        {"items": "not-a-list"},
        {"items": [{"id": "incomplete", "summary": "Workshop"}]},
        {"items": [None]},
    ],
)
def test_malformed_or_incomplete_google_pages_are_untrusted(page):
    result = preview_google_responses(
        _config(), lambda calendar_id, params: page, now=NOW
    )
    assert result.trusted is False
    assert result.desired_states == {}
    assert result.snapshot_status is SnapshotStatus.FAILED


def test_valid_snapshot_without_completion_time_is_unavailable_not_empty():
    snapshot = CalendarSnapshot(SnapshotStatus.VALID, events=())
    result = preview_snapshot(_config(), snapshot, now=NOW)
    assert result.snapshot_status is SnapshotStatus.UNAVAILABLE
    assert result.trusted is False
    assert result.desired_states == {}


def test_snapshot_age_limit_is_explicit_and_boundary_is_inclusive():
    exactly_at_limit = CalendarSnapshot(
        SnapshotStatus.VALID,
        events=(),
        fetched_at=NOW - timedelta(minutes=30),
    )
    just_beyond_limit = CalendarSnapshot(
        SnapshotStatus.VALID,
        events=(),
        fetched_at=NOW - timedelta(minutes=30, microseconds=1),
    )
    boundary = preview_snapshot(
        _config(),
        exactly_at_limit,
        now=NOW,
        max_snapshot_age=timedelta(minutes=30),
    )
    stale = preview_snapshot(
        _config(),
        just_beyond_limit,
        now=NOW,
        max_snapshot_age=timedelta(minutes=30),
    )
    assert boundary.trusted is True
    assert boundary.desired_states == {101: 0}
    assert stale.snapshot_status is SnapshotStatus.STALE
    assert stale.trusted is False
    assert stale.desired_states == {}


def test_snapshot_age_expiry_is_disabled_without_an_explicit_limit():
    snapshot = CalendarSnapshot(
        SnapshotStatus.VALID,
        events=(),
        fetched_at=NOW - timedelta(days=30),
    )
    result = preview_snapshot(_config(), snapshot, now=NOW)
    assert result.trusted is True
    assert result.desired_states == {101: 0}


def test_coverage_boundary_is_inclusive_and_beyond_it_withholds_off():
    snapshot = CalendarSnapshot(
        SnapshotStatus.VALID,
        events=(),
        fetched_at=NOW,
        coverage_start=NOW,
        coverage_end=NOW + timedelta(hours=4),
    )
    boundary = preview_snapshot(
        _config(),
        snapshot,
        now=NOW + timedelta(hours=4),
        max_snapshot_age=timedelta(hours=8),
        require_coverage=True,
    )
    beyond = preview_snapshot(
        _config(),
        snapshot,
        now=NOW + timedelta(hours=4, microseconds=1),
        max_snapshot_age=timedelta(hours=8),
        require_coverage=True,
    )
    assert boundary.trusted is True
    assert boundary.desired_states == {101: 0}
    assert beyond.snapshot_status is SnapshotStatus.OUT_OF_COVERAGE
    assert beyond.trusted is False
    assert beyond.desired_states == {}


def test_invalid_snapshot_age_limit_is_rejected():
    snapshot = CalendarSnapshot(SnapshotStatus.VALID, (), NOW)
    with pytest.raises(ConfigurationError, match="positive timedelta"):
        preview_snapshot(
            _config(), snapshot, now=NOW, max_snapshot_age=timedelta(0)
        )


def test_changed_moved_and_deleted_events_recompute_from_complete_snapshots():
    active = preview_google_responses(
        _config(), _one_page([_timed("instance")]), now=NOW
    )
    moved = preview_google_responses(
        _config(),
        _one_page(
            [
                _timed(
                    "instance",
                    start="2025-03-09T09:00:00Z",
                    end="2025-03-09T10:00:00Z",
                )
            ]
        ),
        now=NOW,
    )
    deleted = preview_google_responses(_config(), _one_page([]), now=NOW)
    assert active.desired_states == {101: 1}
    assert moved.desired_states == {101: 0}
    assert deleted.desired_states == {101: 0}


def test_restart_mid_event_and_after_event_is_deterministic():
    pages = _one_page(
        [
            _timed(
                "instance",
                start="2025-03-09T07:00:00Z",
                end="2025-03-09T08:00:00Z",
            )
        ]
    )
    during = preview_google_responses(_config(), pages, now=NOW)
    after = preview_google_responses(
        _config(), pages, now=datetime(2025, 3, 9, 8, tzinfo=UTC)
    )
    assert during.desired_states == {101: 1}
    assert after.desired_states == {101: 0}


def test_malformed_normalized_event_is_redacted_and_never_interpreted_as_empty():
    snapshot = CalendarSnapshot(
        SnapshotStatus.VALID,
        events=(
            CalendarEvent(
                "private-event-id",
                None,  # type: ignore[arg-type]
                NOW,
                NOW + timedelta(hours=1),
            ),
        ),
        fetched_at=NOW,
    )
    result = preview_snapshot(_config(), snapshot, now=NOW)
    assert result.snapshot_status is SnapshotStatus.FAILED
    assert result.trusted is False
    assert result.desired_states == {}
    assert "private-event-id" not in (result.reason or "")


def test_formatter_uses_rule_names_and_counts_but_not_event_titles_or_ids():
    result = preview_google_responses(
        _config(),
        _one_page(
            [
                _timed(
                    "private-provider-id",
                    summary="Workshop private customer name",
                )
            ]
        ),
        now=NOW,
    )
    rendered = format_preview(result)
    assert "workshop-rule" in rendered
    assert "private-provider-id" not in rendered
    assert "private customer name" not in rendered
