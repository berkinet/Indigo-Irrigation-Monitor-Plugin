import importlib.util
import logging
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SERVER = (
    ROOT
    / "Irrigation Monitor.indigoPlugin"
    / "Contents"
    / "Server Plugin"
)
sys.path.insert(0, str(SERVER))


class PluginBase:
    def __init__(self, plugin_id, display_name, version, prefs):
        self.pluginId = plugin_id
        self.pluginPrefs = prefs
        self.logger = logging.getLogger("test")

    def deviceStartComm(self, device):
        pass

    def deviceStopComm(self, device):
        pass

    def deviceUpdated(self, original_device, new_device):
        pass


class DeviceCollection:
    def __init__(self):
        self.items = {}
        self.subscribed = False

    def __iter__(self):
        return iter(self.items.values())

    def __getitem__(self, device_id):
        return self.items[device_id]

    def iter(self, _filter=None):
        return iter(self.items.values())

    def subscribeToChanges(self):
        self.subscribed = True


indigo = ModuleType("indigo")
indigo.PluginBase = PluginBase
indigo.Dict = dict
indigo.devices = DeviceCollection()
indigo.server = SimpleNamespace(getInstallFolderPath=Mock())
sys.modules["indigo"] = indigo

spec = importlib.util.spec_from_file_location(
    "irrigation_monitor_plugin", SERVER / "plugin.py"
)
plugin_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin_module
spec.loader.exec_module(plugin_module)


def device(
    device_id,
    name,
    states,
    *,
    device_type="external",
    props=None,
):
    return SimpleNamespace(
        id=device_id,
        name=name,
        states=dict(states),
        enabled=True,
        deviceTypeId=device_type,
        pluginProps=dict(props or {}),
        updateStatesOnServer=Mock(),
        updateStateOnServer=Mock(),
        setErrorStateOnServer=Mock(),
    )


class IrrigationMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        indigo.devices.items = {}
        indigo.devices.subscribed = False
        indigo.server.getInstallFolderPath.return_value = self.temp_dir.name
        self.plugin = plugin_module.Plugin(
            "plugin.id", "Irrigation Monitor", "0.1.10", {}
        )
        self.plugin.startup()

    @property
    def history_path(self):
        return (
            Path(self.temp_dir.name)
            / "Logs"
            / "Irrigation Monitor"
            / "irrigation-history.jsonl"
        )

    def make_monitor(self, rainmachine=(), linktap=()):
        monitor = device(
            1,
            "Irrigation",
            {},
            device_type=plugin_module.DEVICE_MONITOR,
            props={
                "rainMachineDevices": [str(value) for value in rainmachine],
                "linkTapDevices": [str(value) for value in linktap],
            },
        )
        indigo.devices.items[monitor.id] = monitor
        self.plugin.deviceStartComm(monitor)
        return monitor

    def records(self):
        history = plugin_module.JsonlHistory(self.history_path)
        return list(history.records() or ())

    def test_startup_subscribes_to_indigo_device_changes(self):
        self.assertTrue(indigo.devices.subscribed)

    def test_active_since_uses_placeholder_while_idle(self):
        monitor = self.make_monitor()
        changes = {
            change["key"]: change["value"]
            for change in monitor.updateStatesOnServer.call_args.args[0]
        }

        self.assertEqual(changes["activeSince"], "--")

    def test_startup_repopulates_history_states(self):
        monitor = device(
            1,
            "Irrigation",
            {"timeSinceLastWatering": ""},
            device_type=plugin_module.DEVICE_MONITOR,
        )
        indigo.devices.items[monitor.id] = monitor
        plugin_module.JsonlHistory(self.history_path).append(
            {
                "event": "stop",
                "time": "2026-07-25T14:56:36+02:00",
                "zone": "RM Pool Refill",
                "totalDurationSeconds": 61,
            }
        )

        restarted = plugin_module.Plugin(
            "plugin.id", "Irrigation Monitor", "0.1.10", {}
        )
        with patch.object(
            plugin_module,
            "_now",
            return_value=datetime.fromisoformat(
                "2026-07-25T15:56:36+02:00"
            ),
        ):
            restarted.startup()

        changes = {
            change["key"]: change["value"]
            for change in monitor.updateStatesOnServer.call_args.args[0]
        }
        self.assertEqual(
            changes["recentRun1"],
            "25/07 14:56 | "
            + "RM Pool Refill"
            + plugin_module.NON_BREAKING_SPACE * 9
            + " | 00:01:01",
        )
        self.assertEqual(changes["recentRun2"], "")
        self.assertEqual(changes["timeSinceLastWatering"], "01:00")

    def test_dynamic_source_lists_use_required_states(self):
        rm = device(
            2,
            "RainMachine",
            {
                "active_watering": False,
                "current_zone": "all off",
                "minutes_left": 0,
            },
        )
        lt = device(
            3,
            "Orchard",
            {
                "is_watering": False,
                "remain_duration": 0,
                "total_duration": 0,
            },
        )
        unrelated = device(4, "Lamp", {"onOffState": False})
        indigo.devices.items = {
            item.id: item for item in (rm, lt, unrelated)
        }

        self.assertEqual(
            self.plugin.availableRainMachineDevices(), [("2", "RainMachine")]
        )
        self.assertEqual(
            self.plugin.availableLinkTapDevices(), [("3", "Orchard")]
        )

    def test_opensprinkler_cycles_are_grouped_as_one_program_event(self):
        payload = {
            "settings": {"sunrise": 400, "sunset": 1235},
            "options": {"wl": 148, "sdt": 5},
            "stations": {
                "snames": ["Front entry"],
                "stn_grp": [0],
            },
            "programs": {
                "pd": [
                    [
                        51,
                        0,
                        2,
                        [145, 5, 1, 0],
                        [60],
                        "Front entry",
                    ]
                ]
            },
        }

        events = self.plugin._parse_opensprinkler_schedule(
            payload, date(2026, 8, 17)
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].name, "OS Front entry")
        self.assertEqual(events[0].start.strftime("%H:%M:%S"), "02:25:00")
        self.assertEqual(events[0].end.strftime("%H:%M:%S"), "02:31:29")

    def test_all_opensprinkler_programs_ignore_day_and_weather(self):
        payload = {
            "settings": {"sunrise": 400, "sunset": 1235},
            "options": {"wl": 25, "sdt": 5},
            "stations": {"snames": ["Front"], "stn_grp": [0]},
            "programs": {
                "pd": [
                    [51, 1, 99, [145, 2, 10, 0], [60], "Front"]
                ]
            },
        }

        events = self.plugin._parse_all_opensprinkler_programs(payload)

        self.assertEqual(events, [("OS Front", 8700, 9960, True)])

    def test_all_rainmachine_programs_use_configured_durations(self):
        rm = device(
            2,
            "RainMachine",
            {},
            props={"ip_address": "controller"},
        )
        programs = [
            {
                "name": "Garden",
                "active": False,
                "startTime": "06:30",
                "cycles": 3,
                "soak": 120,
                "cs_on": True,
                "wateringTimes": [
                    {"active": True, "duration": 600},
                    {"active": True, "duration": 300},
                    {"active": False, "duration": 999},
                ],
            }
        ]
        with patch.object(
            self.plugin,
            "_rainmachine_program_payload",
            return_value=programs,
        ):
            events = self.plugin._all_rainmachine_programs(rm)

        self.assertEqual(events, [("RM Garden", 23400, 24540, False)])

    def test_rainmachine_endpoint_matches_rainmachine2_local_login(self):
        base, context = self.plugin._rainmachine_local_endpoint(
            {
                "connectionType": "Local",
                "port": "8081",
                "https": False,
            },
            "192.0.2.10",
        )

        self.assertEqual(base, "https://192.0.2.10:8080/api/4")
        self.assertIsNotNone(context)

    def test_rainmachine_program_discovery_rejects_cloud_connection(self):
        with self.assertRaisesRegex(ValueError, "requires a local"):
            self.plugin._rainmachine_local_endpoint(
                {"connectionType": "Cloud"}, "controller"
            )

    def test_log_all_programmed_events_writes_sorted_complete_list(self):
        monitor = self.make_monitor()
        self.plugin.logger = Mock()
        with patch.object(
            self.plugin,
            "_all_programmed_events",
            return_value=(
                [
                    ("OS Early", 3600, 4200, True),
                    ("RM Late", 7200, 9000, False),
                ],
                [],
            ),
        ):
            self.plugin.logAllProgrammedEvents()

        messages = [call.args[0] for call in self.plugin.logger.info.call_args_list]
        self.assertEqual(messages[0], "All programmed irrigation events (2 total):")
        self.assertIn("01. OS Early | 01:00 | planned end 01:10", messages)
        self.assertIn(
            "02. RM Late | 02:00 | planned end 02:30 | DISABLED",
            messages,
        )

    def test_rainmachine_cycles_are_grouped_by_program(self):
        payload = {
            "waterLog": {
                "days": [
                    {
                        "date": "2026-08-17",
                        "programs": [
                            {
                                "id": 3,
                                "zones": [
                                    {
                                        "cycles": [
                                            {
                                                "startTime": "2026-08-17 06:00:00",
                                                "machineDuration": 600,
                                            },
                                            {
                                                "startTime": "2026-08-17 06:20:00",
                                                "machineDuration": 300,
                                            },
                                        ]
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        }

        events = self.plugin._parse_rainmachine_schedule(
            payload, date(2026, 8, 17), {3: "Garden"}
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].name, "RM Garden")
        self.assertEqual(events[0].start.strftime("%H:%M"), "06:00")
        self.assertEqual(events[0].end.strftime("%H:%M"), "06:25")

    def test_schedule_refresh_sorts_and_clears_reserved_states(self):
        monitor = self.make_monitor()
        early = plugin_module.PlannedEvent(
            "OpenSprinkler",
            "OS Early",
            datetime.fromisoformat("2026-08-17T06:00:00+02:00"),
            datetime.fromisoformat("2026-08-17T06:10:00+02:00"),
        )
        late = plugin_module.PlannedEvent(
            "RainMachine",
            "RM Late",
            datetime.fromisoformat("2026-08-17T20:00:00+02:00"),
            datetime.fromisoformat("2026-08-17T20:30:00+02:00"),
        )
        self.plugin.pluginPrefs.update(
            {
                "openSprinklerHost": "controller",
                "openSprinklerPassword": "password",
            }
        )
        with patch.object(
            self.plugin, "_opensprinkler_schedule", return_value=[early]
        ), patch.object(
            self.plugin, "_rainmachine_schedule", return_value=[late]
        ):
            monitor.pluginProps["rainMachineDevices"] = ["2"]
            indigo.devices.items[2] = device(2, "RainMachine", {})
            self.plugin._update_todays_schedule(
                monitor, date(2026, 8, 17)
            )

        changes = {
            change["key"]: change["value"]
            for change in monitor.updateStatesOnServer.call_args.args[0]
        }
        self.assertEqual(changes["plannedEventCount"], 2)
        self.assertEqual(
            changes["plannedEvent1"], "OS Early | 06:00 | 06:10"
        )
        self.assertEqual(
            changes["plannedEvent2"], "RM Late | 20:00 | 20:30"
        )
        self.assertEqual(changes["plannedEvent3"], "")

    def test_linktap_start_and_stop_are_written_with_duration_and_volume(self):
        lt = device(
            3,
            "Orchard",
            {
                "is_watering": False,
                "remain_duration": 0,
                "total_duration": 0,
                "is_rf_linked": True,
                "volume": 0,
                "is_cutoff": False,
            },
        )
        indigo.devices.items[lt.id] = lt
        monitor = self.make_monitor(linktap=(lt.id,))

        start_time = datetime.now().astimezone()
        stop_time = start_time + timedelta(seconds=73)
        lt.states.update(
            {
                "is_watering": True,
                "remain_duration": 120,
                "total_duration": 120,
            }
        )
        with patch.object(plugin_module, "_now", return_value=start_time):
            self.plugin._reconcile(monitor)

        lt.states.update(
            {
                "is_watering": False,
                "remain_duration": 0,
                "volume": 34.5,
                "is_cutoff": True,
            }
        )
        with patch.object(plugin_module, "_now", return_value=stop_time):
            self.plugin._reconcile(monitor)

        start, stop = self.records()
        self.assertEqual(start["event"], "start")
        self.assertEqual(start["zone"], "Orchard")
        self.assertEqual(stop["event"], "stop")
        self.assertEqual(stop["totalDurationSeconds"], 73)
        self.assertEqual(stop["volume"], 34.5)
        self.assertEqual(stop["faults"], ["is_cutoff"])
        self.assertNotIn("plannedDuration", start)
        self.assertNotIn("plannedDuration", stop)

    def test_rainmachine_zone_transition_stops_then_starts(self):
        rm = device(
            2,
            "RainMachine",
            {
                "active_watering": True,
                "current_zone": "Front",
                "minutes_left": 5,
                "device_online": True,
            },
        )
        indigo.devices.items[rm.id] = rm
        monitor = self.make_monitor(rainmachine=(rm.id,))

        rm.states["current_zone"] = "Back"
        self.plugin._reconcile(monitor)

        records = self.records()
        self.assertEqual(
            [(record["event"], record["zone"]) for record in records],
            [
                ("start", "RM Front"),
                ("stop", "RM Front"),
                ("start", "RM Back"),
            ],
        )
        self.assertEqual(records[-1]["sourceKey"], "rainmachine:2:Back")

    def test_rainmachine_does_not_duplicate_existing_rm_prefix(self):
        rm = device(
            2,
            "RainMachine",
            {
                "active_watering": True,
                "current_zone": "RM Pool Refill",
                "minutes_left": 5,
                "device_online": True,
            },
        )

        snapshot = self.plugin._rainmachine_snapshot(rm)

        self.assertEqual(snapshot.zone_name, "RM Pool Refill")
        self.assertEqual(
            snapshot.source_key,
            "rainmachine:2:RM Pool Refill",
        )

    def test_unavailable_source_does_not_falsely_end_active_session(self):
        lt = device(
            3,
            "Orchard",
            {
                "is_watering": True,
                "remain_duration": 60,
                "total_duration": 60,
                "is_rf_linked": True,
            },
        )
        indigo.devices.items[lt.id] = lt
        monitor = self.make_monitor(linktap=(lt.id,))

        lt.states["is_rf_linked"] = False
        lt.states["is_watering"] = False
        self.plugin._reconcile(monitor)

        self.assertEqual(
            [record["event"] for record in self.records()], ["start"]
        )
        self.assertTrue(self.plugin._sessions)
        monitor.setErrorStateOnServer.assert_called()

    def test_open_session_is_recovered_after_restart(self):
        lt = device(
            3,
            "Orchard",
            {
                "is_watering": True,
                "remain_duration": 60,
                "total_duration": 60,
                "is_rf_linked": True,
            },
        )
        indigo.devices.items[lt.id] = lt
        self.make_monitor(linktap=(lt.id,))
        self.assertEqual(len(self.records()), 1)

        restarted = plugin_module.Plugin(
            "plugin.id", "Irrigation Monitor", "0.1.10", {}
        )
        restarted.startup()
        self.assertEqual(len(restarted._sessions), 1)

    def test_removing_active_source_closes_its_session(self):
        lt = device(
            3,
            "Orchard",
            {
                "is_watering": True,
                "remain_duration": 60,
                "total_duration": 60,
                "is_rf_linked": True,
            },
        )
        indigo.devices.items[lt.id] = lt
        monitor = self.make_monitor(linktap=(lt.id,))

        monitor.pluginProps["linkTapDevices"] = []
        self.plugin._reconcile(monitor)

        records = self.records()
        self.assertEqual([record["event"] for record in records], ["start", "stop"])
        self.assertEqual(records[-1]["reason"], "sourceRemoved")
        self.assertFalse(self.plugin._sessions)

    def test_recent_runs_are_newest_first_and_limited_to_ten(self):
        history = plugin_module.JsonlHistory(self.history_path)
        for index in range(12):
            history.append(
                {
                    "event": "stop",
                    "time": f"2026-07-25T14:{index:02d}:00+02:00",
                    "zone": f"Zone {index}",
                    "totalDurationSeconds": index,
                }
            )

        recent = history.recent_runs()

        self.assertEqual(len(recent), 10)
        self.assertEqual(recent[0]["zone"], "Zone 11")
        self.assertEqual(recent[-1]["zone"], "Zone 2")

    def test_recent_run_format_omits_volume_and_includes_fault(self):
        formatted = self.plugin._format_run(
            {
                "time": "2026-07-25T14:52:34+02:00",
                "zone": "LinkTap Salad",
                "totalDurationSeconds": 73,
                "volume": 3.24,
                "faults": ["is_cutoff"],
            }
        )

        self.assertEqual(
            formatted,
            "25/07 14:52 | "
            + "LinkTap Salad"
            + plugin_module.NON_BREAKING_SPACE * 10
            + " | 00:01:13 | fault is_cutoff",
        )

    def test_time_display_formatting(self):
        self.assertEqual(
            self.plugin._format_timestamp(
                "2026-07-25T16:00:21+02:00"
            ),
            "25/07 16:00",
        )
        self.assertEqual(
            self.plugin._format_duration(23 * 3600 + 10 * 60),
            "23:10:00",
        )

    def test_time_since_last_watering_uses_latest_stop(self):
        monitor = self.make_monitor()
        monitor.states["timeSinceLastWatering"] = ""
        plugin_module.JsonlHistory(self.history_path).append(
            {
                "event": "stop",
                "time": "2026-07-25T14:56:36+02:00",
                "zone": "RM Pool Refill",
                "totalDurationSeconds": 61,
            }
        )

        with patch.object(
            plugin_module,
            "_now",
            return_value=datetime.fromisoformat(
                "2026-07-26T16:56:36+02:00"
            ),
        ):
            self.plugin._update_time_since_last_watering(monitor)

        monitor.updateStateOnServer.assert_called_with(
            "timeSinceLastWatering",
            value="26:00",
            triggerEvents=False,
        )

    def test_time_since_last_watering_reports_never_without_stop(self):
        self.assertEqual(
            self.plugin._format_time_since_stop(None),
            "Never",
        )

    def test_time_since_last_watering_is_zero_while_watering(self):
        self.assertEqual(
            self.plugin._format_time_since_stop(
                None,
                watering=True,
            ),
            "00:00",
        )

    def test_time_since_last_watering_discards_seconds(self):
        self.assertEqual(
            self.plugin._format_time_since_stop(
                {"time": "2026-07-27T14:00:00+02:00"},
                now=datetime.fromisoformat(
                    "2026-07-27T14:02:45+02:00"
                ),
            ),
            "00:02",
        )

    def test_new_state_is_skipped_until_device_definition_refreshes(self):
        monitor = self.make_monitor()
        changes = self.plugin._history_state_changes(monitor)

        self.assertNotIn(
            "timeSinceLastWatering",
            {change["key"] for change in changes},
        )

    def test_boolean_and_selection_normalization(self):
        self.assertTrue(plugin_module._as_bool("on"))
        self.assertFalse(plugin_module._as_bool("false"))
        self.assertEqual(
            plugin_module._selected_ids("2, 3;2,invalid"), [2, 3]
        )


if __name__ == "__main__":
    unittest.main()
