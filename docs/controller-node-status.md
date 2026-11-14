# Native controller-node status

CalendarRules reports non-sensitive aggregate health through the supported
IoX node-driver profile. The existing `ST` connection driver and `REFRESH`
command are unchanged. Five read-only status drivers are added:

| Driver | Display | Values |
| --- | --- | --- |
| `GV0` | Refresh Status | Idle, In Progress, Succeeded, Failed / Using Cache, Failed |
| `GV1` | Calendar Cache | Unavailable / Untrusted, Trusted |
| `GV2` | Events Evaluated | Integer 0–10000 |
| `GV3` | Targets Desired ON | Integer 0–10000 |
| `GV4` | IoX Communication | Authorized / Readable, Unauthorized, Failed, Pending |

The values contain no event titles, calendar IDs, OAuth data, IoX
credentials, or configuration content. Counts are accepted only from an
already trusted complete evaluation. An unavailable or untrusted evaluation
clears both counts and reports the cache unavailable; it does not manufacture
an OFF decision or change the existing write gates.

`Refresh Now` reports In Progress when its serialized worker request is
accepted. Succeeded is reported only after that worker returns a confirmed
successful complete Google refresh. A failed refresh that still produced a
trusted cached evaluation reports Failed / Using Cache and retains the latest
complete snapshot. A failure without a trusted evaluation reports Failed and
Unavailable / Untrusted.

## Supported publication sequence

The static IoX profile remains the authority for native node presentation:

- `profile/nodedef/nodedefs.xml` declares the status drivers and command;
- `profile/editor/editors.xml` defines UOM 25 enumerations and bounded UOM 56
  integer counts;
- `profile/nls/en_us.txt` supplies the displayed labels;
- `profile/version.txt` is `1.2.0` so the change is distinguishable from the
  earlier Refresh-only profile.

At startup CalendarRules keeps the already-tested SDK order:

1. `polyglot.start(...)`
2. `polyglot.ready()`
3. `polyglot.updateProfile()`
4. controller `addNode(..., conn_status="ST")`
5. `polyglot.runForever()`

`updateProfile()` asks eisy-ui/Polyglot to send the current static profile to
IoX. A release verifier must independently confirm that the installed family
10, slot instance contains `REFRESH` and all five new status properties before
declaring profile publication successful. Existing installations preserve the
same controller address, nodedef ID, `ST` driver, command ID, typed
configuration, OAuth records, cache, and write activation rules.

## Native display limitations

IoX node properties are numeric. Enumerated editors give refresh, cache, and
IoX communication readable labels, while the two activity properties are
counts. The localized exact last-successful-sync time and detailed sanitized
failure explanation remain eisy-ui notices. The native command protocol does
not provide a persistent custom result panel for a button press, so the
reported `GV0` property is the durable Refresh Now feedback. Calendar event
titles and upcoming-event lists are intentionally outside this change.

