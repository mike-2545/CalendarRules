# Getting started with CalendarRules (Beta 0.4.12)

1. Install CalendarRules from the eisy-ui Plugin Store. The publisher must have an active Beta purchase option and a working archive.
2. Under **Overview**, choose **Authenticate**, then authorize the Google Calendar account you want to read. The Google OAuth application currently requires separate public-consent review before general release.
3. Open **Configuration**, select an IANA timezone, add one or more Google Calendar IDs, and add enabled rule(s).
4. Each rule specifies a friendly name, `title` or `location` match field, pattern, match type (`contains`, `exact`, `starts_with`, `ends_with`), case sensitivity, an **existing type-2 IoX state-variable ID**, start/end minute offsets and Enabled.
5. Save Changes, check Overview for a trusted calendar, and validate an isolated test state variable before allowing the owner-authorized IoX API access.
6. Build your own IoX programs to respond to state variable values; CalendarRules does not create state variables and does not directly control HVAC, doors or lights.

**Safety:** Never use an existing facility-equipment variable for the first test. A variable can remain at its last value if the plugin stops, authorization fails, or its license expires. Create separate ISY fail-safe logic where necessary.

The eisy-ui **Configuration Refresh** button reloads form values; it is not a Google Calendar refresh. Use the Google Calendar node's **Refresh Now** action for a forced calendar fetch where available.

See [ISY program examples](isy-program-examples.md), [rule field details](eisy-ui-configuration.md), and [security guidance](../SECURITY.md).

## Testing status

On November 15, 2026, the Barn eisy running eisy-ui 1.4.11 accepted CalendarRules Beta **0.4.12** through the existing plugin's **Reinstall** action. Google authorization, calendar data, rules, and cache persisted. The node reported Connected, a trusted schedule evaluated, and type-2 variable writes/readback were verified. A system backup succeeded after the upgrade.

The invalid-offset diagnostic was also tested live: with a start offset of `+120` and a shorter matching event, CalendarRules correctly reported that the adjusted interval was invalid, that the Google fetch succeeded, and that no state decisions were produced. Returning to `-120` restored normal operation.

**Upgrade guidance:** Retain a successful backup before upgrading. If no Update button is available, eisy-ui may offer **Reinstall** on the existing plugin Overview. Do not use the separate publisher Install action or Delete as an update substitute. A browser HTTP 504 appeared during an otherwise successful installation; confirm the plugin's actual version and health before retrying anything.

**Still unverified:** full restore, complete event ON/OFF lifecycle, failed-upgrade recovery, long-term OAuth token behavior, and public OAuth consent approval. Use isolated test variables before equipment automation.
