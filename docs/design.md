# Design notes (proposed)

## Separation of concerns

Google Calendar is the schedule source. This plugin calculates desired 0/1 state for configured existing IoX state variables. IoX programs own the equipment behavior.

## Proposed rule fields

- `calendar_id`: Google Calendar identifier, not a mutable display name.
- `title_contains`: preserved event-pattern field name for saved-configuration
  compatibility.
- `match_field`: `title` (legacy default) or the Google Calendar event's
  optional `location`; missing or empty locations do not match and never fall
  back to the title.
- `title_match_type`: `contains` (legacy default), `exact`, `starts_with`, or
  `ends_with`; case sensitivity remains an independent rule option and
  case-insensitive comparisons use Unicode casefolding.
- `state_variable_id`: existing IoX state variable ID, validated before use.
- `start_offset_minutes` / `end_offset_minutes`: signed offsets from actual event times.
- `ignore_all_day`, `ignore_cancelled`: defaults `true`.

The user may create multiple rules targeting the same state variable. Desired state is **OR** of their currently active matching intervals.

## Desired-state algorithm

1. Fetch expanded recurring event instances within a bounded schedule horizon.
2. Parse timestamps with offsets and normalize to timezone-aware instants.
3. Discard cancelled events and (by default) all-day events.
4. Select the configured title/location field, apply the shared match operator,
   then apply offsets to each interval.
5. `desired(variable, now) = 1` if **any** matching adjusted interval includes `now`; otherwise `0`.
6. Compare with the actual IoX state and write only when different.
7. Log what changed and why, without leaking private event details or credentials.

## Failure handling

On Google API timeout, expired credentials, invalid configuration, or partial calendar response, do not treat the missing schedule as empty. Keep the last trusted desired state where appropriate, emit a warning/status, and retry. The exact fail-safe policy must be documented and configurable before release.

Snapshot schema v3 preserves validated location data. A legacy v2 snapshot can
remain trusted during an outage only for title-only rules; it cannot supply
complete evidence for a location rule and therefore produces no state decision.
Locations are private input data and never enter activity notices or diagnostics.

## Key test scenarios

- Two overlapping events mapped to one variable.
- Cancelled, deleted, shortened, moved, and recurring events.
- Restart midway through an event; restart just after its end.
- DST spring-forward and fall-back in the calendar's timezone.
- Transient API outage followed by restored calendar access.
- Multiple events with different early/late offsets.
- Rules mapping into existing state variables only; no unrequested device writes.
- Variable manually changed while an event remains active: reconciliation restores desired state.

## Compatibility checkpoint

Before implementing the IoX writer, verify the current supported eisy-ui plugin APIs for accessing existing **state variables** on onboard IoX. The historical local-REST approach in older Google PI scripts is a reference only and must not be copied with embedded credentials.
