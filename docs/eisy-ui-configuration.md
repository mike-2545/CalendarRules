# Offline eisy-ui configuration design

Issue #11 follows the supported configuration pattern used by the Universal
Devices Notification plugin: the node server publishes
`customtypedparams`, receives the complete user value through
`CUSTOMTYPEDDATA`, and stores it as `customtypeddata`. An object definition
with `isList: true` gives the eisy-ui Configuration page its native controls
for adding, editing, and deleting rows.

The documented `udi_interface` typed controls are `STRING`, `NUMBER`, and
`BOOLEAN`, plus lists and nested objects. The current eisy-ui form validator
calls JavaScript `trim()` on NUMBER values and checks `BOOL` rather than the
documented `BOOLEAN` name. CalendarRules therefore publishes editable numeric
and Boolean values as compatibility STRING fields. It parses them explicitly
after submission, while still accepting previously stored native integer and
Boolean values. The interface does not document an enum or a dynamic selector
populated with Google calendars or IoX variables.

## Supported fields

- **Timezone**: IANA timezone string; defaults to `America/New_York`.
- **Google refresh interval**: compatibility text field parsed as an integer
  and validated as exactly 1, 2, or 4 hours; defaults to `2`. Local evaluation
  remains one minute.
- **Google Calendars**: add/edit/delete list with one required Calendar ID per
  row. Every enabled rule currently applies to every configured calendar.
- **Calendar Rules**: add/edit/delete list containing friendly name, event
  pattern, match field (`title` or `location`), match type (`contains`,
  `exact`, `starts_with`, or `ends_with`), case-sensitive `true`/`false` value,
  existing type-2 IoX
  state-variable numeric ID, signed start/end offsets, and enabled
  `true`/`false` value. The platform has no documented enum/dropdown field, so
  match field and match type use compatibility STRINGs with `title` and
  `contains` as their defaults and strict validation. Numeric and Boolean
  values likewise use compatibility
  text fields. Offsets are limited to -1440 through 1440 minutes. Cancelled and
  all-day events remain ignored.
- **Show event titles in Calendar Activity**: compatibility `true`/`false`
  STRING, default `false`. Enabling it opts into displaying bounded event
  titles in the authenticated eisy-ui notice. Event IDs, calendar IDs, OAuth
  material, event locations, raw provider data, and claimed actual IoX values
  are never shown. This opt-in applies only to titles and never authorizes a
  location value to appear in notices or diagnostics.
- **Upcoming events shown**: compatibility STRING validated as exactly `5` or
  `10`; defaults to `5` for older saved configurations. This controls only the
  number of compact notice rows. It is excluded from schedule identity and
  does not invalidate the trusted cache or force a Google refresh.

The parser consumes one complete typed-data payload atomically. Empty,
partial, duplicate, malformed, or out-of-range input is never activated. An
invalid update suspends the cached schedule and withholds every desired state.
A valid configuration change clears any incompatible in-memory snapshot and
cannot restore a persisted snapshot with a different configuration
fingerprint. A complete new snapshot is required before preview decisions are
trusted again.

## Health boundaries

The controller's `ST` driver reports only the Polyglot connection. Separate
internal status and non-sensitive notices report:

- Google authentication: disabled in this milestone;
- configuration: not configured, invalid, or valid;
- cache: unavailable until a future approved schedule integration supplies a
  complete trusted snapshot.

None of these states changes `ST`. This implementation starts no polling
thread and performs no Google request or IoX read/write.

## Known platform limitations

- Until the future OAuth/discovery work is approved, a user must obtain and
  enter a Google Calendar ID in the typed form. No token or secret belongs in
  this field.
- Without IoX access, CalendarRules can validate that a variable ID is a
  positive integer but cannot prove it exists or is type 2. Tests can inject a
  read-only catalog and enforce membership. A production selector or catalog
  requires a separately reviewed, restricted UD-supported interface.
- Compatibility text fields cannot render 1/2/4 or true/false as dropdowns,
  and cannot render the match field or four match operators as native
  dropdowns, so invalid values are rejected with a notice.
- eisy-ui 1.4.9 has no supported arbitrary-text IoX driver or custom node
  widget. Active/upcoming details therefore use the existing authenticated
  Notices interface. Upcoming rows represent event/rule matches so every
  target attribution remains explicit; a bounded summary reports omitted
  matches. ST, GV0–GV4, REFRESH, and profile 1.2.0 are unchanged.

The supervised deployment verifier for 0.4.5 expects the former single
long-form `calendar_activity` notice. A later 0.4.6 release-preparation change
must verify the compact summary plus stable active/upcoming keys without using
private titles or requiring a nonzero event count. This feature PR deliberately
does not weaken the deployed verifier.
- Rules currently apply to the complete selected calendar set; per-rule
  calendar scoping would require an event/model change and is not represented
  as if the platform already supports it.
