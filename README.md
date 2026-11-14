# CalendarRules

Google Calendar rule automation for Universal Devices eisy / IoX.

> **Beta software.** CalendarRules v0.4.10 has been tested through the eisy-ui plugin store, but public OAuth verification, update/restore, and a complete device lifecycle test are not yet finished. Do not connect unvalidated beta rules to safety-critical equipment.

CalendarRules reads selected Google Calendars, matches event **titles or locations**, applies timing offsets, and computes 0/1 values for **existing IoX type-2 state variables**. The ISY owner's programs decide how those variables affect HVAC, lighting, access control, notifications, or other devices.

## Safety principles

- Google OAuth uses read-only Calendar scopes.
- CalendarRules never creates a state variable. Owners supply existing type-2 IDs.
- A stale or untrusted schedule must not be mistaken for an empty calendar.
- If the plugin stops, loses authorization, or its license expires, the last IoX variable value may remain unchanged, including **1**. Build independent fail-safe programs for equipment.
- Use isolated test variables before connecting the plugin to real automations.

## Current beta acceptance

On November 14, 2026, v0.4.10 was installed through the eisy-ui plugin store on a second eisy with a separate ISY Portal account. OAuth, calendar retrieval, rule evaluation, polling authorization, and a type-2 variable write/readback were observed. The Google account was the **same** in both environments. Different-Google-account authorization, full ON/OFF lifecycle, update/restore, and token longevity remain release tests.

## License

MIT. Distribution terms in the eisy-ui store are separate from the source license.
