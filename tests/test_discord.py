import json
import io
import tempfile
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from circus_newsletter.core import build_issue, finalize_due_issues, ingest
from circus_newsletter.discord import (STATE_FILE, RateLimited, _events, baseline_existing,
                                       dispatch_pending, post_webhook, queue_editor_alert,
                                       validate_webhook_url)
from circus_newsletter.secret_store import DISCORD_SECRET_NAME


class DiscordNotificationTests(unittest.TestCase):
    def test_fallback_notification_matches_automatic_delivery_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config/delivery.json").write_text(json.dumps({
                "auto_send_fallback": True, "issue_holds": {}
            }), encoding="utf-8")
            issue = root / "issues/2026-W41"
            issue.mkdir(parents=True)
            (issue / "editor-result.json").write_text(json.dumps({
                "mode": "fallback", "reason": "Editor unavailable"
            }), encoding="utf-8")
            event = dict(_events(root))["editor-fallback:2026-W41"]
            self.assertIn("Scheduled for automatic delivery", event)
            self.assertNotIn("before sending", event)

    def test_url_must_point_to_discord_webhook(self):
        valid = "https://discord.com/api/webhooks/123456/abc_DEF-ghi"
        self.assertEqual(validate_webhook_url(valid), valid)
        for url in ("http://discord.com/api/webhooks/123/x",
                    "https://discord.com.evil.example/api/webhooks/123/x",
                    "https://discord.com/api/webhooks/123/x?token=extra"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_webhook_url(url)

    def test_post_waits_for_confirmation_and_disables_mentions(self):
        url = "https://discord.com/api/webhooks/123456/secret"
        with patch("circus_newsletter.discord.urlopen", return_value=io.BytesIO(b'{"id":"987"}')) as send:
            self.assertEqual(post_webhook(url, "Rowan @everyone"), "987")
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, url + "?wait=true")
        self.assertEqual(json.loads(request.data)["allowed_mentions"], {"parse": []})

    def test_baseline_retry_and_final_bundle_notification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config/roster.json").write_text(json.dumps({"members": {
                "rowan": {"name": "Rowan", "emails": ["rowan@example.com"], "pronouns": "he/him"}
            }}), encoding="utf-8")
            (root / "config/schedule.json").write_text(json.dumps({
                "timezone": "Europe/London", "cutoff_timezone": "UTC", "cutoff_weekday": "Sunday",
                "cutoff_time": "23:00", "first_issue_week": "2026-W40"
            }), encoding="utf-8")

            def submit(source, message_id, subject):
                mail = EmailMessage()
                mail["From"] = "Rowan <rowan@example.com>"
                mail["To"] = "newsletter@example.com"
                mail["Subject"] = subject
                mail["Message-ID"] = message_id
                mail.set_content("A story")
                ingest(root, mail.as_bytes(), source, datetime(2026, 9, 29, 10, tzinfo=timezone.utc))
                build_issue(root, "2026-W40")

            submit("gmail:old", "<old@example.com>", "Older submission")
            self.assertEqual(baseline_existing(root), 1)
            submit("gmail:new", "<new@example.com>", "New submission")
            finalize_due_issues(root, datetime(2026, 10, 4, 23, tzinfo=timezone.utc))
            secret = root / "config/secrets" / DISCORD_SECRET_NAME
            secret.parent.mkdir(parents=True)
            secret.write_bytes(b"placeholder")
            with patch("circus_newsletter.discord.read_secret", return_value="https://discord.com/api/webhooks/1/x"):
                with patch("circus_newsletter.discord.post_webhook", side_effect=RateLimited(2)):
                    self.assertEqual(dispatch_pending(root), 0)
                messages = []
                def send(_url, content):
                    messages.append(content)
                    return str(len(messages))
                with patch("circus_newsletter.discord.post_webhook", side_effect=send):
                    self.assertEqual(dispatch_pending(root), 2)
                    self.assertEqual(dispatch_pending(root), 0)
            self.assertEqual(len(messages), 2)
            self.assertIn("rowan-002", messages[0])
            self.assertIn("editor input finalized", messages[1])
            state = json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
            self.assertEqual(state["submission:gmail:old"]["status"], "before-connection")
            self.assertEqual(state["submission:gmail:new"]["message_id"], "1")
            self.assertEqual(state["final-input:2026-W40"]["message_id"], "2")

    def test_editor_alert_is_queued_once_and_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret = root / "config/secrets" / DISCORD_SECRET_NAME
            secret.parent.mkdir(parents=True)
            secret.write_bytes(b"placeholder")
            queue_editor_alert(root, "2026-W40", "Codex usage limit reached.")
            queue_editor_alert(root, "2026-W40", "Another failure.")
            with patch("circus_newsletter.discord.read_secret", return_value="https://discord.com/api/webhooks/1/x"):
                with patch("circus_newsletter.discord.post_webhook", side_effect=RateLimited(2)):
                    self.assertEqual(dispatch_pending(root), 0)
                with patch("circus_newsletter.discord.post_webhook", return_value="123") as send:
                    self.assertEqual(dispatch_pending(root), 1)
                    self.assertEqual(dispatch_pending(root), 0)
                    self.assertIn("Codex usage limit reached", send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
