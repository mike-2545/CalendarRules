# CalendarRules native eisy-ui plugin

This directory contains the standalone v0.4.10 eisy-ui plugin implementation. The store archive builder places the tracked contents of `plugin/` at the ZIP root and adds `LICENSE` and its package manifest.

CalendarRules maps selected Google Calendar events by **title or location** into 0/1 values for **existing IoX type-2 state variables**, with signed timing offsets, recurring-event evaluation and overlap reconciliation. The owner decides how their own ISY programs use those variables; this is not an HVAC or access-control application.

The native plugin requests only Google Calendar read-only scopes, receives OAuth data using the protected platform SDK, and requires the owner-authorized IoX API grant and a saved enabled rule to reconcile configured targets. Every write is conditioned on trusted schedule/cache, valid type-2 target checks, INFO-or-higher logging and verified minute-poll cadence. Writes are read back when issued.

An untrusted or incomplete calendar is **not** an empty calendar. A stopped/expired plugin can leave an existing state variable at its last value, including `1`. Build independent safe-off/watchdog behavior for any actual equipment.

## Installation and configuration

See [Getting started](../docs/getting-started.md), [configuration details](../docs/eisy-ui-configuration.md), [ISY examples](../docs/isy-program-examples.md), and [security](../SECURITY.md).

The runtime depends on pinned `udi_interface==3.4.9`. Keep SDK credentials and OAuth tokens out of logs, examples and GitHub. Never publish DEBUG output containing credentials.

The home supervised 0.4.9 deployment has different activation and certificate-trust handling and is not documented as an install path here. This repository does not include those home-only deployment scripts or private history.

## eisy-ui routing and connection lifecycle

The pinned SDK preserves the eisy-ui MQTT identity `plugin_<slot>`; older SDK versions may instead use a **legacy PG3x routing identity**. The controller uses `conn_status="ST"` for connection state, with **explicit** readiness reporting: it publishes connected status only after initialization succeeds, and publishes disconnected status during normal shutdown. A Connected indicator alone does not prove Google calendar access or a trustworthy schedule.

## Planned smoke test and Rollback

A **Planned smoke test** for future versions includes fresh install, Google OAuth, isolated existing type-2 test variable, successful trusted refresh, write/readback, scheduled ON and OFF, and restart/reinstall with configuration persistence. Run the test on an isolated eisy, not on production equipment.

**Rollback** must retain the last known-good plugin archive and verify that store updates preserve protected configuration and cached schedule. A failed update/restore procedure has not yet been verified and should not be assumed automatic. The store plugin home typically follows `/var/eisyui/ns/plugin_<slot>`; the archive is **self-contained** and must not depend on the development checkout.
