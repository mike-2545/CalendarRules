# Getting started with CalendarRules

CalendarRules connects **Google Calendar events to existing IoX type-2 state variables**. It does not create state variables, control equipment, or edit Google Calendar events. Your own ISY programs decide what a variable means.

> **Beta v0.4.13.** The Barn test eisy has been upgraded using the existing plugin's **Reinstall** action and the configuration help icons have been verified. Test with a dedicated, unused state variable; do not begin with life-safety or production equipment. A Barn backup completed on eisy-ui 1.4.11, but UD subsequently clarified that newer releases may **skip unreadable plugin files**; complete cache inclusion is not yet independently verified. Full restore, complete ON/OFF lifecycle and long-term OAuth token tests are still pending.

## Before you begin

You need:

- A Universal Devices **eisy** running the supported integrated **eisy-ui** plugin system, with an available plugin slot and access to the eisy-ui Plugin Store.
- An accessible Google Calendar and a Google account authorized to view its events.
- An **existing IoX type-2 state variable** with a positive numeric ID that is **not used by production programs**.
- Permission to manage the eisy, its plugins, and (when you are ready to test writes) its IoX API access.

**Read the safety note first:** if Google access fails, the plugin stops, or its license expires, CalendarRules can leave a previously ON state variable at `1`. It intentionally does **not** assume missing schedule data means OFF. Design independent ISY-side fail-safe behavior before using variables for equipment.

## 1. Install from eisy-ui

1. Open the **eisy-ui** interface for the eisy you intend to use.
2. Find **CalendarRules** in the Plugin Store. The current Beta may require activation; visibility, pricing, and trial availability depend on its store listing.
3. Install the plugin in an available slot. On a first installation, no calendars or enabled rules should yet be configured.
4. Open **Local Plugins → CalendarRules** and check **Overview**.

**Connected** means the plugin is communicating with Polyglot. It does **not** prove Google authentication, schedule trust, IoX authorization, or successful writes.

If an installation dialog returns HTTP 504, **do not repeatedly install**. Check whether CalendarRules appears in Local Plugins and is running. Record the error and use [Troubleshooting](troubleshooting.md).

## 2. Authenticate Google Calendar

1. Open CalendarRules **Overview** and select **Authenticate**.
2. Choose the Google account that can read your intended calendar.
3. Review the consent screen and grant the read-only Calendar permissions.
4. Return to Overview and look for Google synchronization notices.

CalendarRules uses `calendar.calendarlist.readonly` and `calendar.events.readonly`; it cannot create, edit, or delete Google events. A successful authorization message does **not** by itself prove that a schedule has been retrieved; configuration and trusted synchronization are separate steps.

## 3. Locate the Google Calendar ID

1. In **Google Calendar on the web**, open Settings for the specific calendar you want to use.
2. Under **Integrate calendar**, copy the **Calendar ID**. You may also enter `primary` for the authenticated account's main Google calendar. Shared or secondary calendars may have longer IDs.
3. Do not confuse a **Calendar ID** with a Google OAuth Client ID, API key, or client secret.
4. In CalendarRules **Configuration**, select **Add Google Calendars**, enter the Calendar ID, and save.

The account authorized in Step 2 must have read access to that calendar. **Every enabled rule currently applies to every configured calendar**, not just one selected rule-calendar pair.

## 4. Create a harmless test rule

Before creating the rule, identify an **existing type-2 state-variable ID** on your IoX that is not attached to devices or production programs. CalendarRules will not create one.

Create a temporary Google Calendar event, for example:

- **Title:** `CALENDARRULES_TEST`
- **Time:** a short future window, after Google synchronization is expected
- **Location:** optional

In CalendarRules **Configuration → Add Calendar Rules**, enter:

| Field in eisy-ui | Test value |
| --- | --- |
| Friendly rule name | `First Test` |
| Match field | `title` |
| Event match pattern | `CALENDARRULES_TEST` |
| Match type | `contains` |
| Case-sensitive match | `false` |
| Existing IoX type-2 state-variable ID | **Your own unused test ID** |
| Start offset in minutes | `0` |
| End offset in minutes | `0` |
| Rule enabled | `true` |

