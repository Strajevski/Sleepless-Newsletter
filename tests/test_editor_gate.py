import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from circus_newsletter.editor_gate import next_monday_five, parse_budget
from scripts.editor_preflight import DEFER_EXIT_CODE, STOP_EXIT_CODE, run


def response(weekly: float, five_hour: float) -> dict:
    return {"rateLimitsByLimitId": {"codex": {
        "rateLimitReachedType": None,
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "primary": {"windowDurationMins": 300, "usedPercent": five_hour,
                    "resetsAt": 1790676299},
        "secondary": {"windowDurationMins": 10080, "usedPercent": weekly,
                      "resetsAt": 1791171040},
    }}}


class EditorGateTests(unittest.TestCase):
    def test_strict_thresholds_and_missing_window(self):
        self.assertTrue(parse_budget(response(89.9, 79.9)).allowed)
        self.assertFalse(parse_budget(response(90, 79)).allowed)
        self.assertFalse(parse_budget(response(89, 80)).allowed)
        broken = response(20, 20)
        broken["rateLimitsByLimitId"]["codex"]["secondary"] = None
        with self.assertRaises(ValueError):
            parse_budget(broken)

    def test_monday_five_is_uk_local_across_clock_change(self):
        bst = next_monday_five(datetime(2026, 10, 4, 23, 5, tzinfo=timezone.utc))
        self.assertEqual(bst.isoformat(), "2026-10-05T05:00:00+01:00")
        gmt = next_monday_five(datetime(2026, 11, 1, 23, 5, tzinfo=timezone.utc))
        self.assertEqual(gmt.isoformat(), "2026-11-02T05:00:00+00:00")

    def test_defer_then_retry_and_limit_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            final = root / "issues/2026-W40/issue-input-final.md"
            final.parent.mkdir(parents=True)
            final.write_text("final", encoding="utf-8")
            with patch("scripts.editor_preflight.read_codex_budget",
                       return_value=parse_budget(response(91, 30))):
                with patch("scripts.editor_preflight.subprocess.run") as editor:
                    self.assertEqual(run(root, "2026-W40", False, ["editor"]), DEFER_EXIT_CODE)
                    editor.assert_not_called()
                with patch("scripts.editor_preflight.alert") as alert:
                    self.assertEqual(run(root, "2026-W40", True, ["editor"]), STOP_EXIT_CODE)
                    alert.assert_called_once()
            with patch("scripts.editor_preflight.read_codex_budget",
                       return_value=parse_budget(response(10, 10))):
                with patch("scripts.editor_preflight.subprocess.run",
                           return_value=SimpleNamespace(returncode=75)):
                    with patch("scripts.editor_preflight.alert") as alert:
                        self.assertEqual(run(root, "2026-W40", True, ["editor"]), 75)
                        alert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
