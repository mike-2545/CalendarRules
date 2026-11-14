"""Readable, deterministic CalendarRules preview using synthetic Google data."""

from datetime import datetime, timezone

from eisy_google_calendar.models import CalendarConfig, Rule
from eisy_google_calendar.preview import format_preview, preview_google_responses


EVALUATION_TIME = datetime(2025, 3, 9, 7, 15, tzinfo=timezone.utc)
SYNTHETIC_EVENTS = [
    {
        "id": "synthetic-recurring-instance",
        "summary": "Workshop setup",
        "status": "confirmed",
        "recurringEventId": "synthetic-workshop-series",
        "start": {"dateTime": "2025-03-09T01:30:00-05:00"},
        "end": {"dateTime": "2025-03-09T04:00:00-04:00"},
    },
    {
        "id": "synthetic-overlap",
        "summary": "Workshop cleanup",
        "status": "confirmed",
        "start": {"dateTime": "2025-03-09T03:00:00-04:00"},
        "end": {"dateTime": "2025-03-09T04:30:00-04:00"},
    },
    {
        "id": "synthetic-office",
        "summary": "Office coverage",
        "status": "confirmed",
        "start": {"dateTime": "2025-03-09T07:30:00Z"},
        "end": {"dateTime": "2025-03-09T08:30:00Z"},
    },
    {
        "id": "synthetic-cancelled",
        "summary": "Workshop cancelled instance",
        "status": "cancelled",
        "start": {"dateTime": "2025-03-09T06:30:00Z"},
        "end": {"dateTime": "2025-03-09T08:00:00Z"},
    },
    {
        "id": "synthetic-all-day",
        "summary": "Workshop holiday",
        "status": "confirmed",
        "start": {"date": "2025-03-09"},
        "end": {"date": "2025-03-10"},
    },
]

CONFIG = CalendarConfig(
    calendar_ids=("synthetic-primary",),
    timezone="America/New_York",
    poll_seconds=60,
    rules=(
        Rule("workshop-rule", "Workshop", 101),
        Rule("office-rule", "Office", 202, start_offset_minutes=-30),
        Rule("board-rule", "Board", 303),
    ),
)


def synthetic_page(calendar_id, params):
    """Stand in for Google ``events.list``; no network request is made."""

    assert calendar_id == "synthetic-primary"
    assert params["singleEvents"] is True
    return {"items": SYNTHETIC_EVENTS}


def main():
    print("Synthetic events:")
    for event in SYNTHETIC_EVENTS:
        interval = event["start"].get("dateTime", event["start"].get("date"))
        print(f"  {event['summary']} | {event['status']} | starts {interval}")
    print()
    result = preview_google_responses(
        CONFIG, synthetic_page, now=EVALUATION_TIME
    )
    print(format_preview(result))


if __name__ == "__main__":
    main()
