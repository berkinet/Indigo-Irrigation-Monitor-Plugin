# Indigo Irrigation Monitor

Alpha Indigo plugin that consolidates irrigation activity already reported by:

- RainMachine2 controller devices; and
- LinkTap zone devices maintained by OpenSprinkler LinkTap Bridge or MQTT Shims.

The plugin does not connect to RainMachine, LinkTap, OpenSprinkler, or MQTT
directly. It subscribes to Indigo device changes and creates one read-only
summary device.

## Alpha behavior

- Reports **on** whenever any configured zone is watering.
- Reports **off** when every available configured source is idle.
- Shows the active zone names, count, earliest start time, remaining minutes,
  and most recent event as custom states. `activeSince` reads `--` while idle
  so its Indigo Control Page caption remains correctly positioned.
- Shows `timeSinceLastWatering` as `HH:MM`, measured from the newest
  completed watering run and refreshed every minute. It reads `00:00`
  whenever any zone is actively watering.
- Exposes `recentRun1` through `recentRun10`, newest first, for use as fixed
  text rows on an Indigo Control Page. Rows use
  `25/07 14:56 | RM Pool Refill | 00:01:10` formatting. LinkTap volume is
  retained in the history file but omitted from these display states. Zone
  names are right-padded to a 23-character field with non-breaking spaces so
  Indigo's browser display preserves alignment with a monospaced font.
- Prefixes RainMachine zone names with `RM`; LinkTap zone names use their
  Indigo device names.
- Reloads the history file and repopulates the recent-run states when the
  plugin starts.
- Marks the summary device unavailable when a configured source cannot provide
  a trustworthy state. Routine time, history, and schedule updates preserve this
  error until a source check confirms recovery. Disabled sources are temporarily
  excluded and automatically monitored again when enabled, without changing
  selections. Disabling an active source closes its tracked session with reason
  `sourceDisabled`; this records the end of monitoring, not a confirmed valve stop.
- Writes append-only JSON Lines history to:

  `Logs/Irrigation Monitor/irrigation-history.jsonl`

- Records a `start` event when a zone begins watering.
- Records a `stop` event containing `totalDurationSeconds`.
- Adds LinkTap volume and active fault fields to the stop event when available.
- Recovers an open session from history after a plugin restart.
- Collects today's RainMachine and OpenSprinkler plans after 00:01, merges
  them in start-time order, and exposes `plannedEvent1` through
  `plannedEvent64`. Each populated state uses
  `program name | HH:MM | HH:MM` formatting. Repeated cycles belonging to one
  program are represented by one span from the first start to the final end.
- Provides **Plugins -> Irrigation Monitor -> Update Today's Schedule** for an
  immediate refresh using the same collection path as the daily job.
- Provides **Plugins -> Irrigation Monitor -> Log All Programmed Events** to
  query every RainMachine and OpenSprinkler program definition without date
  filtering and write a start-time-ordered list to the Indigo log. Cyclic
  starts are collapsed into one span and planned ends use configured base
  durations, not unavailable future ETo adjustments.

## Source state requirements

RainMachine devices must expose:

- `active_watering`
- `current_zone`
- `minutes_left`

The optional `device_online` state is used to detect availability.

MQTT Shims LinkTap devices must expose:

- `is_watering`
- `remain_duration`
- `total_duration`

The optional `is_rf_linked` state is used to detect availability. Indigo device
names are used as LinkTap zone names.

OpenSprinkler LinkTap Bridge zones expose `watering`, `statusKnown`, and
`requestedSeconds`. The monitor uses confirmed `watering` and marks the source
unavailable while `statusKnown` is false. Requested commands do not count as
watering. Bridge 0.1.13 also exposes `remain_duration` (seconds), `volume`,
and LinkTap fault flags. The monitor converts reported remaining duration to
minutes and retains volume and active faults in stop-event history. Older bridge
versions without these fields remain supported, with zero remaining minutes and
no volume or faults. `requestedSeconds` is never used as a remaining-time estimate.
Bridge availability uses `statusKnown`; optional RF telemetry does not override
that freshness signal.

After switching from MQTT Shims, edit the Irrigation monitor device and replace
its old LinkTap selections with the new bridge's Virtual Irrigation Zone
devices (one per physical valve). Keep the RainMachine selection. Old device IDs
are not automatically mapped to new devices. A missing old source
appears as `Unavailable: LinkTap <device ID>` until its selection is removed.

OpenSprinkler schedule collection connects directly to the controller's local
JSON API. Enter its IP address or hostname and password under **Plugins ->
Irrigation Monitor -> Configure**; the password field is concealed.
OpenSprinkler program durations use the controller's current weather-adjusted
watering level.

## Installation

Double-click `Irrigation Monitor.indigoPlugin`, create one **Irrigation
monitor** device, then select the RainMachine controller and every LinkTap zone
to monitor.

This alpha targets Indigo API 3.8 and Python 3.13.
