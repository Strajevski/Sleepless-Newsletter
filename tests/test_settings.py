import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from circus_newsletter.delivery import _message, send_campaign
from circus_newsletter.discord import dispatch_pending
from circus_newsletter.editor import fallback
from circus_newsletter.imap import connect
from circus_newsletter.render import render_html, render_text
from circus_newsletter.settings import (DEFAULTS, codex_environment, codex_executable,
                                        load_settings, mail_settings, prompt_text)

PROJECT = Path(__file__).resolve().parents[1]


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "config").mkdir()
        (self.root / "config/roster.json").write_text('{"members": {}}')
        self.settings = copy.deepcopy(DEFAULTS)
        self.settings["email"]["login_address"] = "newsletter@example.com"
        self.settings["newsletter"].update(sender_address="newsletter@example.com",
                                             name="Field Notes", short_name="Notes", editor_name="Alex")
        self.save()
        self.schedule = json.loads((PROJECT / "config/schedule.json").read_text(encoding="utf-8"))
        (self.root / "config/schedule.json").write_text(json.dumps(self.schedule))
        self.issue = json.loads((PROJECT / "templates/preview-data.json").read_text(encoding="utf-8"))

    def save(self):
        (self.root / "config/settings.json").write_text(json.dumps(self.settings), encoding="utf-8")

    def test_defaults_keep_discord_enabled_and_bare_sender(self):
        settings = load_settings(self.root)
        self.assertTrue(settings["notifications"]["discord_enabled"])
        self.assertEqual(settings["newsletter"]["sender_name"], "")
        self.assertEqual(settings["newsletter"]["signature"], "Alex")
        self.assertEqual(settings["newsletter"]["headings"]["editorial"], "Alex’s Take")

    def test_headings_are_escaped_and_do_not_determine_section_behaviour(self):
        self.settings["newsletter"]["name"] = "Field <Notes> & Friends"
        self.settings["newsletter"]["headings"].update(
            feed="News <&>", worth_your_time="Updates", personal_updates="Updates",
            gallery="Pictures", editorial="A Final Thought", ledger="Contributors")
        self.save()
        settings = load_settings(self.root)
        page = render_html(self.issue, self.schedule, PROJECT / "templates/issue-email.html",
                           preview=True, settings=settings)
        text = render_text(self.issue, self.schedule, settings=settings)
        self.assertIn("FIELD &lt;NOTES&gt; &amp; FRIENDS", page)
        self.assertIn("NEWS &lt;&amp;&gt;", page)
        self.assertIn("A FINAL THOUGHT", page)
        self.assertIn("A FINAL THOUGHT", text)
        self.assertIn(self.issue["worth_your_time"][0]["url"], text)
        self.assertIn(self.issue["personal_updates"][0]["name"], text)
        self.assertNotIn("SLEEPLESS", page)

    def test_bundled_prompts_receive_identity_but_keep_schema_keys(self):
        settings = load_settings(self.root)
        for path in (PROJECT / "prompts").glob("*.md"):
            rendered = prompt_text(path.read_text(encoding="utf-8"), settings)
            self.assertNotIn("{{", rendered)
            self.assertNotIn("Sleepless", rendered)
            self.assertNotIn("Circus Newsletter", rendered)
            if '"sleepless_take"' in path.read_text(encoding="utf-8"):
                self.assertIn('"sleepless_take"', rendered)
        with self.assertRaisesRegex(ValueError, "Unknown prompt"):
            prompt_text("{{misspelled_name}}", settings)

    def test_message_identity_signature_reply_address_and_privacy(self):
        self.settings["newsletter"].update(sender_name="Álex & Friends", reply_to_address="replies@example.com")
        self.save()
        raw = _message(self.root, "2026-W41", "reminder", "", "",
                       datetime(2026, 10, 10, 17, tzinfo=timezone.utc), None, {})
        message = BytesParser(policy=policy.default).parsebytes(raw)
        self.assertEqual(message["From"].addresses[0].display_name, "Álex & Friends")
        self.assertEqual(str(message["Reply-To"]), "replies@example.com")
        self.assertEqual(str(message["To"]), "newsletter@example.com")
        self.assertIsNone(message["Cc"])
        self.assertIsNone(message["Bcc"])
        self.assertIn("Field Notes", message["Subject"])
        self.assertIn("Hello Notes,", message.get_content())
        self.assertIn("replies@example.com", message.get_content())
        self.assertTrue(message.get_content().endswith("Alex\r\n"))

    def test_mail_uses_selected_root_and_never_legacy_account_when_settings_exist(self):
        (self.root / "config/mail.json").write_text(json.dumps({
            "account": "old@example.com", "host": "imap.gmail.com", "port": 993, "mailbox": "INBOX"}))
        with patch("circus_newsletter.imap.imaplib.IMAP4_SSL") as factory:
            factory.return_value.select.return_value = ("OK", [])
            factory.return_value.capabilities = [b"X-GM-EXT-1"]
            connect(self.root, "fake-secret")
            factory.return_value.login.assert_called_once_with("newsletter@example.com", "fake-secret")
            factory.return_value.select.assert_called_once_with("INBOX", readonly=True)
        self.settings["email"]["login_address"] = ""
        self.save()
        with self.assertRaises(ValueError):
            mail_settings(self.root)

    def test_smtp_login_and_envelope_use_configured_account(self):
        (self.root / "config/roster.json").write_text(json.dumps({"members": {
            "reader": {"name": "Reader", "emails": ["reader@example.com"]}}}))
        (self.root / "config/delivery.json").write_text('{"auto_send_fallback": true}')
        with patch("circus_newsletter.delivery.read_password", return_value="fake-secret"), \
             patch("circus_newsletter.discord.dispatch_pending", return_value=0), \
             patch("smtplib.SMTP_SSL") as factory:
            factory.return_value.sendmail.return_value = {}
            status = send_campaign(self.root, "2026-W40", "reminder",
                                   datetime(2026, 10, 3, 17, tzinfo=timezone.utc), smtp_factory=factory)
            self.assertEqual(status, "sent")
            factory.return_value.login.assert_called_once_with("newsletter@example.com", "fake-secret")
            self.assertEqual(factory.return_value.sendmail.call_args.args[:2],
                             ("newsletter@example.com", ["reader@example.com"]))

    def test_invalid_settings_fail_before_mail_connection(self):
        for section, key, value in (("newsletter", "sender_name", "Editor\nBcc: hidden@example.com"),
                                    ("email", "provider", "other"),
                                    ("notifications", "discord_enabled", "false")):
            original = self.settings[section][key]
            self.settings[section][key] = value
            self.save()
            with patch("circus_newsletter.imap.imaplib.IMAP4_SSL") as factory:
                with self.assertRaises(ValueError):
                    connect(self.root, "fake-secret")
                factory.assert_not_called()
            self.settings[section][key] = original

    def test_codex_overrides_resolve_relative_to_settings_root(self):
        self.settings["codex"].update(executable="tools/codex.exe", home_directory="private/login")
        self.save()
        self.assertEqual(codex_executable(self.root), str(self.root / "tools/codex.exe"))
        self.assertEqual(codex_executable(self.root, prefer_exe=True), str(self.root / "tools/codex.exe"))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake", "CODEX_API_KEY": "fake"}):
            for profile_defaults in (True, False):
                environment = codex_environment(self.root, profile_defaults=profile_defaults)
                self.assertEqual(environment["CODEX_HOME"], str(self.root / "private/login"))
                self.assertNotIn("OPENAI_API_KEY", environment)
                self.assertNotIn("CODEX_API_KEY", environment)

    def test_explicit_discord_switch_keeps_secrets_and_network_untouched(self):
        self.settings["notifications"]["discord_enabled"] = False
        self.save()
        with patch("circus_newsletter.discord.read_secret") as read, \
             patch("circus_newsletter.discord.post_webhook") as post:
            self.assertEqual(dispatch_pending(self.root), 0)
            read.assert_not_called()
            post.assert_not_called()

    def test_fallback_uses_configured_newsletter_and_gallery_names(self):
        self.settings["newsletter"]["headings"]["gallery"] = "Pictures"
        self.save()
        settings = load_settings(self.root)
        empty = fallback("2026-W40", [], "offline", settings=settings)
        self.assertEqual(empty["feed"][0]["attribution"], "Field Notes")
        draft = fallback("2026-W40", [{"body": "", "subject": "Photo", "display_name": "Reader",
                                      "submission_id": "reader-001"}], "offline", settings=settings)
        self.assertIn("the Pictures", draft["feed"][0]["text"])

    def test_trial_copy_preserves_original_relative_tool_and_login_paths(self):
        from scripts.trial_weekly import _stage
        self.settings["codex"].update(executable="tools/codex.exe", home_directory="private/login")
        self.save()
        (self.root / "config/captioner.json").write_text('{"enabled": true}')
        for directory in ("prompts", "templates", "issues/2026-W40/attachments",
                          "issues/2026-W40/submissions"):
            (self.root / directory).mkdir(parents=True)
        (self.root / "issues/2026-W40/issue-input-final.md").write_text("Frozen test input")
        output = self.root / "trial"
        with patch("scripts.trial_weekly.previous_issue_summaries", return_value=[]):
            _stage(self.root, self.root, output, "2026-W40")
        self.assertEqual(codex_executable(output), str(self.root / "tools/codex.exe"))
        self.assertEqual(codex_environment(output)["CODEX_HOME"], str(self.root / "private/login"))


if __name__ == "__main__":
    unittest.main()
