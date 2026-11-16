import hashlib
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from circus_newsletter.delivery import due_at, recipients, send_campaign, send_due
from circus_newsletter.discord import _events


ISSUE = "2026-W40"


class FakeSMTP:
    def __init__(self, fail_on=0):
        self.sent = []
        self.fail_on = fail_on

    def login(self, account, password):
        assert account == "newsletter@example.com"
        assert password == "dummy-local-password"

    def sendmail(self, sender, to_addrs, raw):
        self.sent.append((sender, to_addrs, raw))
        if len(self.sent) == self.fail_on:
            raise TimeoutError("simulated loss after SMTP handoff")
        return {}

    def quit(self):
        pass


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / "config/mail.json").write_text(json.dumps({"account": "newsletter@example.com", "host": "imap.gmail.com", "port": 993, "mailbox": "INBOX"}), encoding="utf-8")
        (self.root / "config/roster.json").write_text(json.dumps({"members": {
            "rowan": {"name": "Rowan", "emails": ["rowan@example.com"], "pronouns": "he/him"},
            "morgan": {"name": "Morgan", "emails": ["morgan@example.com"], "pronouns": "he/him"}
        }}), encoding="utf-8")
        (self.root / "config/schedule.json").write_text(json.dumps({
            "timezone": "Europe/London", "cutoff_timezone": "UTC", "cutoff_weekday": "Sunday",
            "cutoff_time": "23:00", "first_issue_week": ISSUE, "first_issue_number": 2
        }), encoding="utf-8")
        (self.root / "config/delivery.json").write_text(json.dumps({
            "issue_send_time_utc": "07:00", "reminder_time_utc": "17:00",
            "catch_up_minutes": 120, "auto_send_fallback": False
        }), encoding="utf-8")
        self.issue_dir = self.root / "issues" / ISSUE
        self.issue_dir.mkdir(parents=True)
        frozen = b"# Frozen dummy input\n- Included submissions: 1\n- Held for review: 0\n"
        (self.issue_dir / "issue-input-final.md").write_bytes(frozen)
        draft = EmailMessage()
        draft["From"] = "newsletter@example.com"
        draft["Subject"] = "Circus Newsletter №2: dummy"
        draft.set_content("Dummy issue body")
        draft.add_alternative("<p>Dummy issue body</p>", subtype="html")
        draft_bytes = draft.as_bytes()
        (self.issue_dir / "issue-draft.eml").write_bytes(draft_bytes)
        (self.issue_dir / "editor-result.json").write_text(json.dumps({
            "issue_week": ISSUE, "mode": "ai", "source_ids": ["rowan-001"],
            "final_input_sha256": hashlib.sha256(frozen).hexdigest(),
            "draft_sha256": hashlib.sha256(draft_bytes).hexdigest()
        }), encoding="utf-8")
        self.patches = [
            patch("circus_newsletter.delivery.read_password", return_value="dummy-local-password"),
            patch("circus_newsletter.discord.dispatch_pending", return_value=0)
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def send(self, kind, now, smtp, **kwargs):
        return send_campaign(self.root, ISSUE, kind, now, smtp_factory=lambda *a, **k: smtp, **kwargs)

    def test_issue_sends_privately_once_and_archives_one_message(self):
        smtp = FakeSMTP()
        when = datetime(2026, 10, 5, 7, tzinfo=timezone.utc)
        self.assertEqual(self.send("issue", when, smtp), "sent")
        self.assertEqual(len(smtp.sent), 1)
        for _sender, recipients_for_message, raw in smtp.sent:
            self.assertEqual(len(recipients_for_message), 2)
            mail = BytesParser(policy=policy.default).parsebytes(raw)
            self.assertEqual(str(mail["To"]), "newsletter@example.com")
            self.assertIsNone(mail["Cc"])
            self.assertIsNone(mail["Bcc"])
            self.assertIn("Dummy issue body", mail.get_body(preferencelist=("plain",)).get_content())
        journal = json.loads((self.issue_dir / "outbound/issue/delivery.json").read_text())
        self.assertEqual(set(journal["status"].values()), {"sent"})
        self.assertEqual(len(list((self.issue_dir / "outbound/issue").glob("*.eml"))), 1)
        self.assertIn("delivery-complete:issue:2026-W40", dict(_events(self.root)))
        self.assertEqual(self.send("issue", when + timedelta(hours=3), FakeSMTP()), "already_sent")

    def test_reminder_goes_to_everyone_before_cutoff(self):
        smtp = FakeSMTP()
        when = datetime(2026, 10, 3, 17, tzinfo=timezone.utc)
        self.assertEqual(self.send("reminder", when - timedelta(minutes=1), smtp), "not_due")
        self.assertEqual(self.send("reminder", when, smtp), "sent")
        self.assertEqual(len(smtp.sent), 1)
        for _sender, _to, raw in smtp.sent:
            mail = BytesParser(policy=policy.default).parsebytes(raw)
            self.assertIn("Circus Newsletter №2", str(mail["Subject"]))
            self.assertIn("If you have already submitted", mail.get_content())
            self.assertIn("Sunday 04 October 2026 at 23:00 GMT", mail.get_content())

    def test_monday_issue_is_followed_by_one_next_issue_invitation(self):
        smtp = FakeSMTP()
        when = datetime(2026, 10, 5, 7, tzinfo=timezone.utc)
        factory = lambda *a, **k: smtp
        self.assertEqual(send_due(self.root, when, smtp_factory=factory),
                         [("2026-W40", "issue", "sent"),
                          ("2026-W41", "invitation", "sent")])
        self.assertEqual(send_due(self.root, when + timedelta(minutes=30), smtp_factory=factory),
                         [("2026-W40", "issue", "already_sent"),
                          ("2026-W41", "invitation", "already_sent")])
        self.assertEqual(len(smtp.sent), 2)
        for _sender, _to, raw in smtp.sent[1:]:
            mail = BytesParser(policy=policy.default).parsebytes(raw)
            self.assertEqual(str(mail["Subject"]), "Circus Newsletter №3: submissions are open")
            body = mail.get_content().replace("\r\n", "\n")
            self.assertEqual(body,
                             "Hello Circus,\n\n"
                             "Submissions are open for Circus Newsletter №3.\n\n"
                             "Reply with whatever you've been noticing in your field or corner of the world. "
                             "You can also send personal updates and any photos or files you'd like included. "
                             "It can be as long or short as you like.\n\n"
                             "Send as many replies as you like this week; they all count toward your one submission\n\n"
                             "The submission window closes Sunday, 11 October 2026 at 23:00 UTC. "
                             "Issue №3 will follow Monday at 07:00 UTC.\n\n"
                             "Sleepless\n")
            self.assertNotIn("[date]", body)
            self.assertNotIn("[next number]", body)

    def test_invitation_waits_for_complete_issue_delivery(self):
        when = datetime(2026, 10, 5, 7, tzinfo=timezone.utc)
        self.assertEqual(send_campaign(self.root, "2026-W41", "invitation", when,
                                       smtp_factory=lambda *a, **k: FakeSMTP()), "not_ready")
        settings_path = self.root / "config/delivery.json"
        settings = json.loads(settings_path.read_text())
        settings["issue_holds"] = {ISSUE: "Awaiting review"}
        settings_path.write_text(json.dumps(settings))
        connect = lambda *a, **k: self.fail("SMTP must not connect for a held issue")
        self.assertEqual(send_due(self.root, when, smtp_factory=connect),
                         [(ISSUE, "issue", "held_for_review")])

    def test_fallback_requires_manual_approval(self):
        path = self.issue_dir / "editor-result.json"
        result = json.loads(path.read_text())
        result["mode"] = "fallback"
        path.write_text(json.dumps(result))
        when = datetime(2026, 10, 5, 7, tzinfo=timezone.utc)
        smtp = FakeSMTP()
        self.assertEqual(self.send("issue", when, smtp), "held_fallback")
        self.assertEqual(smtp.sent, [])
        self.assertEqual(self.send("issue", when, smtp, approve_fallback=True), "sent")

    def test_configured_fallback_sends_automatically_at_due_time(self):
        result_path = self.issue_dir / "editor-result.json"
        result = json.loads(result_path.read_text())
        result["mode"] = "fallback"
        result_path.write_text(json.dumps(result))
        settings_path = self.root / "config/delivery.json"
        settings = json.loads(settings_path.read_text())
        settings["auto_send_fallback"] = True
        settings_path.write_text(json.dumps(settings))
        due = datetime(2026, 10, 5, 7, tzinfo=timezone.utc)
        smtp = FakeSMTP()
        self.assertEqual(self.send("issue", due - timedelta(minutes=1), smtp), "not_due")
        self.assertEqual(self.send("issue", due, smtp), "sent")
        self.assertEqual(len(smtp.sent), 1)

    def test_oversized_issue_is_held_before_smtp(self):
        when = datetime(2026, 10, 5, 7, tzinfo=timezone.utc)
        smtp = FakeSMTP()
        with patch("circus_newsletter.delivery.MAX_ISSUE_MESSAGE_BYTES", 10):
            self.assertEqual(self.send("issue", when, smtp), "held_message_too_large")
        self.assertEqual(smtp.sent, [])
        self.assertIn("delivery-alert:issue-size:2026-W40", dict(_events(self.root)))

    def test_first_issue_hold_does_not_block_reminders(self):
        path = self.root / "config/delivery.json"
        settings = json.loads(path.read_text())
        settings["issue_holds"] = {ISSUE: "Awaiting editor test"}
        path.write_text(json.dumps(settings))
        smtp = FakeSMTP()
        self.assertEqual(self.send("issue", datetime(2026, 10, 5, 7, tzinfo=timezone.utc), smtp),
                         "held_for_review")
        self.assertEqual(smtp.sent, [])
        self.assertEqual(self.send("reminder", datetime(2026, 10, 3, 17, tzinfo=timezone.utc), smtp),
                         "sent")
        self.assertEqual(len(smtp.sent), 1)

    def test_uncertain_smtp_result_stops_automatic_retry(self):
        when = datetime(2026, 10, 5, 7, tzinfo=timezone.utc)
        with patch("circus_newsletter.delivery._alert"):
            self.assertEqual(self.send("issue", when, FakeSMTP(fail_on=1)), "needs_review")
            retry = FakeSMTP()
            self.assertEqual(self.send("issue", when + timedelta(minutes=30), retry), "needs_review")
        self.assertEqual(retry.sent, [])
        journal = json.loads((self.issue_dir / "outbound/issue/delivery.json").read_text())
        self.assertEqual(list(journal["status"].values()), ["uncertain", "uncertain"])

    def test_connection_failure_can_retry_but_changed_draft_cannot_send(self):
        when = datetime(2026, 10, 5, 7, tzinfo=timezone.utc)
        with patch("circus_newsletter.delivery._alert"):
            self.assertEqual(send_campaign(self.root, ISSUE, "issue", when,
                                           smtp_factory=lambda *a, **k: (_ for _ in ()).throw(OSError())),
                             "connection_failed")
        journal = json.loads((self.issue_dir / "outbound/issue/delivery.json").read_text())
        self.assertEqual(set(journal["status"].values()), {"pending"})
        draft_path = self.issue_dir / "issue-draft.eml"
        draft_path.write_bytes(draft_path.read_bytes() + b"altered")
        with self.assertRaisesRegex(ValueError, "Issue draft changed"):
            self.send("issue", when + timedelta(minutes=1), FakeSMTP())

    def test_connection_retries_are_spaced_and_bounded(self):
        when = datetime(2026, 10, 5, 7, tzinfo=timezone.utc)
        failed = lambda *a, **k: (_ for _ in ()).throw(OSError())
        with patch("circus_newsletter.delivery._alert"):
            self.assertEqual(send_campaign(self.root, ISSUE, "issue", when,
                                           smtp_factory=failed), "connection_failed")
            self.assertEqual(send_campaign(self.root, ISSUE, "issue", when + timedelta(minutes=19),
                                           smtp_factory=failed), "retry_wait")
            for minute in (20, 40, 60):
                self.assertEqual(send_campaign(self.root, ISSUE, "issue", when + timedelta(minutes=minute),
                                               smtp_factory=failed), "connection_failed")
            self.assertEqual(send_campaign(self.root, ISSUE, "issue", when + timedelta(minutes=80),
                                           smtp_factory=failed), "retry_exhausted")
            recovered = FakeSMTP()
            self.assertEqual(send_campaign(self.root, ISSUE, "issue", when + timedelta(minutes=80),
                                           approve_safe_retry=True,
                                           smtp_factory=lambda *a, **k: recovered), "sent")
            self.assertEqual(len(recovered.sent), 1)
        journal = json.loads((self.issue_dir / "outbound/issue/delivery.json").read_text())
        self.assertEqual(journal["retry_count"], 4)

    def test_explicit_refusal_retries_only_refused_recipient(self):
        class RefusingSMTP(FakeSMTP):
            def sendmail(self, sender, addresses, raw):
                self.sent.append((sender, addresses, raw))
                return {"morgan@example.com": (450, b"temporary")}

        when = datetime(2026, 10, 5, 7, tzinfo=timezone.utc)
        first = RefusingSMTP()
        self.assertEqual(self.send("issue", when, first), "partial_refusal")
        self.assertEqual(first.sent[0][1], ["rowan@example.com", "morgan@example.com"])
        second = FakeSMTP()
        self.assertEqual(self.send("issue", when + timedelta(minutes=20), second), "sent")
        self.assertEqual(second.sent[0][1], ["morgan@example.com"])
        self.assertEqual(first.sent[0][2], second.sent[0][2])
        self.assertEqual(len(list((self.issue_dir / "outbound/issue").glob("*.eml"))), 1)

    def test_task_triggers_cover_utc_times_across_dst(self):
        task = Path(__file__).resolve().parents[1] / "config/delivery-task.xml"
        root = ET.parse(task).getroot()
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        triggers = root.findall("t:Triggers/t:TimeTrigger", ns)
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0].findtext("t:Repetition/t:Interval", namespaces=ns), "PT1M")
        self.assertIsNone(triggers[0].find("t:Repetition/t:Duration", ns))
        self.assertIn("send_due.py", root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns))
        london = ZoneInfo("Europe/London")
        for sunday in (date(2026, 10, 4), date(2026, 11, 1)):
            issue = f"{sunday.isocalendar().year}-W{sunday.isocalendar().week:02d}"
            for kind, day, local_hours in (("reminder", sunday - timedelta(days=1), (17, 18)),
                                           ("issue", sunday + timedelta(days=1), (7, 8))):
                due = due_at(issue, kind, {"issue_send_time_utc": "07:00",
                                             "reminder_time_utc": "17:00"})
                self.assertIn(due, {datetime(day.year, day.month, day.day, hour,
                                             tzinfo=london).astimezone(timezone.utc)
                                    for hour in local_hours})
            next_sunday = sunday + timedelta(days=7)
            next_issue = f"{next_sunday.isocalendar().year}-W{next_sunday.isocalendar().week:02d}"
            self.assertEqual(due_at(next_issue, "invitation", {
                "issue_send_time_utc": "07:00", "reminder_time_utc": "17:00"}),
                due_at(issue, "issue", {"issue_send_time_utc": "07:00",
                                         "reminder_time_utc": "17:00"}))


if __name__ == "__main__":
    unittest.main()
