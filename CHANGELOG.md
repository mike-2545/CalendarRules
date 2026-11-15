# Changelog

## 0.4.12 — Maintenance update candidate (not released)

- Explain invalid adjusted event intervals when a positive start offset would place the effective start at or after the effective end. A successful Google fetch is reported separately from an untrusted local schedule evaluation.
- Restore the IoX controller Connected (`ST`) driver during the existing one-time post-registration status snapshot to recover a potentially lost initial report.
- Preserve fail-closed schedule evaluation, the trusted cache, IoX target validation, and write/readback behavior.

**Status:** Candidate for a versioned eisy-ui Beta Store update. Requires a verified archive, controlled Update Now acceptance on the backed-up Barn eisy, and confirmation of the ST behavior. Existing v0.4.11 packaging is not reused.

## 0.4.11 — Maintenance candidate (not released)

- Point plugin documentation to the public CalendarRules website.
- Point the license link to the public MIT license.
- Align the plugin metadata and eisy-ui reported runtime version at `0.4.11`.
- Add release-contract checks and an isolated upgrade acceptance plan.

No changes to calendar matching, event offsets, polling, Google OAuth, cache, IoX target validation, write gating, reconciliation, installation dependencies, or the publisher identifier.

**Status:** A pull-request candidate only. Not published to the eisy-ui store, not deployed to any eisy, and not yet verified using **Update Now**.

## 0.4.10 — Native Beta baseline

Installed from the eisy-ui Beta Plugin Store on a separate eisy/ISY Portal account. Calendar retrieval, OAuth authorization, rule matching, trusted scheduling, and a real IoX type-2 state-variable write/readback were demonstrated. The full scheduled event lifecycle, store upgrade/restore, and long-lived OAuth token behavior require further acceptance testing.
