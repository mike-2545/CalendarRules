# CalendarRules configuration

CalendarRules publishes a supported eisy-ui typed configuration form with:

- an IANA timezone;
- Google refresh interval of 1, 2, or 4 hours (default 2);
- upcoming activity notice limit of 5 or 10 (default 5);
- add/edit/delete Google Calendar ID rows;
- add/edit/delete rule rows with friendly name, event pattern, match field
  (`title` or `location`), match type (`contains`, `exact`, `starts_with`, or
  `ends_with`), case-sensitive flag,
  existing state-variable ID, signed start/end offsets, and enabled flag.

Every enabled rule currently applies to all selected calendars. Per-rule
calendar scoping is planned for a later milestone and is not implied by this
form.

Google calendar IDs remain text fields. Each enabled rule's state-variable ID
must be a positive integer. The documented typed configuration API does not
provide a dynamic IoX variable dropdown, so the separately activated write
mode performs bounded, authenticated read-only validation of every configured
target before any write is possible. Disabled rules do not add targets.

The same interface has no documented enum control, so **Match field** and
**Match type** are compatibility STRING fields. Missing match fields preserve
the legacy `title` behavior, while missing match types preserve `contains`;
unknown values fail validation. Explicit `title`/`contains` defaults retain the
legacy schedule identity. Selecting `location` or a non-default operator gets a
different identity without changing the calendar, offsets, or variable ID.

Invalid or partial configuration is never activated. Calendar or rule changes
invalidate incompatible cached data, and CalendarRules withholds all preview
states until a complete trusted snapshot for the new configuration exists.
Complete version-3 snapshots contain event titles and the provider's optional
location value, 32 days of forward coverage, and remain eligible for
offline local evaluation for at most 30 days, subject to their actual coverage
end. They are saved atomically and restored only after checksum, schema,
configuration identity, timestamp and event validation.
Legacy version-2 snapshots remain usable during an outage for title-only rules,
but are rejected for any location rule because they cannot prove complete
location data.
The controller's Connected status reports only Polyglot health; Google auth,
configuration validity, and cache trust remain separate.

The controller node's supported `Refresh Now` command requests a complete
Google schedule refresh without restarting the plugin. It uses the same
bounded serialized worker and does not change the selected 1, 2, or 4 hour
automatic interval. Refresh status and the last successful synchronization
time are displayed through sanitized eisy-ui notices.

Compact `Calendar Activity`, `Active Now`, and stable `Upcoming Event` notices
are computed from the same trusted snapshot. Upcoming rows represent
event/rule matches and are ordered by effective automation start. Titles remain
hidden unless the owner explicitly sets `show_event_titles=true`; no provider
IDs, event locations, or credentials are displayed. The title opt-in never
authorizes location disclosure. Cached outage data is labeled, and
untrusted, expired, incompatible, or out-of-coverage schedules clear event
details and show activity as unavailable.

The form itself cannot activate IoX writes. CalendarRules requests the
platform's owner-controlled IoX authorization (`requiresIoXAccess: true`) and
uses it only through the bounded configured-target transport. Authorization alone
does not enable writes: the separate owner-only activation marker, trusted
schedule, verified minute poll, INFO-or-higher logging, complete type-2 target
validation, configured-target boundary, compare-before-write, and readback checks must all
pass. CalendarRules never creates the activation marker itself.
