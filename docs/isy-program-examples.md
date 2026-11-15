# ISY program examples — calendar demand signals

CalendarRules **only updates existing IoX type-2 state variables**. It does not issue commands to equipment, create variables, or decide what your automations should do.

These are *illustrative ISY-style examples*, not importable program files. Replace variable names, program paths and device commands with your actual IoX setup. **Test with a variable disconnected from production equipment first.**

## 1. Lighting demand

Google event: `Workshop`, 10:00 AM–noon.

CalendarRules rule: match field `title`, pattern `Workshop`, match type `contains`, offsets `0` and `0`, existing type-2 state-variable ID selected by the owner.

Illustrative ISY program:

```text
If
        $sWorkshop_Occupied is 1
Then
        Set 'Workshop Lights' On
Else
        Set 'Workshop Lights' Off
```

This shows how a user **might** react to a variable. It is **not a fail-safe**. If the plugin stops while the variable is ON, the variable might stay ON.

## 2. Occupancy request for preconditioning

Google event: `Pottery Class`, 10:00 AM–noon.

Rule: title contains `Pottery Class`; start offset `-120`; end offset `0`.

Effective desired-ON interval: 8:00 AM–noon.

```text
If
        $sPottery_Occupied is 1
Then
        Run Program 'Pottery Occupied HVAC' (Then Path)
Else
        Run Program 'Pottery Setback HVAC' (Then Path)
```

The user-defined ISY programs determine occupancy and equipment actions, including independent limits and overrides.

## 3. Location-based scheduling

Google event:

- Title: `Beginner Painting`
- Actual Google **Location** field: `Studio A`
- Time: 2:00 PM–4:00 PM

Rule: match field `location`, match type `exact`, pattern `Studio A`, start offset `-30`, end offset `15`.

Desired demand interval: 1:30 PM–4:15 PM.

The Location field must be populated on the Google Calendar event. A room name appearing only in the description is not a location match.

## 4. Multiple events sharing one variable

Event 1: `Studio A` from 10:00 AM to noon.

Event 2: `Studio A` from 11:30 AM to 1:00 PM.

Both match the same enabled rule and target. With zero offsets, the desired value stays `1` from 10:00 AM until **1:00 PM**. CalendarRules should not send an unnecessary OFF at noon while the second event still requires ON.

Every enabled rule currently applies to **every configured calendar**. That is not per-rule calendar targeting.

## 5. Notifications

Users can write ordinary ISY programs that react to a state variable and send a message through their preferred supported notification mechanism:

```text
If
        $sWorkshop_Occupied is 1
Then
        [Your configured notification action]
Else
        [Optional end-of-demand notification]
```

The exact action depends on the installed notification integration and the user's ISY program design.

## Operational safety and failure behavior

- All-day and cancelled Google events are ignored.
- Recurring event occurrences, edits, and overlapping windows are evaluated according to the trusted schedule.
- Offsets can move the effective demand window to a different calendar day.
- A missing, untrusted, or stale Google schedule is **not** treated as an empty calendar; it does not authorize a guessed OFF value.
- If CalendarRules stops, OAuth access is revoked, the eisy is offline, or licensing expires, **a previously ON state variable can remain ON**. Use an independently designed ISY watchdog, equipment interlock, or other safe fallback appropriate to the application.
- Do not operate life-safety equipment based on this Beta plugin.

For setup, see [Getting started](getting-started.md), [Configuration reference](configuration-reference.md), and [Troubleshooting](troubleshooting.md).
