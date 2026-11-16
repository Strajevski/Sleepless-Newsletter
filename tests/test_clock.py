import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from circus_newsletter.clock import event_at, intake_due, load_schedule, validate
from circus_newsletter.delivery import _settings, due_at, send_campaign, send_due


PROJECT = Path(__file__).resolve().parents[1]


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.schedule = json.loads((PROJECT / "config/schedule.json").read_text())

    def root(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "config").mkdir()
        (root / "config/mail.json").write_text(json.dumps({"account": "newsletter@example.com", "host": "imap.gmail.com", "port": 993, "mailbox": "INBOX"}), encoding="utf-8")
        self.save(root)
        (root / "config/roster.json").write_text('{"members": {}}')
        (root / "config/delivery.json").write_text(json.dumps({"auto_send_fallback": True}))
        return root

    def save(self, root):
        (root / "config/schedule.json").write_text(json.dumps(self.schedule))

    def test_default_schedule_preserves_live_times(self):
        validate(self.schedule)
        expected = {"invitation": (28, 8), "reminder": (3, 17), "cutoff": (5, 3),
                    "media": (5, 4), "retry": (5, 7), "issue_send": (5, 8)}
        for event, (day, hour) in expected.items():
            month = 9 if event == "invitation" else 10
            self.assertEqual(event_at("2026-W40", event, self.schedule),
                             datetime(2026, month, day, hour, tzinfo=timezone.utc))

    def test_thursday_cutoff_friday_delivery_and_saturday_invitation(self):
        self.schedule.update(cutoff_weekday="Thursday", retry_day_offset=1,
                             issue_send_day_offset=1, invitation_day_offset=-5,
                             invitation_time="09:00")
        validate(self.schedule)
        root = self.root()
        friday = datetime(2026, 10, 9, 8, tzinfo=timezone.utc)
        with patch("circus_newsletter.delivery.send_campaign", return_value="sent") as send:
            self.assertEqual(send_due(root, friday), [("2026-W40", "issue", "sent")])
            self.assertEqual(send.call_args.args[2], "issue")
            self.assertEqual(send_due(root, friday + timedelta(days=1, hours=1)),
                             [("2026-W41", "invitation", "sent")])

    def test_manual_edit_is_read_on_next_check_and_overrides_legacy_values(self):
        root = self.root()
        (root / "config/delivery.json").write_text(json.dumps({
            "auto_send_fallback": True, "issue_send_time_utc": "01:00",
            "catch_up_minutes": 999}))
        self.assertEqual(due_at("2026-W40", "issue", _settings(root)).hour, 8)
        self.schedule.update(issue_send_time="09:15")
        self.save(root)
        settings = _settings(root)
        self.assertEqual(settings["catch_up_minutes"], 120)
        self.assertEqual(due_at("2026-W40", "issue", settings).hour, 9)
        self.assertEqual(due_at("2026-W40", "issue", settings).minute, 15)

    def test_invalid_order_or_values_fail_before_sending(self):
        for key, value in (("issue_send_time", "06:00"), ("retry_time", "02:00"),
                           ("reminder_day_offset", 1), ("media_time", "25:00"),
                           ("invitation_time", "05:00"), ("intake_interval_minutes", 0),
                           ("delivery_retry_limit", True), ("media_timezone", "bad-zone")):
            with self.subTest(key=key):
                original = self.schedule[key]
                self.schedule[key] = value
                root = self.root()
                with patch("circus_newsletter.delivery.send_campaign") as send:
                    with self.assertRaises((ValueError, KeyError)):
                        send_due(root, datetime(2026, 10, 5, 8, tzinfo=timezone.utc))
                    send.assert_not_called()
                self.schedule[key] = original

    def test_utc_and_london_clock_choices_across_dst(self):
        for iso_week in (40, 44):
            issue = f"2026-W{iso_week}"
            self.assertEqual(event_at(issue, "issue_send", self.schedule).hour, 8)
        for key in ("cutoff_timezone", "retry_timezone", "media_timezone",
                    "issue_send_timezone", "reminder_timezone", "invitation_timezone"):
            self.schedule[key] = "Europe/London"
        validate(self.schedule)
        self.assertEqual(event_at("2026-W40", "issue_send", self.schedule).hour, 7)
        self.assertEqual(event_at("2026-W44", "issue_send", self.schedule).hour, 8)

    def test_catch_up_crosses_midnight_and_iso_year(self):
        self.schedule.update(issue_send_time="23:30", invitation_day_offset=-6,
                             invitation_time="01:00")
        validate(self.schedule)
        root = self.root()
        due = event_at("2026-W53", "issue_send", self.schedule)
        self.assertEqual(due.year, 2027)
        with patch("circus_newsletter.delivery.send_campaign", return_value="sent"):
            outcomes = send_due(root, due + timedelta(minutes=60))
        self.assertIn(("2026-W53", "issue", "sent"), outcomes)
        self.assertFalse(any(kind == "invitation" for _, kind, _ in outcomes))

    def test_stage_tick_runs_off_interval_and_changes_weekday(self):
        self.schedule.update(cutoff_weekday="Thursday", retry_day_offset=0,
                             cutoff_time="03:07", media_time="04:11", retry_time="07:13")
        validate(self.schedule)
        for event in ("cutoff", "media", "retry"):
            due = event_at("2026-W40", event, self.schedule)
            self.assertTrue(intake_due(due + timedelta(seconds=40), self.schedule))
            self.assertFalse(intake_due(due + timedelta(minutes=1), self.schedule))
        self.assertTrue(intake_due(datetime(2026, 10, 8, 10, 30, tzinfo=timezone.utc), self.schedule))

    def test_new_schedule_requires_explicit_fields(self):
        root = self.root()
        del self.schedule["retry_time"]
        self.save(root)
        with self.assertRaisesRegex(ValueError, "Missing retry_time"):
            load_schedule(root)

    def test_separate_invitation_waits_for_complete_previous_delivery(self):
        self.schedule.update(invitation_day_offset=-6, invitation_time="09:00")
        root = self.root()
        (root / "config/roster.json").write_text(json.dumps({"members": {
            "reader": {"name": "Example", "emails": ["reader@example.invalid"]}}}))
        due = event_at("2026-W41", "invitation", self.schedule)
        with patch("circus_newsletter.delivery.read_password", return_value="fake"), \
             patch("circus_newsletter.discord.dispatch_pending", return_value=0), \
             patch("circus_newsletter.delivery.smtplib.SMTP_SSL") as smtp:
            # Pass the factory explicitly: send_campaign binds its default at import.
            smtp.return_value.sendmail.return_value = {}
            self.assertEqual(send_campaign(root, "2026-W41", "invitation", due,
                                           smtp_factory=smtp), "not_ready")
            smtp.assert_not_called()
            journal = root / "issues/2026-W40/outbound/issue/delivery.json"
            journal.parent.mkdir(parents=True)
            journal.write_text(json.dumps({"status": {"one": "sent", "two": "uncertain"}}))
            self.assertEqual(send_campaign(root, "2026-W41", "invitation", due,
                                           smtp_factory=smtp), "not_ready")
            smtp.assert_not_called()
            journal.write_text(json.dumps({"status": {"one": "sent", "two": "sent"}}))
            self.assertEqual(send_campaign(root, "2026-W41", "invitation", due,
                                           smtp_factory=smtp), "sent")
            smtp.return_value.sendmail.assert_called_once()
            raw = smtp.return_value.sendmail.call_args.args[2]
            self.assertIn(b"Monday at 08:00 UTC", raw)


if __name__ == "__main__":
    unittest.main()
