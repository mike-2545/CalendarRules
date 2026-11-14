# Security and privacy

CalendarRules reads selected Google Calendars using read-only OAuth scopes and may write only configured existing IoX type-2 state variables after all write prerequisites are satisfied. No calendar event is itself an equipment command; owners decide how their ISY programs interpret a state variable.

## Private information

Do not commit OAuth client secrets, access or refresh tokens, Google Calendar IDs from private calendars, IoX passwords, user-specific configuration, cache files, logs, certificates or production network details. Calendar event titles and locations can be sensitive; title display is optional and location text should not appear in notices. Snapshot cache checksums protect integrity but do not encrypt event data. Keep the plugin's file permissions restricted.

## Write safety

Malformed or incomplete calendar responses are **unknown**, not an empty schedule. A failed Google refresh must not synthesize a forced OFF state. IoX writing requires explicit enabled rules, an owner-authorized API grant, checked readable binary type-2 targets, trusted compatible cache, safe logging and verified poll cadence. A disabled or removed rule does not force its previous variable OFF.

**Important:** Stopping the plugin, losing Google access, losing IoX authorization, or expiring a license may leave a previously ON variable at `1`. For equipment, implement appropriate independent ISY failsafes and watchdogs. Do not use this beta as a life-safety controller.

The native local IoX transport is constrained to the SDK-authorized `127.0.0.1:443` endpoint. Because Universal Devices does not expose a supported accessor for the local self-signed certificate, the native transport uses encrypted loopback HTTPS without independently verifying the listener's certificate identity. This does not defend against a malicious local process impersonating that listener. Do not loosen the loopback address and peer restrictions or use arbitrary HTTPS certificate-validation bypasses.

## Reporting

Before public publication, report possible vulnerabilities privately to the repository owner rather than posting credentials, proof-of-concept secrets, or personal calendar contents in a public issue.
