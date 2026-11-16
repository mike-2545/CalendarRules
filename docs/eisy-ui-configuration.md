# eisy-ui typed configuration — technical notes

**For everyday configuration, use [Getting started](getting-started.md) and [Configuration reference](configuration-reference.md).** This document records platform integration details rather than user instructions.

CalendarRules publishes its typed configuration through eisy-ui's Polyglot interface (`customtypedparams`), receives complete `CUSTOMTYPEDDATA` submissions, and stores validated data through the supported custom-data mechanism.

The current eisy-ui form requires compatibility `STRING` fields for some numeric and Boolean values; CalendarRules validates typed values strictly after submission rather than assuming they are valid. Users enter text such as `true`, `false`, `1`, `2`, and `4`.

## Actual native behavior

- A Google Calendar ID must be entered for each selected calendar. This is **not** a Google OAuth Client ID or an API secret.
- Every enabled rule applies to all selected calendars; per-rule calendar selection is not supported.
- Matching can use `title` or `location`, with `contains`, `exact`, `starts_with`, and `ends_with`.
- Offsets are limited to -1440 through +1440 minutes. Cancelled and all-day events are ignored.
- Invalid, partial, duplicate, or out-of-range configuration is withheld; incompatible cached schedules are not trusted after a change.
- `Upcoming events shown` is limited to 5 or 10 and does **not** change the number of events evaluated.
- `Show event titles` is an explicit authenticated-notice privacy opt-in; locations are not intended for display in notices.

## Separate status and safety checks

A Connected controller `ST` indicates communication with Polyglot, not Google authorization or permission to change IoX variables. Google synchronization, trusted schedule/cache, configured target type-2 read validation, API access grant, and observed short polling are separately checked. Native store mode uses **enabled rules** as the owner's intended mapping; it does **not** require the supervised home-mode activation marker.

`Configuration → Refresh` reloads the form. A Google Calendar node's `Refresh Now` command requests a Google refresh through the bounded worker. Sanitized eisy-ui notices show schedule trust and activity. See [Troubleshooting](troubleshooting.md).

The native cache is in `persist/calendarrules`. Its intentionally strict privacy permissions previously exposed an **eisy-ui backup HTTP 500 / EACCES compatibility issue**. Backup completed on the Barn after updating the platform to eisy-ui 1.4.11, IoX 6.1.2_16, and UDX 5.3.0_4 without changing CalendarRules' private file permissions. UD subsequently clarified that newer backups **skip inaccessible files and report omissions**; completion does not prove that CalendarRules' cache was archived. UD recommends **group read/write access**, with **0770** as guidance. Cache directory ownership, group/ACL inheritance and file modes must be evaluated before migrating the current 0700/0600 policy. The pending implementation and acceptance plan is [Issue #17](https://github.com/mike-2545/CalendarRules/issues/17).

## Developer reference

See `plugin/eisy_google_calendar/ui_configuration.py` for the actual typed field schema, `plugin/eisy_google_calendar/validation.py` for rule validation, `plugin/controller.py` for notices/short polling, and `plugin/main.py` for native-store runtime initialization.

A complete list of user fields and examples is in the [configuration reference](configuration-reference.md).
