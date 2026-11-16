"""Exercise a proposed Monday cutoff without changing the live schedule."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from circus_newsletter.core import _issue_date, finalize_due_issues, issue_for
from circus_newsletter.delivery import _message, due_at, send_due
from circus_newsletter.render import next_deadline
from circus_newsletter.weekly import latest_closed_issue, process_issue, retry_time


class MondayCutoffTests(unittest.TestCase):
    def test_staged_schedule_has_four_hour_retry_and_one_hour_to_send(self):
        project = Path(__file__).resolve().parents[1]
        schedule = json.loads((project / "config/schedule.json").read_text(encoding="utf-8"))
        delivery = schedule
        cutoff = datetime(2026, 10, 5, 3, tzinfo=timezone.utc)
        retry = retry_time("2026-W40", schedule)
        send = due_at("2026-W40", "issue", delivery)
        self.assertEqual(issue_for(cutoff, schedule)[0], "2026-W41")
        self.assertEqual(issue_for(cutoff - timedelta(minutes=1), schedule)[0], "2026-W40")
        self.assertEqual((retry - cutoff).total_seconds(), 4 * 3600)
        self.assertEqual((send - retry).total_seconds(), 3600)
        self.assertEqual(due_at("2026-W41", "invitation", delivery), send)
        self.assertEqual(delivery["reminder_time"], "17:00")

    def schedule(self, zone):
        return {
            "timezone": "Europe/London", "cutoff_timezone": zone,
            "cutoff_weekday": "Monday", "cutoff_time": "05:00",
            "retry_weekday": "Monday", "retry_timezone": zone,
            "retry_time": "06:00", "first_issue_week": "2026-W40",
            "first_issue_number": 2,
        }

    def test_week_label_stays_with_previous_sunday_across_cutoff(self):
        for zone, before, at in (
            ("UTC", datetime(2026, 10, 5, 4, 59, tzinfo=timezone.utc),
             datetime(2026, 10, 5, 5, tzinfo=timezone.utc)),
            ("Europe/London", datetime(2026, 10, 5, 3, 59, tzinfo=timezone.utc),
             datetime(2026, 10, 5, 4, tzinfo=timezone.utc)),
        ):
            with self.subTest(zone=zone):
                schedule = self.schedule(zone)
                self.assertEqual(issue_for(before, schedule)[0], "2026-W40")
                self.assertEqual(issue_for(at, schedule)[0], "2026-W41")
                self.assertEqual(_issue_date("2026-W40", schedule).isoformat(), "2026-10-05")
                self.assertIn("Monday 12 October 2026, 05:00", next_deadline("2026-W40", schedule))
                self.assertEqual(due_at("2026-W40", "issue", {
                    "issue_send_time_utc": "07:00", "reminder_time_utc": "17:00"
                }), datetime(2026, 10, 5, 7, tzinfo=timezone.utc))
                self.assertLess(retry_time("2026-W40", schedule),
                                datetime(2026, 10, 5, 7, tzinfo=timezone.utc))

    def test_default_retry_is_rejected_when_it_precedes_new_cutoff(self):
        schedule = self.schedule("UTC")
        del schedule["retry_time"]
        with self.assertRaisesRegex(ValueError, "retry must be after"):
            retry_time("2026-W40", schedule)

    def test_reminder_and_invitation_quote_monday_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config/mail.json").write_text(json.dumps({"account": "newsletter@example.com", "host": "imap.gmail.com", "port": 993, "mailbox": "INBOX"}), encoding="utf-8")
            (root / "config/roster.json").write_text(json.dumps({"members": {
                "alex": {"name": "Alex", "emails": ["alex@example.com"]}
            }}), encoding="utf-8")
            (root / "config/delivery.json").write_text(json.dumps({
                "issue_send_time_utc": "07:00", "reminder_time_utc": "17:00",
                "catch_up_minutes": 120, "auto_send_fallback": True,
            }), encoding="utf-8")
            for zone, expected in (("UTC", "Monday 05 October 2026 at 05:00 GMT (UTC)"),
                                   ("Europe/London", "Monday 05 October 2026 at 05:00 BST")):
                with self.subTest(zone=zone):
                    (root / "config/schedule.json").write_text(
                        json.dumps(self.schedule(zone)), encoding="utf-8")
                    mail = _message(root, "2026-W40", "reminder", "alex", "alex@example.com",
                                    datetime(2026, 10, 3, 17, tzinfo=timezone.utc), None,
                                    {"members": {"alex": {"name": "Alex"}}})
                    self.assertIn(expected, BytesParser(policy=policy.default).parsebytes(mail).get_content())
                    invitation = _message(root, "2026-W41", "invitation", "alex",
                                          "alex@example.com",
                                          datetime(2026, 10, 5, 7, tzinfo=timezone.utc), None,
                                          {"members": {"alex": {"name": "Alex"}}})
                    self.assertIn("Monday, 12 October 2026 at 05:00",
                                  BytesParser(policy=policy.default).parsebytes(invitation).get_content())

    def test_finalization_and_latest_issue_use_sunday_week_number(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config/mail.json").write_text(json.dumps({"account": "newsletter@example.com", "host": "imap.gmail.com", "port": 993, "mailbox": "INBOX"}), encoding="utf-8")
            (root / "config/schedule.json").write_text(
                json.dumps(self.schedule("UTC")), encoding="utf-8")
            (root / "config/roster.json").write_text(
                json.dumps({"members": {}}), encoding="utf-8")
            before = datetime(2026, 10, 5, 4, 59, tzinfo=timezone.utc)
            at = datetime(2026, 10, 5, 5, tzinfo=timezone.utc)
            self.assertIsNone(latest_closed_issue(root, before))
            self.assertEqual(finalize_due_issues(root, before), [])
            self.assertEqual(latest_closed_issue(root, at), "2026-W40")
            finalized = finalize_due_issues(root, at)
            self.assertEqual(len(finalized), 1)
            self.assertEqual(finalized[0].parent.name, "2026-W40")
            self.assertIn("2026-10-05T05:00:00+00:00",
                          finalized[0].read_text(encoding="utf-8"))

    def test_early_monday_delivery_check_ignores_previous_week(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config/mail.json").write_text(json.dumps({"account": "newsletter@example.com", "host": "imap.gmail.com", "port": 993, "mailbox": "INBOX"}), encoding="utf-8")
            (root / "config/schedule.json").write_text(
                json.dumps(self.schedule("UTC")), encoding="utf-8")
            (root / "config/roster.json").write_text(
                json.dumps({"members": {}}), encoding="utf-8")
            (root / "config/delivery.json").write_text(json.dumps({
                "issue_send_time_utc": "07:00", "reminder_time_utc": "17:00",
                "catch_up_minutes": 120, "auto_send_fallback": True,
            }), encoding="utf-8")
            with patch("circus_newsletter.delivery.send_campaign", return_value="not_due") as send:
                self.assertEqual(send_due(root, datetime(2026, 10, 5, 4, tzinfo=timezone.utc)), [])
                send.assert_not_called()
                self.assertEqual(send_due(root, datetime(2026, 10, 5, 5, tzinfo=timezone.utc)), [])
                send.assert_not_called()  # A closed issue still waits for its send time.

    def test_limit_failure_defers_until_new_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config/mail.json").write_text(json.dumps({"account": "newsletter@example.com", "host": "imap.gmail.com", "port": 993, "mailbox": "INBOX"}), encoding="utf-8")
            (root / "config/schedule.json").write_text(
                json.dumps(self.schedule("UTC")), encoding="utf-8")
            (root / "config/roster.json").write_text(
                json.dumps({"members": {}}), encoding="utf-8")
            (root / "config/delivery.json").write_text(json.dumps({
                "issue_send_time_utc": "07:00", "reminder_time_utc": "17:00",
            }), encoding="utf-8")
            final = root / "issues/2026-W40/issue-input-final.md"
            final.parent.mkdir(parents=True)
            final.write_text("frozen", encoding="utf-8")
            blocked = SimpleNamespace(allowed=False, weekly_used=91, five_hour_used=20)
            with patch("circus_newsletter.weekly.submissions", return_value=([{"submission_id": "alex-001"}], "hash")), \
                 patch("circus_newsletter.weekly.read_codex_budget", return_value=blocked), \
                 patch("circus_newsletter.weekly.fallback", return_value={}), \
                 patch("circus_newsletter.weekly.publish_draft") as publish, \
                 patch("circus_newsletter.weekly._notify"):
                self.assertEqual(process_issue(root, "2026-W40",
                    datetime(2026, 10, 5, 5, tzinfo=timezone.utc)), "deferred")
                self.assertEqual(process_issue(root, "2026-W40",
                    datetime(2026, 10, 5, 5, 30, tzinfo=timezone.utc)), "deferred")
                publish.assert_not_called()
                self.assertEqual(process_issue(root, "2026-W40",
                    datetime(2026, 10, 5, 6, tzinfo=timezone.utc)), "fallback")
                publish.assert_called_once()


if __name__ == "__main__":
    unittest.main()
