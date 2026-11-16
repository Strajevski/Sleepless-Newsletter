import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


class FinalScheduleTests(unittest.TestCase):
    def test_intake_clock_checks_configuration_every_minute(self):
        path = Path(__file__).resolve().parents[1] / "config/final-build-task.xml"
        root = ET.parse(path).getroot()
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        triggers = root.findall("t:Triggers/t:TimeTrigger", ns)
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0].findtext("t:Repetition/t:Interval", namespaces=ns), "PT1M")
        self.assertIsNone(triggers[0].find("t:Repetition/t:Duration", ns))
        self.assertIn("schedule.py\" --run-intake", root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns))


if __name__ == "__main__":
    unittest.main()
