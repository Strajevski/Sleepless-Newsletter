import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from circus_newsletter.render import issue_label, issue_number, next_deadline, render_html, render_text
from circus_newsletter.settings import load_settings


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        # Test fixed fixtures, never the user's configurable identity or schedule.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = load_settings(Path(temporary.name))
        self.schedule = {
            "timezone": "Europe/London", "cutoff_timezone": "UTC",
            "cutoff_weekday": "Monday", "cutoff_time": "03:00",
            "first_issue_week": "2026-W40", "first_issue_number": 2,
        }
        guard = patch("circus_newsletter.render.load_settings",
                      side_effect=AssertionError("Render tests must not read installation settings"))
        guard.start()
        self.addCleanup(guard.stop)
        self.issue = json.loads((self.project / "templates/preview-data.json").read_text(encoding="utf-8"))
        self.template = self.project / "templates/issue-email.html"

    def test_issue_number_and_utc_next_deadline(self):
        self.assertEqual(issue_number("2026-W40", self.schedule), 2)
        self.assertEqual(issue_number("2026-W41", self.schedule), 3)
        self.assertEqual(issue_label("2026-W41", self.schedule), "№3")
        self.assertIn("Monday 12 October 2026, 03:00 UTC", next_deadline("2026-W40", self.schedule))

    def test_html_sections_escaping_and_plain_text(self):
        self.issue["title"] = "A <careful> & clear title"
        page = render_html(self.issue, self.schedule, self.template, preview=True, settings=self.settings)
        for heading in ("THE LEAD", "WORTH YOUR TIME", "PERSONAL UPDATES", "GALLERY", "SLEEPLESS’S TAKE", "THE LEDGER"):
            self.assertIn(heading, page)
        self.assertLess(page.index("GALLERY"), page.index("SLEEPLESS’S TAKE"))
        self.assertLess(page.index("SLEEPLESS’S TAKE"), page.index("THE LEDGER"))
        self.assertIn("CIRCUS NEWSLETTER", page)
        self.assertIn("Circus Newsletter №2:", page)
        self.assertIn("№2", page)
        self.assertNotIn("No. 2", page)
        self.assertIn("A &lt;careful&gt; &amp; clear title", page)
        self.assertNotIn("A <careful> & clear title", page)
        self.assertIn("max-width:960px", page)
        self.assertIn("overflow-wrap:anywhere", page)
        self.assertIn("CIRCUS NEWSLETTER | №2", render_text(self.issue, self.schedule, settings=self.settings))
        self.assertIn("SLEEPLESS’S TAKE", render_text(self.issue, self.schedule, settings=self.settings))

    def test_sleepless_take_is_required(self):
        self.issue["sleepless_take"] = "  "
        with self.assertRaisesRegex(ValueError, "Sleepless’s Take is empty"):
            render_html(self.issue, self.schedule, self.template, preview=True, settings=self.settings)

    def test_gallery_requires_real_image_for_non_preview_render(self):
        with self.assertRaisesRegex(ValueError, "image_src"):
            render_html(self.issue, self.schedule, self.template, settings=self.settings)

    def test_unsafe_url_is_rejected(self):
        self.issue["worth_your_time"][0]["url"] = "javascript:alert(1)"
        with self.assertRaisesRegex(ValueError, "Invalid issue link"):
            render_html(self.issue, self.schedule, self.template, preview=True, settings=self.settings)


if __name__ == "__main__":
    unittest.main()
