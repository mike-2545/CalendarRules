# ISY program examples (planned integration)

The plugin is planned to update **existing state variables**. The following are illustrative ISY-style programs, not imported program files. Create the variables and devices on your IoX and adjust program syntax to your actual device tree.

## 1. Workshop lighting

Calendar: `Workshop` 10:00 AM–12:00 PM. Rule: keyword `Workshop`, state variable ID `10`, 0-minute offsets.

```text
If
        $sWorkshop_Occupied is 1
Then
        Set 'Workshop Lights' On
Else
        Set 'Workshop Lights' Off
```

## 2. Precondition HVAC

Calendar: `Pottery` 10:00 AM–noon. Rule: start offset `-120` minutes, end offset `0`. This example models the *demand signal*, not specific thermostat commands. Existing IoX programs choose occupied vs setback temperatures.

```text
If
        $sPottery_Occupied is 1
Then
        Run Program 'Pottery Occupied HVAC' (Then Path)
Else
        Run Program 'Pottery Setback HVAC' (Then Path)
```

## 3. Placeholder / shared facility scheduling

One calendar can carry placeholder titles (`Conference Room`, `Book Center`, `Pottery`) matched by separate rules, or multiple calendars can map to one shared variable. A placeholder can represent occupancy without a formal class booking.

```text
Calendar event title: Conference Room
Event time: 2:00 PM–3:30 PM
Rule: keyword Conference Room; target state variable 5; start offset -120
Expected: variable 5 = 1 from noon to 3:30 PM
```

**Overlap policy**: if two matching events require the same variable at 3:30 PM, it stays at `1`; it drops to `0` only when the last adjusted event ends.

## 4. Notification on an occupancy transition

```text
If
        $sWorkshop_Occupied is 1
Then
        [Choose an existing Notification plugin message action]
Else
        [Optional end-of-event message]
```

The exact Notification action depends on the installed plugin version and service configuration.

## 5. What placeholders do *not* do

A placeholder does not directly unlock a door, control HVAC, or command a device. It requests occupancy through a state variable. Normal ISY programs, including any safety interlocks, remain responsible for actions.

## Timing and resilience notes

- All-day and cancelled entries are ignored by default.
- Recurring occurrences should be evaluated individually, respecting exceptions.
- Offsets can shift the effective window to the previous or following day.
- Calendar API errors must not be interpreted as zero matching events.
- After restart or an edited/deleted event, recompute desired state and correct it instead of depending on missed trigger windows.
- Some programs may need a one-time synchronization strategy after startup; confirm behavior through testing.
