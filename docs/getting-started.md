# Getting started with CalendarRules (Beta 0.4.10)

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

A separate ISY Portal account and second eisy installed Beta 0.4.10 and verified Google retrieval plus IoX type-2 write/readback on November 14, 2026. The Google account was shared with the home test; another Google account, token longevity, Update Now, backup/restore, and lifecycle transitions remain unverified. Do not treat these as completed acceptance tests.