Select **Save Changes**. Numeric and Boolean form values are text fields: use values such as `0`, `false`, and `true` exactly as shown. If a configuration is incomplete or invalid, CalendarRules will withhold schedule decisions.

## 5. Confirm the schedule before allowing IoX writes

In **Overview**, look for these independent conditions:

- Google Calendar refresh **succeeded** and shows a last successful sync time.
- Calendar configuration is **valid**, with at least one enabled rule.
- Calendar activity shows a **trusted schedule** and the expected active/upcoming test event.
- The configured existing type-2 variable is confirmed **readable**.

IoX writes require the separate owner-granted **Allow API Access** permission in eisy-ui, verified variable checks, trusted data, safe logging, and observed minute polling. At startup, CalendarRules requests minute polling but does not treat that request as proof that scheduling works. After the first real short-poll callback it may perform one fully guarded initial reconciliation; ongoing updates remain blocked until reliable cadence is observed. If no callback arrives or the polling request fails, no startup reconciliation occurs. **Refresh Now only retrieves Google Calendar data and does not advance scheduler verification. Avoid restarting while startup verification is in progress.**

Enabling API access does **not** necessarily open the write gate immediately. In our observed tests, the reliable procedure was: enable **Allow API Access**, save, restart CalendarRules once, and verify the authorization and readable type-2 target notices. UD has confirmed a plugin **restart is required** after enabling API access.

Once the write gate opens, observe the test variable change to `1` during the matching event and return to `0` after the final matching event ends. Check the **write/readback verified** or **state verified without a change** notice. Do not use this test as permission to connect production equipment automatically.

If the variable does not change, see [Troubleshooting](troubleshooting.md). If more than one matching event requires the same variable ON, it stays ON until all effective event windows have ended.

## 6. Decide what your ISY does with the variable

After your test is proven and you have independent fail-safe logic, you can create ISY programs that react to a state variable. CalendarRules itself never commands HVAC, lights, or doors.

See [ISY program examples](isy-program-examples.md) and the [field reference](configuration-reference.md).

## 7. Verify persistence and backups

- Save your configuration, refresh the browser, and confirm it persists. Recheck after a deliberate plugin restart only when safe.
- Before upgrading, create an eisy-ui backup **and inspect any skipped-file list**. UD confirmed newer eisy-ui may complete while skipping unreadable plugin files. The Barn previously encountered HTTP 500 / `EACCES` at the private cache directory, and subsequently completed a backup after upgrading to eisy-ui 1.4.11 / IoX 6.1.2_16 / UDX 5.3.0_4. We have **not yet confirmed archive inclusion of all private cache files**. See [Troubleshooting](troubleshooting.md) and [Issue #17](https://github.com/mike-2545/CalendarRules/issues/17).
- On our tested eisy-ui installation, the existing plugin's **Reinstall** action fetched the newer Beta version in the same slot, preserving Google authorization, rules, and cache. Verify you have a successful backup first. Do **not** delete the plugin or use the separate publisher **Install** button to attempt an in-place update.

## Help and privacy

- [Configuration field reference](configuration-reference.md)
- [Troubleshooting and error notices](troubleshooting.md)
- [ISY examples and safety](isy-program-examples.md)
- [Security and privacy guidance](../SECURITY.md)
- [Privacy policy](https://calendarrules.org/privacy/)
- [Report a bug or ask a question](https://github.com/mike-2545/CalendarRules/issues) — omit private calendar titles, tokens, passwords, and sensitive logs.

**Release status (November 15, 2026):** CalendarRules Beta **v0.4.13** is installed on the Barn eisy, with missing configuration help icons now visible. During the prior 0.4.12 live test, Google OAuth/configuration/cache survived an in-place **Reinstall** upgrade; the node reported Connected, schedule evaluation and type-2 write/readback succeeded, an intentionally invalid +120-minute start offset failed closed, and changing back to -120 restored trusted scheduling. A Barn system backup also succeeded. The most recent 0.4.13 upgrade reportedly completed **without** the HTTP 504 warning seen on earlier installations; the cause of those earlier timeouts is not established. A full restore, complete event ON→OFF lifecycle, post-0.4.13 write/readback and backup, token longevity and public OAuth verification remain unverified.
