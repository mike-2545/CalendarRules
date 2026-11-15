# Controlled native Beta upgrade acceptance: 0.4.10 → 0.4.11

> **Historical planning document (superseded November 15, 2026).** CalendarRules 0.4.12 was successfully upgraded on the Barn using **Reinstall** on the existing plugin Overview, not Update Now. The updated Barn retained OAuth/rules/cache, passed live scheduling and invalid-offset tests, and successfully completed a system backup. The installer returned HTTP 504 despite subsequently completing. This document preserves the earlier proposed test plan; see [CHANGELOG](../CHANGELOG.md) for actual acceptance and remaining gaps.


**Planning only. No installation or plugin-store changes authorized by this document.**

## Scope

v0.4.11 changes only the app version reported through `polyglot.start`, the metadata version, public documentation and license URLs, and release tests/documentation. Do not change the stable UD store plugin entry/NSID, Beta edition, publisher identifier, Google OAuth client, dependency pin, IoX grants, runtime cache, or application behavior.

## Before publishing an update

1. Merge an approved v0.4.11 PR only after full tests, package determinism, and source/secrets checks are green.
2. Retain the known-good v0.4.10 store ZIP and SHA-256. Generate and verify a deterministic v0.4.11 ZIP from an immutable reviewed commit. The store's executable/install hook/profile settings should remain unchanged.
3. On the isolated test eisy, preserve a full eisy-ui and IoX backup. Record the existing Beta edition/slot and node identity, installed version, saved calendar IDs, rule rows, enabled states, timezone and poll settings. Do **not** copy or expose Google OAuth tokens.
4. Verify target state variables are isolated from equipment and capture their current values. Record Google refresh and readback status. Prepare a harmless synthetic calendar event, and arrange test time windows that won't be disrupted.
5. In Firefox Developer Tools → Network, enable **Persist Logs** before Update Now; capture HTTP status, elapsed time and request path if another 504 occurs. Do not capture or share Authorization headers, cookies, tokens or private request bodies.

## Update Now acceptance

1. Under the **existing Beta listing**, publish the reviewed 0.4.11 archive as an upgrade, keeping the store plugin identity. Verify the installed eisy recognizes Update Available before clicking it.
2. Use **Update Now** once, and record any UI error and the installation logs. Avoid repeated clicks while the operation may still be running.
3. Verify the plugin reconnects as v0.4.11 with the same slot and IoX node identity; no duplicate install is created.
4. Verify all typed configuration persists after F5 and restart. Confirm the stored Google OAuth authorization remains functional without unnecessary reauthentication, while keeping secrets private.
5. Verify trusted calendar refresh/cache, enabled rule evaluation, target read, write/readback, no-op reconciliation, and scheduled ON/OFF transitions with an isolated variable.
6. Disable the test rule and verify disabling it cannot force its previous variable OFF. Separately verify an invalid/untrusted schedule withholds writes.
7. Inspect sanitized logs and resource usage. Document whether a previous HTTP 504 repeats, when the process actually started, and whether the installer command exited successfully.

## Recovery tests (only on disposable setup)

- Evaluate backup/restore and failed-update recovery separately, with a retained known-good ZIP and owner-approved rollback plan.
- Do not assume that expired licensing, plugin stopping, lost OAuth access or upgrade failure will reset an already-ON IoX state variable. Provide independent ISY fail-safe logic before any equipment use.
- If Update Now fails or state changes unexpectedly, stop; retain logs/backups, do not blindly reinstall over the configured slot.

**Release gate:** No production promotion until the tested native update path and safe recovery behavior are documented.
