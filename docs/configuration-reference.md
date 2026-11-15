# CalendarRules configuration reference

This document describes the **native eisy-ui typed Configuration form**, not the older JSON examples or developer-only supervised installation. Select **Save Changes** to apply edits; **Configuration Refresh** reloads the form rather than fetching Google events.

## General fields

| Field | Accepted values | What it does |
| --- | --- | --- |
| Timezone (IANA name) | e.g., `America/New_York` | Used for displaying local activity times; enter an IANA timezone, not just `EST`. All-day events are ignored |
| Google refresh interval in hours | `1`, `2`, `4` | Interval for retrieving Google data; local schedule evaluation remains approximately once per minute |
| Show event titles in Calendar Activity | `true` or `false`; default `false` | Optional display of titles in authenticated eisy-ui notices; locations are not shown |
| Upcoming events shown | `5` or `10` | Display limit only, not the number of events evaluated |
| Google Calendars | One or more Calendar ID rows | Calendars evaluated by **all** enabled rules |
| Calendar Rules | One or more rows, at least one enabled | Rules that determine desired values for existing IoX type-2 state variables |

Values such as `true`, `false`, offsets, IDs, and interval numbers are entered as **text** in the current eisy-ui form because of platform compatibility. Invalid values are rejected rather than guessed.

## Calendar Rules fields

| Field | Options and meaning |
| --- | --- |
| Friendly rule name | Human-friendly label for the rule, shown in activity notices |
| Match field | `title` matches the Google event title; `location` matches the event's actual Location field |
| Event match pattern | Non-empty string to find in the selected field |
| Match type | `contains`, `exact`, `starts_with`, `ends_with` |
| Case-sensitive match | `false` by default or `true` |
| Existing IoX type-2 state-variable ID | Positive integer ID of a variable already created in IoX; not its display name |
| Start offset in minutes | Signed integer from `-1440` to `1440` |
| End offset in minutes | Signed integer from `-1440` to `1440` |
| Rule enabled | `true` to include this rule; `false` to exclude it |

### Offset examples

For a calendar event from 10:00 AM to noon:

| Offsets | Effective interval |
| --- | --- |
| Start `0`, End `0` | 10:00 AM–noon |
| Start `-30`, End `0` | 9:30 AM–noon |
| Start `0`, End `30` | 10:00 AM–12:30 PM |
| Start `-120`, End `15` | 8:00 AM–12:15 PM |

These are **adjusted schedule windows**, not commands to a device. A negative start offset starts demand early. A positive end offset extends demand later. Offsets can move effective times into another day. **An adjusted start that is at or after the adjusted end is invalid.** CalendarRules withholds state decisions in that case and distinguishes an invalid local schedule from a successful Google fetch (v0.4.12+).

### Title and location matching

The following rules are illustrative:

- **Title rule:** `title`, `contains`, `Workshop`, case-sensitive `false`: matches a title such as *Beginner Workshop*.
- **Location rule:** `location`, `exact`, `Studio A`: matches a Google event whose **Location field** is exactly `Studio A` (subject to case-sensitivity setting). Merely writing *Studio A* in an event description does not count.
- **Starts-with rule:** `title`, `starts_with`, `Staff:`: useful when events use consistent title prefixes.

Every enabled rule is currently evaluated against **every configured calendar**. Per-rule calendar selection is not supported. If several matching events demand the same variable ON at once, CalendarRules retains the desired ON state until the final effective interval ends.

### Special cases

- **Cancelled** events and **all-day** events are ignored.
- Recurring events are evaluated by their occurrences, accounting for changes and cancellations.
- An edited calendar/rule configuration invalidates an incompatible cached schedule; the plugin will not invent an OFF state while rebuilding trust.
- An invalid or duplicate configuration prevents decisions until fixed. A valid empty event list is different from an unsuccessful calendar retrieval.

## Understanding the various permission gates

| Indicator or control | What it proves |
| --- | --- |
| **Connected** | Plugin ↔ Polyglot communication only |
| **Google Authenticate succeeded** | Authorization flow and token handling occurred; schedule retrieval still needs checking |
| **Google refresh succeeded** | Google event data was retrieved for the configured schedule |
| **Trusted schedule** | A complete, compatible schedule is available for local evaluation |
| **IoX target readable** | Existing configured type-2 variable(s) were validated through authorized IoX access |
| **Write gate open** | Enabled rule and all required runtime checks permit reconciliation |
| **Write and readback verified** | A change was sent and confirmed through IoX |
| **State verified without a change** | IoX already held the required state; no redundant write was needed |

The **Allow API Access** switch belongs to eisy-ui and controls the platform's grant. It is distinct from enabling a CalendarRules rule. A grant alone does not guarantee a write. Stable minute polling must also be observed. If eisy-ui's **Allow API Access** toggle changes during an active session, a plugin restart was needed in our observed test; whether the platform should push a new authorization event automatically remains a UD support question.

## Refresh and status

- **Configuration → Refresh:** reloads saved form values; it does not fetch Google events.
- **Google Calendar IoX node → Refresh Now:** requests a fresh Google schedule retrieval through the supported worker.
- **Overview / Notices:** show last successful Google sync, schedule trust, active/upcoming matches, configured variable IDs, and sanitized write-gate status.
- **Show event titles:** opt-in only; event locations, IDs, and credentials must not be included in notices.

## Privacy and safe operation

The plugin caches event data locally in its private `persist/calendarrules` directory. The cache can contain event titles and locations. A valid cached schedule can support evaluation through some temporary Google outages, within its actual coverage and maximum offline eligibility, but changes made during an outage cannot be known until refresh succeeds.

**Critical:** If CalendarRules stops, its license expires, or authorization is removed, a state variable previously set to `1` may stay `1`. Create independent IoX fail-safe behavior before connecting it to equipment.

See [Getting started](getting-started.md), [Troubleshooting](troubleshooting.md), and [ISY program examples](isy-program-examples.md).
