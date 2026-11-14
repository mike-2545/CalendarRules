# Release validation and known limitations

CalendarRules v0.4.10 is a Beta release for Universal Devices eisy / IoX.

## Source and regression verification

- The native plugin's executable/configuration/profile files are byte-identical to the reviewed v0.4.10 Beta source; user documentation was rewritten for standalone distribution.
- The full portable regression suite passed: **799 tests**, with no exclusions.
- TLS transport tests generate temporary synthetic certificates and private keys at test time; no TLS fixture keys are distributed.
- The tracked-source privacy heuristic check passed.
- A separate `detect-secrets` scan passed: 18 reviewed synthetic test literals or approved SDK source hashes, and zero unreviewed findings. Entries are bound to exact line hashes; changed lines require re-review.
- The deterministic store-package builder and its validation tests are included.

## Remaining acceptance work

- Validate OAuth with an unrelated Google account and resolve consent-screen production verification and refresh-token longevity.
- Verify complete scheduled ON/OFF cycles, revoked authorization behavior and cache recovery.
- Test native eisy-ui Update Now, reinstall, backup/restore and rollback on an isolated controller.
- Confirm store license-expiration behavior and document the production edition policy.
- Do not connect beta rules to safety-critical equipment. When scheduling cannot be trusted, the write gate fails closed; an existing IoX state variable can remain at its last value.

## Security scope

Automated scans and regression tests do not replace independent human review of code, dependencies and distribution configuration. For security guidance see [SECURITY.md](../SECURITY.md).

The plugin maps calendar titles/locations to **existing IoX type-2 state variables**; ISY users determine their own equipment automation.
