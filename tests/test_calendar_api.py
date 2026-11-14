from datetime import datetime, timedelta, timezone
import pytest

from eisy_google_calendar.calendar_api import (
    FETCH_BOUNDARY_BUFFER,
    calculate_fetch_window,
    fetch_snapshot,
)
from eisy_google_calendar.models import CalendarConfig, Rule, SnapshotStatus

UTC = timezone.utc
NOW = datetime(2025, 1, 1, 12, tzinfo=UTC)


def rule(name, start=0, end=0, target=1):
    return Rule(name, name, target, start_offset_minutes=start, end_offset_minutes=end)


def config(*calendar_ids, rules=(rule("Meeting"),), timezone_name="UTC"):
    return CalendarConfig(tuple(calendar_ids or ("primary",)), timezone_name, 60, tuple(rules))


def timed(event_id="e1", summary="Meeting", start="2025-01-01T12:00:00Z", end="2025-01-01T13:00:00Z", status="confirmed"):
    return {
        "id": event_id,
        "summary": summary,
        "status": status,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }


def all_day(event_id="e1", summary="Meeting", start="2025-01-01", end="2025-01-02", status="confirmed"):
    return {
        "id": event_id,
        "summary": summary,
        "status": status,
        "start": {"date": start},
        "end": {"date": end},
    }


class Pages:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, calendar_id, params):
        self.calls.append((calendar_id, dict(params)))
        token = params.get("pageToken")
        return self.responses[(calendar_id, token)]


def test_fetch_window_expands_positive_end_offset_into_past():
    start, end = calculate_fetch_window(now=NOW, config=config(rules=(rule("Meeting", end=30),)))
    assert start == NOW - timedelta(minutes=30) - FETCH_BOUNDARY_BUFFER
    assert end == NOW + FETCH_BOUNDARY_BUFFER


def test_fetch_window_expands_large_positive_end_offset():
    start, end = calculate_fetch_window(now=NOW, config=config(rules=(rule("Meeting", end=90),)))
    assert start == NOW - timedelta(minutes=90) - FETCH_BOUNDARY_BUFFER
    assert end == NOW + FETCH_BOUNDARY_BUFFER


def test_fetch_window_expands_negative_start_offset_into_future():
    start, end = calculate_fetch_window(now=NOW, config=config(rules=(rule("Meeting", start=-30),)))
    assert start == NOW - FETCH_BOUNDARY_BUFFER
    assert end == NOW + timedelta(minutes=30) + FETCH_BOUNDARY_BUFFER


def test_fetch_window_expands_large_negative_start_offset():
    start, end = calculate_fetch_window(now=NOW, config=config(rules=(rule("Meeting", start=-120),)))
    assert start == NOW - FETCH_BOUNDARY_BUFFER
    assert end == NOW + timedelta(minutes=120) + FETCH_BOUNDARY_BUFFER


def test_zero_offsets_only_use_boundary_buffer():
    start, end = calculate_fetch_window(now=NOW, config=config())
    assert (start, end) == (NOW - FETCH_BOUNDARY_BUFFER, NOW + FETCH_BOUNDARY_BUFFER)


def test_mixed_rule_offsets_use_widest_expansion():
    rules = (rule("A", start=-30, end=90), rule("B", start=-120, end=15))
    start, end = calculate_fetch_window(now=NOW, config=config(rules=rules))
    assert start == NOW - timedelta(minutes=90) - FETCH_BOUNDARY_BUFFER
    assert end == NOW + timedelta(minutes=120) + FETCH_BOUNDARY_BUFFER


def test_32_day_coverage_with_maximum_rule_offsets_stays_under_google_limit():
    start, end = calculate_fetch_window(
        now=NOW,
        config=config(rules=(rule("Meeting", start=-1440, end=1440),)),
        coverage_horizon=timedelta(days=32),
    )
    assert end - start < timedelta(days=40)


@pytest.mark.parametrize("hours", [0.5, 2, 4, 24])
def test_cached_fetch_window_covers_explicit_future_horizon(hours):
    horizon = timedelta(hours=hours)
    start, end = calculate_fetch_window(
        now=NOW,
        config=config(rules=(rule("Meeting", start=-30, end=90),)),
        coverage_horizon=horizon,
    )
    assert start == NOW - timedelta(minutes=90) - FETCH_BOUNDARY_BUFFER
    assert end == NOW + horizon + timedelta(minutes=30) + FETCH_BOUNDARY_BUFFER


