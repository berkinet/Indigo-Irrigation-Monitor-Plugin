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
        self.assertEqual(info["PluginVersion"], "0.1.2")

    def test_devices_xml_is_well_formed(self):
        root = ET.parse(
            BUNDLE / "Contents" / "Server Plugin" / "Devices.xml"
        ).getroot()
        state_ids = {
            state.attrib["id"] for state in root.findall(".//State")
        }
        self.assertIn("onOffState", state_ids)
        self.assertIn("lastEvent", state_ids)
        self.assertTrue(
            {f"recentRun{index}" for index in range(1, 11)}.issubset(
                state_ids
            )
        )


if __name__ == "__main__":
    unittest.main()
