# Indigo Irrigation Monitor

Alpha Indigo plugin that consolidates irrigation activity already reported by:

- RainMachine2 controller devices; and
- LinkTap zone devices maintained by MQTT Shims.

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
  a trustworthy state.
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

## Source state requirements

RainMachine devices must expose:

- `active_watering`
- `current_zone`
- `minutes_left`

The optional `device_online` state is used to detect availability.

LinkTap devices must expose:

- `is_watering`
- `remain_duration`
- `total_duration`

The optional `is_rf_linked` state is used to detect availability. Indigo device
names are used as LinkTap zone names.

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