def test_cached_snapshot_records_complete_evaluation_coverage():
    pages = Pages({("primary", None): {"items": []}})
    result = fetch_snapshot(
        config(),
        fetch_page=pages,
        now=NOW,
        coverage_horizon=timedelta(hours=24),
    )
    assert result.coverage_start == NOW
    assert result.coverage_end == NOW + timedelta(hours=24)


def test_negative_cached_fetch_horizon_is_rejected_without_partial_data():
    result = fetch_snapshot(
        config(),
        fetch_page=lambda calendar_id, params: {"items": []},
        now=NOW,
        coverage_horizon=timedelta(seconds=-1),
    )
    assert result.status is SnapshotStatus.FAILED
    assert result.events == ()


def test_google_time_bounds_are_sent_as_utc_rfc3339_and_flags_are_fixed():
    pages = Pages({("primary", None): {"items": []}})
    result = fetch_snapshot(config("primary", rules=(rule("Meeting", start=-30, end=90),)), fetch_page=pages, now=NOW)
    assert result.status is SnapshotStatus.VALID
    calendar_id, params = pages.calls[0]
    assert calendar_id == "primary"
    assert params == {
        "singleEvents": True,
        "showDeleted": False,
        "timeMin": "2025-01-01T10:29:00Z",
        "timeMax": "2025-01-01T12:31:00Z",
    }


def test_early_activation_event_is_retrieved_by_future_time_max():
    pages = Pages({("primary", None): {"items": [timed(start="2025-01-01T12:30:00Z", end="2025-01-01T13:00:00Z")]}})
    result = fetch_snapshot(config("primary", rules=(rule("Meeting", start=-30),)), fetch_page=pages, now=NOW)
    assert result.status is SnapshotStatus.VALID
    assert len(result.events) == 1
    assert pages.calls[0][1]["timeMax"] == "2025-01-01T12:31:00Z"


def test_delayed_deactivation_event_is_retrieved_by_past_time_min():
    pages = Pages({("primary", None): {"items": [timed(start="2025-01-01T11:00:00Z", end="2025-01-01T11:30:00Z")]}})
    result = fetch_snapshot(config("primary", rules=(rule("Meeting", end=90),)), fetch_page=pages, now=NOW)
    assert result.status is SnapshotStatus.VALID
    assert len(result.events) == 1
    assert pages.calls[0][1]["timeMin"] == "2025-01-01T10:29:00Z"


def test_all_pages_and_calendars_must_complete():
    pages = Pages({
        ("one", None): {"items": [timed("a")], "nextPageToken": "p2"},
        ("one", "p2"): {"items": [timed("b")]},
        ("two", None): {"items": [timed("c")]},
    })
    result = fetch_snapshot(config("one", "two"), fetch_page=pages, now=NOW)
    assert result.status is SnapshotStatus.VALID
    assert {event.event_id for event in result.events} == {"one:a", "one:b", "two:c"}
    assert [call[1].get("pageToken") for call in pages.calls] == [None, "p2", None]


def test_complete_empty_snapshot_is_valid():
    pages = Pages({("primary", None): {"items": []}})
    result = fetch_snapshot(config(), fetch_page=pages, now=NOW)
    assert result.status is SnapshotStatus.VALID
    assert result.events == ()


@pytest.mark.parametrize("failure_page", [None, "p2"])
def test_fetch_failure_returns_failed_with_no_partial_events(failure_page):
    class Failing:
        def __init__(self):
            self.calls = 0

        def __call__(self, calendar_id, params):
            self.calls += 1
            if (failure_page is None and self.calls == 1) or failure_page == params.get("pageToken"):
                raise RuntimeError("offline fetch failure")
            return {"items": [timed("a")], "nextPageToken": "p2"}

    result = fetch_snapshot(config(), fetch_page=Failing(), now=NOW)
    assert result.status is SnapshotStatus.FAILED
    assert result.events == ()
    assert result.error == "calendar snapshot could not be completed"


def test_malformed_active_event_fails_snapshot():
    bad = timed()
    del bad["end"]
    pages = Pages({("primary", None): {"items": [bad]}})
    result = fetch_snapshot(config(), fetch_page=pages, now=NOW)
    assert result.status is SnapshotStatus.FAILED
    assert result.events == ()
    assert result.error == "calendar snapshot could not be completed"


