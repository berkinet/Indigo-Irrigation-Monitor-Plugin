import plistlib
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "Irrigation Monitor.indigoPlugin"


class BundleTests(unittest.TestCase):
    def test_info_plist_targets_python_313_indigo(self):
        with (BUNDLE / "Contents" / "Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        self.assertEqual(info["ServerApiVersion"], "3.8")
        self.assertEqual(info["PluginVersion"], "0.3.2")

    def test_devices_xml_is_well_formed(self):
        root = ET.parse(
            BUNDLE / "Contents" / "Server Plugin" / "Devices.xml"
        ).getroot()
        state_ids = {
            state.attrib["id"] for state in root.findall(".//State")
        }
        self.assertIn("onOffState", state_ids)
        self.assertIn("lastEvent", state_ids)
        self.assertIn("timeSinceLastWatering", state_ids)
        self.assertTrue(
            {f"recentRun{index}" for index in range(1, 11)}.issubset(
                state_ids
            )
        )
        self.assertTrue(
            {f"plannedEvent{index}" for index in range(1, 65)}.issubset(
                state_ids
            )
        )

    def test_schedule_menu_is_well_formed(self):
        root = ET.parse(
            BUNDLE / "Contents" / "Server Plugin" / "MenuItems.xml"
        ).getroot()
        callbacks = {
            item.findtext("CallbackMethod")
            for item in root.findall(".//MenuItem")
        }
        self.assertEqual(
            callbacks, {"updateTodaysSchedule", "logAllProgrammedEvents"}
        )

    def test_plugin_configuration_contains_secure_os_password(self):
        root = ET.parse(
            BUNDLE / "Contents" / "Server Plugin" / "PluginConfig.xml"
        ).getroot()
        fields = {
            field.attrib["id"]: field.attrib
            for field in root.findall(".//Field")
        }
        self.assertIn("openSprinklerHost", fields)
        self.assertEqual(
            fields["openSprinklerPassword"].get("secure"), "true"
        )
        self.assertNotIn("hidden", fields["openSprinklerPassword"])


if __name__ == "__main__":
    unittest.main()
