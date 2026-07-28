#! /usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import indigo


DEVICE_MONITOR = "irrigationMonitor"
TIME_SINCE_REFRESH_SECONDS = 60

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
        if not rainmachine_ids and not linktap_ids:
            errors = indigo.Dict()
            errors["rainMachineDevices"] = (
                "Select at least one RainMachine or LinkTap source device."
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