def test_cancelled_timestamp_less_recurring_exception_is_skipped():
    cancelled = {"id": "series_20250101", "status": "cancelled", "recurringEventId": "series"}
    pages = Pages({("primary", None): {"items": [cancelled, timed("active")]}})
    result = fetch_snapshot(config(), fetch_page=pages, now=NOW)
    assert result.status is SnapshotStatus.VALID
    assert [event.event_id for event in result.events] == ["primary:active"]


def test_cancelled_complete_event_is_retained_without_inventing_times():
    pages = Pages({("primary", None): {"items": [timed("cancelled", status="cancelled")]}})
    result = fetch_snapshot(config(), fetch_page=pages, now=NOW)
    assert result.status is SnapshotStatus.VALID
    assert result.events[0].cancelled is True


def test_later_snapshot_is_independent_after_cancelled_exception():
    cancelled = {"id": "series_20250101", "status": "cancelled", "recurringEventId": "series"}
    first = Pages({("primary", None): {"items": [cancelled]}})
    second = Pages({("primary", None): {"items": [timed("new")]}})
    first_result = fetch_snapshot(config(), fetch_page=first, now=NOW)
    second_result = fetch_snapshot(config(), fetch_page=second, now=NOW)
    assert first_result.status is SnapshotStatus.VALID and first_result.events == ()
    assert second_result.status is SnapshotStatus.VALID
    assert [event.event_id for event in second_result.events] == ["primary:new"]


def test_rfc3339_offsets_are_normalized_to_utc():
    pages = Pages({("primary", None): {"items": [timed(start="2025-01-01T07:00:00-05:00", end="2025-01-01T08:00:00-05:00")]}})
    result = fetch_snapshot(config(), fetch_page=pages, now=NOW)
    assert result.events[0].start == datetime(2025, 1, 1, 12, tzinfo=UTC)
    assert result.events[0].end == datetime(2025, 1, 1, 13, tzinfo=UTC)


def test_all_day_dates_use_configured_timezone_and_exclusive_end():
    pages = Pages({("primary", None): {"items": [all_day()]}})
    result = fetch_snapshot(config(timezone_name="America/New_York"), fetch_page=pages, now=NOW)
    event = result.events[0]
    assert event.all_day is True
    assert event.start == datetime(2025, 1, 1, 5, tzinfo=UTC)
    assert event.end == datetime(2025, 1, 2, 5, tzinfo=UTC)


def test_same_event_id_on_different_calendars_is_distinct():
    pages = Pages({("one", None): {"items": [timed("same")]}, ("two", None): {"items": [timed("same")]}})
    result = fetch_snapshot(config("one", "two"), fetch_page=pages, now=NOW)
    assert {event.event_id for event in result.events} == {"one:same", "two:same"}


def test_identical_duplicate_across_pages_is_retained_once():
    pages = Pages({
        ("primary", None): {"items": [timed("same")], "nextPageToken": "p2"},
        ("primary", "p2"): {"items": [timed("same")]},
    })
    result = fetch_snapshot(config(), fetch_page=pages, now=NOW)
    assert result.status is SnapshotStatus.VALID
    assert [event.event_id for event in result.events] == ["primary:same"]


def test_conflicting_duplicate_across_pages_fails_without_events():
    pages = Pages({
        ("primary", None): {"items": [timed("same", summary="Meeting")], "nextPageToken": "p2"},
        ("primary", "p2"): {"items": [timed("same", summary="Changed")]},
    })
    result = fetch_snapshot(config(), fetch_page=pages, now=NOW)
    assert result.status is SnapshotStatus.FAILED
    assert result.events == ()
    assert result.error == "calendar snapshot could not be completed"


def test_repeated_page_token_fails_without_events():
    pages = Pages({
        ("primary", None): {"items": [timed("a")], "nextPageToken": "same"},
        ("primary", "same"): {"items": [timed("b")], "nextPageToken": "same"},
    })
    result = fetch_snapshot(config(), fetch_page=pages, now=NOW)
    assert result.status is SnapshotStatus.FAILED
    assert result.events == ()
    assert result.error == "calendar snapshot could not be completed"


def test_first_calendar_success_second_calendar_failure_discards_partial_snapshot():
    class TwoCalendarFetcher:
        def __call__(self, calendar_id, params):
            if calendar_id == "first":
                return {"items": [timed("partial")]}
            raise RuntimeError("second calendar unavailable")

    result = fetch_snapshot(
        config("first", "second"),
        fetch_page=TwoCalendarFetcher(),
        now=NOW,
    )
    assert result.status is SnapshotStatus.FAILED
    assert result.events == ()
    assert result.error is not None
    assert result.error == "calendar snapshot could not be completed"


