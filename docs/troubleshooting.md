# CalendarRules troubleshooting

This guide is for the **native eisy-ui plugin**. Start with the plugin's **Overview** and sanitized notices, then use its **Log Viewer** if needed. Do not share OAuth tokens, cookies, credentials, calendar IDs, private event titles/locations, or unsanitized HTTP request bodies.

## Diagnose in order

1. **Plugin health:** Is CalendarRules listed under **Local Plugins** and **Connected**? Connected confirms Polyglot communication, not a successful Google or IoX operation.
2. **Google authorization:** Has the correct Google account authenticated and does it have read access to the configured calendar?
3. **Configuration:** Are there calendar IDs and at least one enabled, valid rule?
4. **Schedule trust:** Does Overview show a successful Google refresh and a trusted, compatible schedule? An empty calendar after a successful fetch is different from a failed fetch.
5. **IoX authorization:** Is owner-granted **Allow API Access** enabled, and is the target an existing readable type-2 state variable?
6. **Write readiness:** Has CalendarRules observed the expected minute polling cadence and opened its write gate?
7. **Actual result:** Look for **write and readback verified** or **state verified without a change**, not merely an ON demand.

## Common messages and errors

| What you see | Likely meaning | Safe next step |
| --- | --- | --- |
| **Connected**, but nothing happens | Polyglot is connected; other prerequisites may not be met | Read Overview notices for authorization, configuration, schedule trust, and write gate |
| **Google authorization event received; credentials and Google access have not yet been verified** | An OAuth event was received, but successful API retrieval has not yet been established | Check subsequent sync notices; do not assume authentication alone proved access |
| **Google refresh failed** | Google data could not be retrieved | Confirm the Google account can read that calendar; check whether a trusted compatible cache is still active; do not force targets OFF |
| **Invalid adjusted event interval** | A rule's offsets leave adjusted start at or after adjusted end for a matched event | Check start/end offset signs and event duration. Google retrieval may still have succeeded; no state-variable decisions are made until the schedule is valid |
| **At least one enabled rule is required** | Calendar IDs alone are insufficient | Add a fully configured rule, enable it, save, and recheck notices |
| **Calendar configuration is invalid** | A field is missing, duplicated, malformed, or outside limits | Compare against the [configuration reference](configuration-reference.md); save a complete corrected configuration |
| **No trusted, compatible schedule** | Cached schedule is missing, stale, incompatible with changed rules, or outside coverage | Request a Google refresh through the IoX node's **Refresh Now** command and check its result |
| **IoX access is not authorized** | Owner has not granted the platform's IoX access, or it was revoked | Inspect **Allow API Access**; grant only after confirming the configured test variables are isolated |
| **IoX type-2 variable not readable / invalid** | Target ID/type/value or IoX access could not be verified | Verify the ID exists as a type-2 *state* variable, not a type-1 integer variable |
| **Minute polling awaiting observed cadence** | The plugin has not yet verified the requested short-poll timing | Keep settings stable and review subsequent polling notices; do not force writes |
| **Write gate closed** | One or more required safety conditions has not passed | Do not bypass gates; identify the missing condition in notices |
| **State verified without a change** | IoX already had the desired 0/1 value | Normal idempotent behavior, not a missed command |
| **Upcoming event missing** | Wrong Calendar ID, title/location field, rule match, event timing, cancellation, or all-day event | Check the event's actual Google **Location** field and the rule; verify Google sync time and timezone |
| **HTTP 504 during install** | A gateway request timed out, possibly before reporting completed installation | Check **Local Plugins** and installed status before trying anything else; avoid repeated Install/Reinstall clicks. An earlier upgrade showed 504 but installed correctly; the owner's latest 0.4.13 upgrade did not show 504 |
| **HTTP 500 when creating an eisy-ui backup** | Backup service could not read a plugin persist directory in an earlier platform installation | Confirm installed eisy-ui/IoX/UDX versions, preserve the error log, and seek UD guidance before upgrading. The Barn backup succeeded after a platform update without plugin permission changes; do not loosen private cache permissions |

### Historical backup-permissions issue — resolved on the updated Barn

On an isolated native installation, the eisy-ui backup operation has failed with:

```text
EACCES: permission denied, scandir
'/var/eisyui/ns/plugin_2/persist/calendarrules'
```

The plugin creates its local calendar schedule cache under `persist/calendarrules` using owner-only directory/file permissions (`0700` / `0600`). On November 15, 2026, the Barn eisy running eisy-ui **1.4.11**, IoX **6.1.2_16**, and UDX **5.3.0_4** successfully created a system backup that included the plugin's persistent data. Those plugin permissions were **not changed**. The upgrade and reboot changed multiple platform components, so the exact cause of the earlier failure is not established.

**Do not run `chmod -R 777`, make the directory world-readable, or remove saved data.** Confirm a successful backup on your own eisy before updating; if the failure recurs, retain a sanitized log and seek UD support. The observed failure did not establish a Google OAuth or IoX scheduling defect.

### Allow API Access changed while running

In our observed tests, restarting the plugin after granting **Allow API Access** made the IoX authorization available to CalendarRules. We have asked UD whether the platform should deliver a new ISY authorization event automatically or whether a running plugin must request the updated information. A restart is an observed workaround, **not** a confirmed platform requirement. Verify trusted schedule and target readback afterward.

### Plugin stopping while a variable is ON

CalendarRules intentionally avoids turning a target OFF when it cannot trust calendar data. If the plugin or eisy is unavailable, a variable that was already `1` may remain `1`.

Do not assume the plugin will automatically reset variables when:

- Google authorization is revoked;
- Google access fails and trusted coverage expires;
- the plugin is stopped/uninstalled or the Beta license expires;
- backup/update/recovery fails.

For equipment integrations, implement **independent safeguards** using normal ISY programming (for example, a separate watchdog or equipment-specific maximum runtime/interlock) rather than relying on an unverified cache or a plugin shutdown callback. [ISY examples](isy-program-examples.md) illustrate demand signals, not a complete fail-safe design.

### Before posting an issue

Include only sanitized details:

- CalendarRules version and whether installed from the Beta store;
- eisy-ui version, platform and approximate time of failure;
- relevant **notice text** and error code, with private information removed;
- whether Google refresh, schedule trust, and target readback succeeded;
- whether this is a fresh install, restart, upgrade, or restore;
- if installation timed out, the HTTP status and elapsed time, **not** headers or cookies.

[Open a CalendarRules issue](https://github.com/mike-2545/CalendarRules/issues). For an eisy-ui backup platform error, consider reporting the sanitized `/api/system/backup/create` failure to Universal Devices as well.

See [Getting started](getting-started.md), [Configuration reference](configuration-reference.md), and [Security](../SECURITY.md).
