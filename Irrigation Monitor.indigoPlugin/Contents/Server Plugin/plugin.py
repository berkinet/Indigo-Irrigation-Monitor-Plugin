#! /usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import hashlib
import json
import os
import ssl
import threading
import urllib.parse
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

import indigo


DEVICE_MONITOR = "irrigationMonitor"
TIME_SINCE_REFRESH_SECONDS = 60
PLANNED_EVENT_STATE_COUNT = 64
ZONE_DISPLAY_WIDTH = 23
NON_BREAKING_SPACE = "\u00a0"

RM_REQUIRED_STATES = frozenset(
    {"active_watering", "current_zone", "minutes_left"}
)
LT_REQUIRED_STATES = frozenset(
    {"is_watering", "remain_duration", "total_duration"}
)
LT_FAULT_STATES = (
    "is_broken",
    "is_clog",
    "is_cutoff",
    "is_fall",
    "is_leak",
)


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "open",
        "watering",
    }


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _selected_ids(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_values: Iterable[Any] = value.replace(";", ",").split(",")
    else:
        try:
            raw_values = list(value)
        except TypeError:
            raw_values = [value]

    selected: list[int] = []
    for raw in raw_values:
        text = str(raw).strip()
        if not text:
            continue
        try:
            device_id = int(text)
        except ValueError:
            continue
        if device_id not in selected:
            selected.append(device_id)
    return selected


@dataclass(frozen=True)
class SourceSnapshot:
    source_key: str
    source_type: str
    device_id: int
    device_name: str
    zone_name: str
    watering: bool
    available: bool
    remaining_minutes: float
    volume: Any = None
    faults: tuple[str, ...] = ()


@dataclass
class ActiveSession:
    session_id: str
    source_key: str
    source_type: str
    device_id: int
    device_name: str
    zone_name: str
    started_at: datetime


@dataclass(frozen=True)
class PlannedEvent:
    source: str
    name: str
    start: datetime
    end: datetime

    def clipped_to(self, day: date) -> "PlannedEvent | None":
        tzinfo = self.start.tzinfo
        day_start = datetime.combine(day, time.min, tzinfo=tzinfo)
        day_end = day_start + timedelta(days=1)
        start = max(self.start, day_start)
        end = min(self.end, day_end)
        if end <= start:
            return None
        return PlannedEvent(self.source, self.name, start, end)


class JsonlHistory:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        encoded = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def records(self) -> Iterable[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"Invalid JSON in history line {line_number}"
                    ) from None
                if isinstance(record, dict):
                    yield record

    def active_sessions(self) -> dict[str, ActiveSession]:
        sessions: dict[str, ActiveSession] = {}
        for record in self.records() or ():
            event = record.get("event")
            source_key = str(record.get("sourceKey", ""))
            if not source_key:
                continue
            if event == "start":
                try:
                    started_at = datetime.fromisoformat(str(record["time"]))
                except (KeyError, TypeError, ValueError):
                    continue
                sessions[source_key] = ActiveSession(
                    session_id=str(
                        record.get("sessionId") or uuid.uuid4().hex
                    ),
                    source_key=source_key,
                    source_type=str(record.get("source", "")),
                    device_id=int(record.get("deviceId", 0)),
                    device_name=str(record.get("deviceName", "")),
                    zone_name=str(record.get("zone", "")),
                    started_at=started_at,
                )
            elif event == "stop":
                sessions.pop(source_key, None)
        return sessions

    def last_record(self) -> dict[str, Any] | None:
        last = None
        for last in self.records() or ():
            pass
        return last

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        completed = deque(maxlen=max(0, limit))
        for record in self.records() or ():
            if record.get("event") == "stop":
                completed.append(record)
        return list(reversed(completed))


class Plugin(indigo.PluginBase):
    def __init__(
        self,
        plugin_id,
        plugin_display_name,
        plugin_version,
        plugin_prefs,
    ):
        super().__init__(
            plugin_id,
            plugin_display_name,
            plugin_version,
            plugin_prefs,
        )
        self._lock = threading.RLock()
        self._monitor_device_id: int | None = None
        self._history: JsonlHistory | None = None
        self._sessions: dict[str, ActiveSession] = {}
        self._schedule_refresh_date: date | None = None

    def startup(self):
        self.logger.info("Irrigation Monitor plugin started")
        indigo.devices.subscribeToChanges()
        self._ensure_history()
        self._sessions = self._history.active_sessions()
        for device in indigo.devices.iter("self"):
            if (
                device.deviceTypeId == DEVICE_MONITOR
                and device.enabled
            ):
                self._monitor_device_id = device.id
                self._populate_history_states(device)
                break

    def shutdown(self):
        self.logger.info("Irrigation Monitor plugin stopped")

    def runConcurrentThread(self):
        try:
            while True:
                self.sleep(TIME_SINCE_REFRESH_SECONDS)
                monitor = self._monitor_device()
                if monitor is None:
                    continue
                with self._lock:
                    self._update_time_since_last_watering(monitor)
                    now = _now()
                    if (
                        (now.hour, now.minute) >= (0, 1)
                        and self._schedule_refresh_date != now.date()
                    ):
                        self._update_todays_schedule(monitor, now.date())
        except self.StopThread:
            pass

    def deviceStartComm(self, device):
        super().deviceStartComm(device)
        if device.deviceTypeId != DEVICE_MONITOR:
            return
        with self._lock:
            self._monitor_device_id = device.id
            self._ensure_history()
            self._sessions = self._history.active_sessions()
            self._reconcile(device)

    def updateTodaysSchedule(self):
        """Plugin menu callback for an immediate schedule refresh."""
        monitor = self._monitor_device()
        if monitor is None:
            self.logger.error(
                "Unable to update today's schedule: no enabled Irrigation "
                "Monitor device is available"
            )
            return
        with self._lock:
            self._update_todays_schedule(monitor, _now().date())

    def logAllProgrammedEvents(self):
        """Log every configured program definition, without date filtering."""
        monitor = self._monitor_device()
        if monitor is None:
            self.logger.error(
                "Unable to log programmed events: no enabled Irrigation "
                "Monitor device is available"
            )
            return
        with self._lock:
            events, failures = self._all_programmed_events(monitor)
        self.logger.info(
            f"All programmed irrigation events ({len(events)} total):"
        )
        if not events:
            self.logger.info("No programmed irrigation events found")
        for index, event in enumerate(events, start=1):
            status = "" if event[3] else " | DISABLED"
            self.logger.info(
                f"{index:02d}. {event[0]} | {self._format_program_clock(event[1])}"
                f" | planned end {self._format_program_clock(event[2])}{status}"
            )
        for failure in failures:
            self.logger.error("Programmed event query failed: " + failure)

    def deviceStopComm(self, device):
        super().deviceStopComm(device)
        if device.id == self._monitor_device_id:
            self._monitor_device_id = None

    def deviceUpdated(self, original_device, new_device):
        super().deviceUpdated(original_device, new_device)
        monitor = self._monitor_device()
        if monitor is None or new_device.id == monitor.id:
            return
        configured = set(self._configured_source_ids(monitor))
        if new_device.id not in configured:
            return
        with self._lock:
            self._reconcile(monitor)

    def validateDeviceConfigUi(self, values_dict, type_id, device_id):
        if type_id != DEVICE_MONITOR:
            return True, values_dict

        rainmachine_ids = _selected_ids(
            values_dict.get("rainMachineDevices")
        )
        linktap_ids = _selected_ids(values_dict.get("linkTapDevices"))
        opensprinkler_host = str(
            self.pluginPrefs.get("openSprinklerHost") or ""
        ).strip()
        if not rainmachine_ids and not linktap_ids and not opensprinkler_host:
            errors = indigo.Dict()
            errors["rainMachineDevices"] = (
                "Select a RainMachine or LinkTap source, or configure "
                "OpenSprinkler."
            )
            errors["showAlertText"] = (
                "The irrigation monitor needs at least one source device."
            )
            return False, values_dict, errors

        duplicate_ids = set(rainmachine_ids).intersection(linktap_ids)
        if duplicate_ids:
            errors = indigo.Dict()
            errors["showAlertText"] = (
                "A source device cannot be selected as both RainMachine "
                "and LinkTap."
            )
            return False, values_dict, errors

        for existing in indigo.devices.iter("self"):
            if (
                existing.deviceTypeId == DEVICE_MONITOR
                and existing.id != device_id
            ):
                errors = indigo.Dict()
                errors["showAlertText"] = (
                    "Only one Irrigation Monitor device is supported."
                )
                return False, values_dict, errors

        return True, values_dict

    def validatePrefsConfigUi(self, values_dict):
        host = str(values_dict.get("openSprinklerHost") or "").strip()
        password = str(
            values_dict.get("openSprinklerPassword") or ""
        )
        if bool(host) != bool(password):
            errors = indigo.Dict()
            field = (
                "openSprinklerPassword" if host else "openSprinklerHost"
            )
            errors[field] = (
                "Enter both the OpenSprinkler address and password, or "
                "leave both blank."
            )
            errors["showAlertText"] = errors[field]
            return False, values_dict, errors
        values_dict["openSprinklerHost"] = host
        return True, values_dict

    def closedPrefsConfigUi(self, values_dict, user_cancelled):
        if user_cancelled:
            return
        monitor = self._monitor_device()
        if monitor is not None:
            with self._lock:
                self._update_todays_schedule(monitor, _now().date())

    def closedDeviceConfigUi(self, values_dict, user_cancelled, type_id, device_id):
        if user_cancelled or type_id != DEVICE_MONITOR:
            return
        monitor = self._device_by_id(device_id)
        if monitor is not None and monitor.enabled:
            with self._lock:
                self._monitor_device_id = monitor.id
                self._reconcile(monitor)

    def availableRainMachineDevices(
        self, filter="", valuesDict=None, typeId="", targetId=0
    ):
        return self._devices_with_states(RM_REQUIRED_STATES)

    def availableLinkTapDevices(
        self, filter="", valuesDict=None, typeId="", targetId=0
    ):
        return self._devices_with_states(LT_REQUIRED_STATES)

    def _devices_with_states(self, required_states):
        available = []
        for device in indigo.devices:
            states = set(getattr(device, "states", {}).keys())
            if required_states.issubset(states):
                available.append((str(device.id), device.name))
        return sorted(available, key=lambda item: item[1].casefold())

    def _ensure_history(self):
        if self._history is not None:
            return
        install_path = Path(indigo.server.getInstallFolderPath())
        history_path = (
            install_path
            / "Logs"
            / "Irrigation Monitor"
            / "irrigation-history.jsonl"
        )
        self._history = JsonlHistory(history_path)

    def _monitor_device(self):
        if self._monitor_device_id is None:
            return None
        monitor = self._device_by_id(self._monitor_device_id)
        if monitor is None or not monitor.enabled:
            return None
        return monitor

    @staticmethod
    def _device_by_id(device_id):
        try:
            return indigo.devices[int(device_id)]
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _configured_source_ids(monitor):
        props = monitor.pluginProps
        return (
            _selected_ids(props.get("rainMachineDevices"))
            + _selected_ids(props.get("linkTapDevices"))
        )

    def _snapshots(self, monitor):
        snapshots: list[SourceSnapshot] = []
        unavailable: list[str] = []

        for device_id in _selected_ids(
            monitor.pluginProps.get("rainMachineDevices")
        ):
            device = self._device_by_id(device_id)
            if device is None or not device.enabled:
                unavailable.append(f"RainMachine {device_id}")
                continue
            snapshot = self._rainmachine_snapshot(device)
            snapshots.append(snapshot)
            if not snapshot.available:
                unavailable.append(device.name)

        for device_id in _selected_ids(
            monitor.pluginProps.get("linkTapDevices")
        ):
            device = self._device_by_id(device_id)
            if device is None or not device.enabled:
                unavailable.append(f"LinkTap {device_id}")
                continue
            snapshot = self._linktap_snapshot(device)
            snapshots.append(snapshot)
            if not snapshot.available:
                unavailable.append(device.name)

        return snapshots, unavailable

    @staticmethod
    def _rainmachine_snapshot(device):
        states = device.states
        watering = _as_bool(states.get("active_watering"))
        zone_name = str(states.get("current_zone") or "").strip()
        available = _as_bool(states.get("device_online", True))
        if watering and not zone_name:
            available = False
        source_key = f"rainmachine:{device.id}:{zone_name}" if zone_name else (
            f"rainmachine:{device.id}"
        )
        display_name = zone_name
        if zone_name and not zone_name.casefold().startswith("rm "):
            display_name = f"RM {zone_name}"
        if not display_name:
            display_name = device.name
        return SourceSnapshot(
            source_key=source_key,
            source_type="RainMachine",
            device_id=device.id,
            device_name=device.name,
            zone_name=display_name,
            watering=watering,
            available=available,
            remaining_minutes=_as_float(states.get("minutes_left")),
        )

    @staticmethod
    def _linktap_snapshot(device):
        states = device.states
        available = _as_bool(states.get("is_rf_linked", True))
        faults = tuple(
            state_name
            for state_name in LT_FAULT_STATES
            if _as_bool(states.get(state_name, False))
        )
        return SourceSnapshot(
            source_key=f"linktap:{device.id}",
            source_type="LinkTap",
            device_id=device.id,
            device_name=device.name,
            zone_name=device.name,
            watering=_as_bool(states.get("is_watering")),
            available=available,
            remaining_minutes=_as_float(
                states.get("remain_duration")
            )
            / 60.0,
            volume=states.get("volume"),
            faults=faults,
        )

    def _reconcile(self, monitor):
        snapshots, unavailable = self._snapshots(monitor)
        configured_source_prefixes = {
            f"RainMachine:{device_id}"
            for device_id in _selected_ids(
                monitor.pluginProps.get("rainMachineDevices")
            )
        }
        configured_source_prefixes.update(
            f"LinkTap:{device_id}"
            for device_id in _selected_ids(
                monitor.pluginProps.get("linkTapDevices")
            )
        )
        available_source_prefixes = {
            self._source_prefix(snapshot)
            for snapshot in snapshots
            if snapshot.available
        }
        desired = {
            snapshot.source_key: snapshot
            for snapshot in snapshots
            if snapshot.available and snapshot.watering
        }

        for source_key, session in list(self._sessions.items()):
            prefix = self._session_prefix(session)
            if prefix not in configured_source_prefixes:
                self._stop_session(
                    session, snapshot=None, reason="sourceRemoved"
                )
                del self._sessions[source_key]
                continue
            if prefix not in available_source_prefixes:
                continue
            if source_key not in desired:
                ending_snapshot = self._snapshot_for_session(
                    snapshots, session
                )
                self._stop_session(session, ending_snapshot)
                del self._sessions[source_key]

        for source_key, snapshot in desired.items():
            for existing_key, existing in list(self._sessions.items()):
                if (
                    self._session_prefix(existing)
                    == self._source_prefix(snapshot)
                    and existing_key != source_key
                ):
                    self._stop_session(existing, snapshot)
                    del self._sessions[existing_key]
            if source_key not in self._sessions:
                self._sessions[source_key] = self._start_session(snapshot)

        self._update_monitor_states(monitor, snapshots, unavailable)

    @staticmethod
    def _source_prefix(snapshot):
        return f"{snapshot.source_type}:{snapshot.device_id}"

    @staticmethod
    def _session_prefix(session):
        return f"{session.source_type}:{session.device_id}"

    @staticmethod
    def _snapshot_for_session(snapshots, session):
        for snapshot in snapshots:
            if (
                snapshot.source_type == session.source_type
                and snapshot.device_id == session.device_id
            ):
                return snapshot
        return None

    def _start_session(self, snapshot):
        started_at = _now()
        session = ActiveSession(
            session_id=uuid.uuid4().hex,
            source_key=snapshot.source_key,
            source_type=snapshot.source_type,
            device_id=snapshot.device_id,
            device_name=snapshot.device_name,
            zone_name=snapshot.zone_name,
            started_at=started_at,
        )
        record = {
            "event": "start",
            "time": _iso(started_at),
            "sessionId": session.session_id,
            "source": session.source_type,
            "sourceKey": session.source_key,
            "deviceId": session.device_id,
            "deviceName": session.device_name,
            "zone": session.zone_name,
        }
        self._history.append(record)
        self.logger.info(f"Irrigation started: {session.zone_name}")
        return session

    def _stop_session(self, session, snapshot, reason=None):
        stopped_at = _now()
        total_duration = max(
            0, round((stopped_at - session.started_at).total_seconds())
        )
        record = {
            "event": "stop",
            "time": _iso(stopped_at),
            "sessionId": session.session_id,
            "source": session.source_type,
            "sourceKey": session.source_key,
            "deviceId": session.device_id,
            "deviceName": session.device_name,
            "zone": session.zone_name,
            "totalDurationSeconds": total_duration,
        }
        if reason is not None:
            record["reason"] = reason
        if snapshot is not None and session.source_type == "LinkTap":
            if snapshot.volume is not None:
                record["volume"] = snapshot.volume
            if snapshot.faults:
                record["faults"] = list(snapshot.faults)
        self._history.append(record)
        self.logger.info(
            f"Irrigation stopped: {session.zone_name} "
            f"({total_duration} seconds)"
        )

    def _update_monitor_states(self, monitor, snapshots, unavailable):
        active_snapshots = [
            snapshot
            for snapshot in snapshots
            if snapshot.available and snapshot.watering
        ]
        active_names = [
            session.zone_name for session in self._sessions.values()
        ]
        active_since = "--"
        if self._sessions:
            active_since = self._format_timestamp(
                min(session.started_at for session in self._sessions.values())
            )
        remaining_minutes = 0
        if active_snapshots:
            remaining_minutes = round(
                max(
                    snapshot.remaining_minutes
                    for snapshot in active_snapshots
                ),
                1,
            )

        changes = [
            {"key": "onOffState", "value": bool(self._sessions)},
            {
                "key": "activeZones",
                "value": ", ".join(active_names) if active_names else "None",
            },
            {"key": "activeCount", "value": len(active_names)},
            {"key": "activeSince", "value": active_since},
            {"key": "remainingMinutes", "value": remaining_minutes},
        ]
        changes.extend(self._history_state_changes(monitor))
        monitor.updateStatesOnServer(changes)
        monitor.updateStateOnServer(
            "onOffState",
            value=bool(self._sessions),
            uiValue="on" if self._sessions else "off",
            clearErrorState=not unavailable,
        )
        if unavailable:
            monitor.setErrorStateOnServer(
                "Unavailable: " + ", ".join(unavailable)
            )

    def _populate_history_states(self, monitor):
        monitor.updateStatesOnServer(self._history_state_changes(monitor))

    def _update_time_since_last_watering(self, monitor):
        if "timeSinceLastWatering" not in monitor.states:
            return
        recent_runs = self._history.recent_runs(1)
        last_stop = recent_runs[0] if recent_runs else None
        monitor.updateStateOnServer(
            "timeSinceLastWatering",
            value=self._format_time_since_stop(
                last_stop,
                watering=bool(self._sessions),
            ),
            triggerEvents=False,
        )

    def _update_todays_schedule(self, monitor, day):
        events = []
        failures = []
        for device_id in _selected_ids(
            monitor.pluginProps.get("rainMachineDevices")
        ):
            device = self._device_by_id(device_id)
            if device is None or not device.enabled:
                failures.append(f"RainMachine {device_id}: unavailable")
                continue
            try:
                events.extend(self._rainmachine_schedule(device, day))
            except Exception as error:
                failures.append(f"{device.name}: {error}")

        host = str(
            self.pluginPrefs.get("openSprinklerHost") or ""
        ).strip()
        if host:
            try:
                events.extend(self._opensprinkler_schedule(monitor, day))
            except Exception as error:
                failures.append(f"OpenSprinkler: {error}")

        merged = []
        for event in events:
            clipped = event.clipped_to(day)
            if clipped is not None:
                merged.append(clipped)
        merged.sort(
            key=lambda event: (
                event.start,
                event.end,
                event.source.casefold(),
                event.name.casefold(),
            )
        )

        overflow = max(0, len(merged) - PLANNED_EVENT_STATE_COUNT)
        changes = [
            {"key": "plannedEventCount", "value": len(merged)},
            {"key": "plannedScheduleDate", "value": day.isoformat()},
            {"key": "plannedScheduleUpdated", "value": _iso(_now())},
            {
                "key": "plannedScheduleStatus",
                "value": self._schedule_status(failures, overflow),
            },
        ]
        for index in range(PLANNED_EVENT_STATE_COUNT):
            value = ""
            if index < len(merged):
                value = self._format_planned_event(merged[index])
            changes.append(
                {"key": f"plannedEvent{index + 1}", "value": value}
            )
        monitor.updateStatesOnServer(changes)
        self._schedule_refresh_date = day
        if failures:
            self.logger.error(
                "Today's schedule updated with errors: " + "; ".join(failures)
            )
        else:
            self.logger.info(
                f"Today's schedule updated: {len(merged)} planned events"
            )
        if overflow:
            self.logger.error(
                f"Today's schedule has {overflow} events beyond the "
                f"{PLANNED_EVENT_STATE_COUNT} available event states"
            )

    @staticmethod
    def _schedule_status(failures, overflow):
        if failures:
            return "Partial: " + "; ".join(failures)
        if overflow:
            return f"Overflow: {overflow} events not displayed"
        return "Available"

    @staticmethod
    def _format_planned_event(event):
        return f"{event.name} | {event.start:%H:%M} | {event.end:%H:%M}"

    def _all_programmed_events(self, monitor):
        events = []
        failures = []
        for device_id in _selected_ids(
            monitor.pluginProps.get("rainMachineDevices")
        ):
            device = self._device_by_id(device_id)
            if device is None or not device.enabled:
                failures.append(f"RainMachine {device_id}: unavailable")
                continue
            try:
                events.extend(self._all_rainmachine_programs(device))
            except Exception as error:
                failures.append(f"{device.name}: {error}")
        if str(self.pluginPrefs.get("openSprinklerHost") or "").strip():
            try:
                events.extend(self._all_opensprinkler_programs())
            except Exception as error:
                failures.append(f"OpenSprinkler: {error}")
        events.sort(key=lambda event: (event[1], event[2], event[0].casefold()))
        return events, failures

    @staticmethod
    def _format_program_clock(total_seconds):
        total_seconds = max(0, int(total_seconds))
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        suffix = f" +{days}d" if days else ""
        return f"{hours:02d}:{minutes:02d}{suffix}"

    def _all_opensprinkler_programs(self):
        host = str(self.pluginPrefs.get("openSprinklerHost") or "").strip()
        password = str(self.pluginPrefs.get("openSprinklerPassword") or "")
        token = hashlib.md5(password.encode("utf-8")).hexdigest()
        url = f"http://{host}/ja?" + urllib.parse.urlencode({"pw": token})
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = json.load(response)
        return self._parse_all_opensprinkler_programs(payload)

    @classmethod
    def _parse_all_opensprinkler_programs(cls, payload):
        settings = payload.get("settings", {})
        options = payload.get("options", {})
        programs = payload.get("programs", {})
        stations = payload.get("stations", {})
        names = stations.get("snames", [])
        groups = stations.get("stn_grp", [0] * len(names))
        station_delay = int(_as_float(options.get("sdt"), 0))
        sunrise = int(_as_float(settings.get("sunrise"), 360))
        sunset = int(_as_float(settings.get("sunset"), 1080))
        result = []
        for program in programs.get("pd", []):
            if not isinstance(program, list) or len(program) < 6:
                continue
            flags, _days0, _days1, starts, durations, name = program[:6]
            start_minutes = cls._opensprinkler_start_minutes(
                int(flags), starts, sunrise, sunset
            )
            run_seconds = [
                (index, max(0, int(_as_float(duration))))
                for index, duration in enumerate(durations)
                if duration and index < len(names)
            ]
            if not start_minutes or not run_seconds:
                continue
            first_start = min(start_minutes) * 60
            final_end = first_start
            for start_minute in start_minutes:
                group_ends = {}
                latest = start_minute * 60
                for station_index, duration in run_seconds:
                    group = groups[station_index] if station_index < len(groups) else 0
                    station_start = max(start_minute * 60, group_ends.get(group, 0))
                    station_end = station_start + duration
                    group_ends[group] = station_end + station_delay
                    latest = max(latest, station_end)
                final_end = max(final_end, latest)
            result.append(
                (
                    f"OS {str(name).strip()}",
                    first_start,
                    final_end,
                    bool(int(flags) & 1),
                )
            )
        return result

    def _all_rainmachine_programs(self, device):
        programs = self._rainmachine_program_payload(device)
        result = []
        for program in programs:
            raw_start = str(program.get("startTime") or "00:00")
            try:
                hours, minutes = (int(value) for value in raw_start.split(":")[:2])
            except (TypeError, ValueError):
                continue
            start_seconds = hours * 3600 + minutes * 60
            duration = sum(
                max(0, int(_as_float(zone.get("duration", 0))))
                for zone in program.get("wateringTimes", [])
                if _as_bool(zone.get("active", False))
            )
            if _as_bool(program.get("delay_on", False)):
                duration += max(0, int(_as_float(program.get("delay", 0))))
            cycles = max(1, int(_as_float(program.get("cycles", 1))))
            if _as_bool(program.get("cs_on", False)) and cycles > 1:
                duration += (cycles - 1) * max(
                    0, int(_as_float(program.get("soak", 0)))
                )
            name = str(program.get("name") or "Unnamed program").strip()
            if not name.casefold().startswith("rm "):
                name = "RM " + name
            result.append(
                (
                    name,
                    start_seconds,
                    start_seconds + duration,
                    _as_bool(program.get("active", False)),
                )
            )
        return result

    def _rainmachine_program_payload(self, device):
        props = device.pluginProps
        host = str(props.get("ip_address") or "").strip()
        password = str(props.get("password") or "")
        base, context = self._rainmachine_local_endpoint(props, host)
        body = json.dumps({"pwd": password, "remember": True}).encode()
        request = urllib.request.Request(
            base + "/auth/login",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15, context=context) as response:
            token = json.load(response)["access_token"]
        url = base + "/program?" + urllib.parse.urlencode(
            {"access_token": token}
        )
        with urllib.request.urlopen(url, timeout=15, context=context) as response:
            return json.load(response).get("programs", [])

    @staticmethod
    def _rainmachine_local_endpoint(props, host):
        connection_type = str(
            props.get("connectionType") or "Local"
        ).strip()
        if connection_type.casefold() != "local":
            raise ValueError(
                "program discovery currently requires a local "
                "RainMachine connection"
            )
        if not host:
            raise ValueError("RainMachine local IP address is missing")
        return (
            f"https://{host}:8080/api/4",
            ssl._create_unverified_context(),
        )

    def _opensprinkler_schedule(self, monitor, day):
        host = str(
            self.pluginPrefs.get("openSprinklerHost") or ""
        ).strip()
        password = str(
            self.pluginPrefs.get("openSprinklerPassword") or ""
        )
        token = hashlib.md5(password.encode("utf-8")).hexdigest()
        url = f"http://{host}/ja?" + urllib.parse.urlencode({"pw": token})
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = json.load(response)
        return self._parse_opensprinkler_schedule(payload, day)

    @classmethod
    def _parse_opensprinkler_schedule(cls, payload, day):
        settings = payload.get("settings", {})
        options = payload.get("options", {})
        programs = payload.get("programs", {})
        stations = payload.get("stations", {})
        names = stations.get("snames", [])
        groups = stations.get("stn_grp", [0] * len(names))
        weather_level = _as_float(options.get("wl"), 100.0) / 100.0
        station_delay = int(_as_float(options.get("sdt"), 0))
        sunrise = int(_as_float(settings.get("sunrise"), 360))
        sunset = int(_as_float(settings.get("sunset"), 1080))
        tzinfo = _now().tzinfo
        day_start = datetime.combine(day, time.min, tzinfo=tzinfo)
        epoch_day = (day - date(1970, 1, 1)).days
        result = []

        for program in programs.get("pd", []):
            if not isinstance(program, list) or len(program) < 6:
                continue
            flags, days0, days1, starts, durations, name = program[:6]
            if not int(flags) & 1:
                continue
            program_type = (int(flags) >> 4) & 3
            if not cls._opensprinkler_day_matches(
                program_type, int(days0), int(days1), day, epoch_day
            ):
                continue
            start_minutes = cls._opensprinkler_start_minutes(
                int(flags), starts, sunrise, sunset
            )
            if not start_minutes:
                continue
            run_seconds = []
            for index, raw_duration in enumerate(durations):
                seconds = max(0, int(_as_float(raw_duration)))
                if int(flags) & 2:
                    seconds = round(seconds * weather_level)
                if seconds and index < len(names):
                    run_seconds.append((index, seconds))
            if not run_seconds:
                continue

            spans = []
            for start_minute in start_minutes:
                group_ends = {}
                latest = start_minute * 60
                for station_index, duration in run_seconds:
                    group = groups[station_index] if station_index < len(groups) else 0
                    station_start = max(start_minute * 60, group_ends.get(group, 0))
                    station_end = station_start + duration
                    group_ends[group] = station_end + station_delay
                    latest = max(latest, station_end)
                spans.append((start_minute * 60, latest))
            first = min(value[0] for value in spans)
            last = max(value[1] for value in spans)
            result.append(
                PlannedEvent(
                    source="OpenSprinkler",
                    name=f"OS {str(name).strip()}",
                    start=day_start + timedelta(seconds=first),
                    end=day_start + timedelta(seconds=last),
                )
            )
        return result

    @staticmethod
    def _opensprinkler_day_matches(
        program_type, days0, days1, day, epoch_day
    ):
        if program_type == 0:
            return bool(days0 & (1 << day.weekday()))
        if program_type == 1:
            return day.day % 2 == 1 and not (
                day.day == 31 or (day.month == 2 and day.day == 29)
            )
        if program_type == 2:
            return day.day % 2 == 0
        if program_type == 3:
            return days1 > 0 and epoch_day % days1 == days0
        return False

    @staticmethod
    def _opensprinkler_start_minutes(flags, starts, sunrise, sunset):
        def decode(value):
            value = int(value)
            if value < 0 or value & 0x8000:
                return None
            offset = value & 0x7FF
            if value & 0x1000:
                offset = -offset
            if value & 0x2000:
                return max(0, sunrise + offset)
            if value & 0x4000:
                return max(0, sunset + offset)
            return value

        if flags & 0x40:
            return [
                value
                for value in (decode(item) for item in starts)
                if value is not None
            ]
        if len(starts) < 3:
            return []
        first = decode(starts[0])
        repeat = int(starts[1])
        interval = int(starts[2])
        if first is None:
            return []
        return [first + interval * index for index in range(repeat + 1)]

    def _rainmachine_schedule(self, device, day):
        props = device.pluginProps
        host = str(props.get("ip_address") or "").strip()
        password = str(props.get("password") or "")
        base, context = self._rainmachine_local_endpoint(props, host)
        body = json.dumps({"pwd": password, "remember": True}).encode()
        request = urllib.request.Request(
            base + "/auth/login",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(
            request, timeout=15, context=context
        ) as response:
            token = json.load(response)["access_token"]

        def get(endpoint):
            url = base + "/" + endpoint + "?" + urllib.parse.urlencode(
                {"access_token": token}
            )
            with urllib.request.urlopen(
                url, timeout=15, context=context
            ) as response:
                return json.load(response)

        programs = get("program").get("programs", [])
        names = {int(item["uid"]): item.get("name", "") for item in programs}
        endpoint = f"watering/log/simulated/details/{day.isoformat()}/1"
        return self._parse_rainmachine_schedule(get(endpoint), day, names)

    @staticmethod
    def _parse_rainmachine_schedule(payload, day, program_names):
        result = []
        tzinfo = _now().tzinfo
        for day_record in payload.get("waterLog", {}).get("days", []):
            if day_record.get("date") and day_record.get("date") != day.isoformat():
                continue
            for program in day_record.get("programs", []):
                starts = []
                ends = []
                for zone in program.get("zones", []):
                    for cycle in zone.get("cycles", []):
                        raw_start = cycle.get("startTime")
                        if not raw_start:
                            continue
                        try:
                            start = datetime.fromisoformat(str(raw_start))
                        except ValueError:
                            start = datetime.strptime(
                                str(raw_start), "%Y-%m-%d %H:%M:%S"
                            )
                        if start.tzinfo is None:
                            start = start.replace(tzinfo=tzinfo)
                        duration = max(
                            0,
                            int(
                                _as_float(
                                    cycle.get(
                                        "machineDuration",
                                        cycle.get("userDuration", 0),
                                    )
                                )
                            ),
                        )
                        starts.append(start)
                        ends.append(start + timedelta(seconds=duration))
                if not starts:
                    continue
                program_id = int(program.get("id", 0))
                name = str(
                    program_names.get(program_id)
                    or program.get("name")
                    or f"Program {program_id}"
                ).strip()
                if not name.casefold().startswith("rm "):
                    name = "RM " + name
                result.append(
                    PlannedEvent(
                        source="RainMachine",
                        name=name,
                        start=min(starts),
                        end=max(ends),
                    )
                )
        return result

    def _history_state_changes(self, monitor=None):
        last_record = self._history.last_record()
        recent_runs = self._history.recent_runs(10)
        last_stop = recent_runs[0] if recent_runs else None
        changes = [
            {"key": "lastEvent", "value": self._format_event(last_record)},
            {"key": "historyFile", "value": str(self._history.path)},
            {
                "key": "timeSinceLastWatering",
                "value": self._format_time_since_stop(
                    last_stop,
                    watering=bool(self._sessions),
                ),
            },
        ]
        for index in range(10):
            value = ""
            if index < len(recent_runs):
                value = self._format_run(recent_runs[index])
            changes.append(
                {"key": f"recentRun{index + 1}", "value": value}
            )
        if (
            monitor is not None
            and "timeSinceLastWatering" not in monitor.states
        ):
            changes = [
                change
                for change in changes
                if change["key"] != "timeSinceLastWatering"
            ]
        return changes

    @staticmethod
    def _format_event(record):
        if not record:
            return "No events recorded"
        zone = str(record.get("zone", "Unknown zone"))
        if record.get("event") == "start":
            return f"{record.get('time', '')} - {zone} started"
        duration = record.get("totalDurationSeconds", 0)
        return (
            f"{record.get('time', '')} - {zone} stopped "
            f"after {duration} seconds"
        )

    @classmethod
    def _format_run(cls, record):
        timestamp = cls._format_timestamp(record.get("time", ""))
        zone = str(record.get("zone", "Unknown zone"))
        zone += NON_BREAKING_SPACE * max(
            0,
            ZONE_DISPLAY_WIDTH - len(zone),
        )
        duration = cls._format_duration(
            record.get("totalDurationSeconds", 0)
        )
        details = [timestamp, zone, duration]
        faults = record.get("faults") or ()
        if faults:
            details.append("fault " + ", ".join(str(value) for value in faults))
        return " | ".join(details)

    @staticmethod
    def _format_duration(value):
        total_seconds = max(0, round(_as_float(value)))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    @classmethod
    def _format_time_since_stop(cls, record, now=None, watering=False):
        if watering:
            return "00:00"
        if not record:
            return "Never"
        try:
            stopped_at = datetime.fromisoformat(str(record["time"]))
            elapsed = (now or _now()) - stopped_at
        except (KeyError, TypeError, ValueError):
            return "Unknown"
        total_minutes = max(0, int(elapsed.total_seconds() // 60))
        hours, minutes = divmod(total_minutes, 60)
        return f"{hours:02d}:{minutes:02d}"

    @staticmethod
    def _format_timestamp(value):
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value))
            except ValueError:
                return str(value)
        return parsed.strftime("%d/%m %H:%M")
