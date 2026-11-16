# Changelog

## Pending — eisy-ui backup permission compatibility (not released)

- UD clarified that backup-eligible plugin files must have group read/write permission (recommended 0770) and that newer eisy-ui backups may skip unreadable files while reporting omissions.
- CalendarRules currently creates a 0700 directory and 0600 JSON cache files; group ownership, inherited ACLs and atomic replacement need a reviewed migration.
- An earlier Barn backup completed after a platform update, but archive inclusion and skipped-file status are not independently established. Track the fix and tests in [Issue #17](https://github.com/mike-2545/CalendarRules/issues/17).
- UD confirmed that enabling Allow API Access requires restarting the plugin; Reinstall installs the latest available release, while newer eisy-ui also offers an Update indicator/button.

**Status:** Documentation and investigation only; no runtime permissions or Store package changed.

## 0.4.14 — Guarded startup reconciliation (native Beta, 2026-11-15)

- Permit one fully guarded startup reconciliation after the first genuine short-poll callback, instead of waiting for three callbacks before any state-variable update.
- Retain verified, fresh polling for ongoing reconciliation and fail closed if callbacks are missing, late, or duplicated.
- Recheck safe logging, valid trusted schedule, owner IoX authorization, exact configured target IDs, validated type-2 variables, and write/readback safety before operations.
- Improve startup, scheduler and Allow API Access notices so users know what is happening and what actions help.
- Add regressions for no-poll, failed-poll, missed/duplicate poll, restart, and ON/OFF recovery scenarios.

**Live acceptance (Barn eisy, November 15, 2026):** Owner installed native Beta `0.4.14` in the existing Barn CalendarRules installation (slot 2). Screenshots confirm startup notices, Google Calendar node **Connected**, trusted calendar data, configured type-2 variables `32` and `33` readable, and later a successful Google refresh with five events evaluated, two desired ON, the IoX write gate open, and **IoX state write and readback verified**. The startup notice changed to explain that initial reconciliation is available after the first scheduler callback. The later write/readback confirms operational reconciliation, **but the screenshots alone do not prove that a write happened on the first poll** rather than after full cadence verification. No post-0.4.14 backup, complete ON→OFF event cycle, or missed-poll live recovery is established by these screenshots. PR #14 and the 0.4.14 release package PR #15 were merged after regression and security checks; the package build was reproducible. Issue #13 was closed with the merged implementation; first-poll timing and remaining live lifecycle checks are still useful acceptance follow-ups.

## 0.4.13 — Native Beta configuration help update (2026-11-15)

- Add practical help tooltips for every eisy-ui CalendarRules field, including Google Calendar ID, event matching, start/end offsets, rule enablement, existing type-2 state variable ID and privacy controls.
- Correct outdated wording about offline previews and clarify the difference between hourly Google refresh and minute-by-minute cached schedule evaluation.
- Add tests requiring a help description for each configuration field and validating key instructions.
- No changes to scheduling, Google OAuth, IoX communication, cache permissions, or state-variable writing.

**Status:** Published in the UD native Beta Plugin Store and installed on the Barn eisy as `0.4.13` (owner-confirmed November 15, 2026). The Configuration screen visibly shows `?` help icons beside previously undocumented nested rule fields including Friendly rule name, Event match pattern, Match type, Case-sensitive match, existing IoX type-2 variable ID, and Start/End offsets. The 0.4.13 package was built reproducibly (37 ZIP entries) and passed regression, secret-scan, and packaging workflows before release. **Acceptance scope:** visual help-icon presence and reported version are confirmed; individual tooltip text, OAuth persistence, operational write/readback after this update, and a post-0.4.13 system backup have not yet been independently verified. The full scheduling/write and backup acceptance previously completed on 0.4.12 remains recorded below.

## 0.4.12 — Native Beta Store maintenance update (2026-11-15)

- Explain invalid adjusted event intervals when a positive start offset would place the effective start at or after the effective end. A successful Google fetch is reported separately from an untrusted local schedule evaluation.
- Restore the IoX controller Connected (`ST`) driver during the existing one-time post-registration status snapshot to recover a potentially lost initial report.
- Preserve fail-closed schedule evaluation, the trusted cache, IoX target validation, and write/readback behavior.

**Status:** Published to the native eisy-ui Beta Store and installed via **Reinstall** over the existing Barn plugin (slot 2), preserving Google authorization, rule configuration and persistent cache. An HTTP 504 appeared during installation, but the plugin subsequently started as `0.4.12`, refreshed Google successfully, achieved a trusted cache, verified type-2 variable writes/readback, and showed the IoX node **Connected**. Tested invalid `+120` start offset: Google refresh succeeded, local evaluation correctly rejected the invalid interval, and no state decisions were produced; restoring `-120` recovered trusted scheduling and a verified desired-ON result. A new eisy-ui system backup succeeded afterward. Issues #4 and #5 closed following live acceptance. This is **Beta acceptance**, not proof of a full ON→OFF lifecycle, restore test, token longevity, or resolution of the HTTP 504 installer warning.

## 0.4.11 — Intermediate maintenance build (installed on Barn before 0.4.12)

- Point plugin documentation to the public CalendarRules website.
- Point the license link to the public MIT license.
- Align the plugin metadata and eisy-ui reported runtime version at `0.4.11`.
- Add release-contract checks and an isolated upgrade acceptance plan.

No changes to calendar matching, event offsets, polling, Google OAuth, cache, IoX target validation, write gating, reconciliation, installation dependencies, or the publisher identifier.

**Historical status:** This build was observed running on the Barn before the successful 0.4.12 Beta upgrade. The native **Reinstall** action was used for 0.4.12; no separate **Update Now** control was observed.

## 0.4.10 — Native Beta baseline

Installed from the eisy-ui Beta Plugin Store on a separate eisy/ISY Portal account. Calendar retrieval, OAuth authorization, rule matching, trusted scheduling, and a real IoX type-2 state-variable write/readback were demonstrated. The full scheduled event lifecycle, store upgrade/restore, and long-lived OAuth token behavior require further acceptance testing.
