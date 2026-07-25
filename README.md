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
  and most recent event as custom states.
- Marks the summary device unavailable when a configured source cannot provide
  a trustworthy state.
- Writes append-only JSON Lines history to:

  `Logs/Irrigation Monitor/irrigation-history.jsonl`

- Records a `start` event when a zone begins watering.
- Records a `stop` event containing `totalDurationSeconds`.
- Adds LinkTap volume and active fault fields to the stop event when available.
- Recovers an open session from history after a plugin restart.

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

## Installation

Double-click `Irrigation Monitor.indigoPlugin`, create one **Irrigation
monitor** device, then select the RainMachine controller and every LinkTap zone
to monitor.

This alpha targets Indigo API 3.8 and Python 3.13.
