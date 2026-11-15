# CalendarRules

Google Calendar rule automation for Universal Devices eisy / IoX.

> **Beta software.** CalendarRules v0.4.13 is in the native eisy-ui Beta Plugin Store and installed on the Barn test eisy; the newly added configuration help icons are visible. The full scheduling/write and backup acceptance was performed on v0.4.12. A native in-place Reinstall upgrade has been tested on a separate eisy; public OAuth verification, a full restore, and complete device lifecycle testing are not yet finished. Do not connect unvalidated beta rules to safety-critical equipment.

CalendarRules reads selected Google Calendars, matches event **titles or locations**, applies timing offsets, and computes 0/1 values for **existing IoX type-2 state variables**. The ISY owner's programs decide how those variables affect HVAC, lighting, access control, notifications, or other devices.

## Documentation and help

| Goal | Guide |
| --- | --- |
| Install and test safely | [Getting started](docs/getting-started.md) |
| Understand configuration and offsets | [Configuration reference](docs/configuration-reference.md) |
| Diagnose warnings and installation/backup problems | [Troubleshooting](docs/troubleshooting.md) |
| Build ISY programs from schedule signals | [ISY program examples](docs/isy-program-examples.md) |
| Privacy and security | [Security](SECURITY.md) · [Privacy](https://calendarrules.org/privacy/) |
| Report an issue | [GitHub Issues](https://github.com/mike-2545/CalendarRules/issues) |

**Website:** https://calendarrules.org

## Safety principles

- Google OAuth uses read-only Calendar scopes.
- CalendarRules never creates a state variable. Owners supply existing type-2 IDs.
- A stale or untrusted schedule must not be mistaken for an empty calendar.
- If the plugin stops, loses authorization, or its license expires, the last IoX variable value may remain unchanged, including **1**. Build independent fail-safe programs for equipment.
- Use isolated test variables before connecting the plugin to real automations.

## Current beta acceptance

On November 15, 2026, Beta 0.4.12 was installed on the Barn eisy via the existing plugin's **Reinstall** control, retaining authorization, saved rules, trusted schedule cache and IoX variable targets 32/33. The IoX node reported **Connected**, actual polling opened the write gate, and type-2 write/readback was verified.

An intentionally invalid start offset (`+120` against a shorter event) produced a specific local-interval warning while accurately reporting that Google refresh succeeded. No state decisions were made until the offset was restored to `-120`, after which trusted evaluation and verified reconciliation resumed. The owner confirmed an eisy-ui system backup succeeded after the update. See [changelog](CHANGELOG.md).

**Still pending:** a complete ON→OFF lifecycle acceptance, full restore and failed-update recovery, long-lived OAuth token behavior and public Google OAuth review. The installer displayed an HTTP 504 timeout warning even though the upgrade installed successfully; the warning remains unresolved.

## License

MIT. Distribution terms in the eisy-ui store are separate from the source license.
