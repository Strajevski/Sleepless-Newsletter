import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import send_announcement


class AnnouncementTests(unittest.TestCase):
    def test_one_message_uses_private_envelope_recipients(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config/mail.json").write_text(json.dumps({"account": "newsletter@example.com", "host": "imap.gmail.com", "port": 993, "mailbox": "INBOX"}), encoding="utf-8")
            (root / "config/roster.json").write_text(json.dumps({"members": {
                "a": {"name": "A", "emails": ["a@example.com"]},
                "b": {"name": "B", "emails": ["b@example.com"]}}}), encoding="utf-8")
            (root / "config/schedule.json").write_text(json.dumps({
                "timezone": "Europe/London", "cutoff_weekday": "Sunday",
                "cutoff_time": "23:00"}), encoding="utf-8")
            draft = root / "announcement.txt"
            draft.write_text("To: Circus roster (private SMTP recipients)\n"
                             "From: newsletter@example.com\n"
                             "Subject: Hello\n\nHello all\n", encoding="utf-8")

            class SMTP:
                sent = []

                def __init__(self, *args, **kwargs):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def login(self, *args):
                    pass

                def sendmail(self, sender, recipients, raw):
                    self.sent.append((recipients, raw))
                    return {}

            with patch.object(send_announcement, "read_password", return_value="test"), patch.object(
                    send_announcement.smtplib, "SMTP_SSL", SMTP):
                self.assertEqual(send_announcement.send(root, draft, "trial"), "sent")
            self.assertEqual(SMTP.sent[0][0], ["a@example.com", "b@example.com"])
            self.assertEqual(len(SMTP.sent), 1)
            self.assertIn(b"To: newsletter@example.com", SMTP.sent[0][1])
            self.assertEqual(len(list((root / "announcements/trial").glob("*.eml"))), 1)


if __name__ == "__main__":
    unittest.main()