def test_adapter_to_evaluator_early_activation_with_negative_start_offset():
    from eisy_google_calendar.evaluator import evaluate_snapshot

    pages = Pages({
        ("primary", None): {
            "items": [timed("early", start="2025-01-01T12:30:00Z", end="2025-01-01T13:00:00Z")]
        }
    })
    snapshot = fetch_snapshot(
        config("primary", rules=(rule("Meeting", start=-30, target=10),)),
        fetch_page=pages,
        now=NOW,
    )
    result = evaluate_snapshot(snapshot, config("primary", rules=(rule("Meeting", start=-30, target=10),)).rules, NOW)
    assert snapshot.status is SnapshotStatus.VALID
    assert result.trusted is True
    assert result.desired_states == {10: 1}


def test_adapter_to_evaluator_delayed_deactivation_with_positive_end_offset():
    from eisy_google_calendar.evaluator import evaluate_snapshot

    pages = Pages({
        ("primary", None): {
            "items": [timed("late", start="2025-01-01T11:00:00Z", end="2025-01-01T11:31:00Z")]
        }
    })
    rules = (rule("Meeting", end=30, target=10),)
    snapshot = fetch_snapshot(
        config("primary", rules=rules),
        fetch_page=pages,
        now=NOW,
    )
    result = evaluate_snapshot(snapshot, rules, NOW)
    assert snapshot.status is SnapshotStatus.VALID
    assert result.trusted is True
    assert result.desired_states == {10: 1}


def test_all_day_new_york_spring_forward_uses_local_midnight_boundaries():
    pages = Pages({
        ("primary", None): {
            "items": [all_day("spring-day", start="2025-03-08", end="2025-03-10")]
        }
    })
    result = fetch_snapshot(
        config("primary", timezone_name="America/New_York"),
        fetch_page=pages,
        now=NOW,
    )
    event = result.events[0]
    assert result.status is SnapshotStatus.VALID
    assert event.start == datetime(2025, 3, 8, 5, tzinfo=UTC)
    assert event.end == datetime(2025, 3, 10, 4, tzinfo=UTC)
    assert event.end - event.start == timedelta(hours=47)


@pytest.mark.parametrize(
    "provider_detail",
    (
        "SENSITIVE_TRANSPORT_EXCEPTION_PAYLOAD",
        "SENSITIVE_CALENDAR_REQUEST_URL",
        "SENSITIVE_PROVIDER_AUTH_HEADER",
    ),
)
def test_transport_failure_discards_untrusted_exception_text(provider_detail):
    def failing_provider(_calendar_id, _params):
        raise RuntimeError(provider_detail)

    result = fetch_snapshot(
        config("SENSITIVE_CALENDAR_IDENTIFIER"),
        fetch_page=failing_provider,
        now=NOW,
    )
    assert result.status is SnapshotStatus.FAILED
    assert result.events == ()
    assert result.error == "calendar snapshot could not be completed"
    assert provider_detail not in repr(result)
    assert "SENSITIVE_CALENDAR_IDENTIFIER" not in repr(result)


def test_malformed_event_does_not_expose_identifiers_in_failed_snapshot():
    calendar_id = "SENSITIVE_CALENDAR_IDENTIFIER"
    event_id = "SENSITIVE_EVENT_IDENTIFIER"
    pages = Pages({
        (calendar_id, None): {
            "items": [{"id": event_id, "summary": "SENSITIVE_EVENT_TITLE", "start": {"dateTime": "2025-01-01T12:00:00Z"}}]
        }
    })
    result = fetch_snapshot(config(calendar_id), fetch_page=pages, now=NOW)
    assert result.status is SnapshotStatus.FAILED
    assert result.events == ()
    assert result.error == "calendar snapshot could not be completed"
    for marker in (calendar_id, event_id, "SENSITIVE_EVENT_TITLE"):
        assert marker not in repr(result)


def test_repeated_pagination_marker_is_not_exposed_in_failed_snapshot():
    marker = "SENSITIVE_PAGINATION_MARKER"
    pages = Pages({
        ("primary", None): {"items": [], "nextPageToken": marker},
        ("primary", marker): {"items": [], "nextPageToken": marker},
    })
    result = fetch_snapshot(config(), fetch_page=pages, now=NOW)
    assert result.status is SnapshotStatus.FAILED
    assert result.events == ()
    assert result.error == "calendar snapshot could not be completed"
    assert marker not in repr(result)
