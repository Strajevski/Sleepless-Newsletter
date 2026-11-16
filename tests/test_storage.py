import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

import test_delivery as delivery_cases
from circus_newsletter.core import (build_issue, existing_source_ids, finalize_due_issues,
                                    ingest, issues_needing_bundle)
from circus_newsletter.discord import queue_delivery_alert
from circus_newsletter.editor import fallback, publish_draft, submissions
from circus_newsletter.settings import DEFAULTS, data_directory, load_settings
from scripts.compact_outbound_archive import compact
from scripts import manual_ops

PROJECT = Path(__file__).resolve().parents[1]


class StorageTests(unittest.TestCase):
    def setUp(self):
        delivery_cases.DeliveryTests.setUp(self)
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        self.data = Path(external.name).resolve() / "Private newsletter data"
        self.data.mkdir()
        shutil.copytree(self.root / "issues", self.data / "issues")
        self.settings = copy.deepcopy(DEFAULTS)
        self.settings["email"]["login_address"] = "newsletter@example.com"
        self.settings["newsletter"]["sender_address"] = "newsletter@example.com"
        self.settings["storage"]["data_directory"] = str(self.data)
        self.save()

    def save(self):
        (self.root / "config/settings.json").write_text(json.dumps(self.settings), encoding="utf-8")

    def test_default_and_relative_paths_are_independent_of_working_directory(self):
        self.settings["storage"]["data_directory"] = "."
        self.save()
        self.assertEqual(data_directory(self.root), self.root.resolve())
        self.settings["storage"]["data_directory"] = "private/files"
        self.save()
        self.assertEqual(data_directory(self.root), (self.root / "private/files").resolve())
        command = [sys.executable, str(PROJECT / "scripts/data_directory.py"), "--root", str(self.root)]
        result = subprocess.run(command, cwd=self.data, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), str((self.root / "private/files").resolve()))
        self.assertFalse((self.root / "private").exists())  # Resolving never creates or moves data.

    def test_blank_data_directory_is_rejected(self):
        for value in ("", "  ", None, False):
            self.settings["storage"]["data_directory"] = value
            self.save()
            with self.assertRaises(ValueError):
                data_directory(self.root)

    def test_external_delivery_journal_prevents_duplicate_send(self):
        now = datetime(2026, 10, 5, 7, tzinfo=timezone.utc)
        smtp = delivery_cases.FakeSMTP()
        result = delivery_cases.DeliveryTests.send(self, "issue", now, smtp)
        self.assertEqual(result, "sent")
        external_journal = self.data / "issues/2026-W40/outbound/issue/delivery.json"
        self.assertEqual(set(json.loads(external_journal.read_text())["status"].values()), {"sent"})
        second = delivery_cases.FakeSMTP()
        self.assertEqual(delivery_cases.DeliveryTests.send(self, "issue", now, second), "already_sent")
        self.assertEqual(second.sent, [])
        self.assertFalse((self.root / "issues/2026-W40/outbound").exists())
        self.assertTrue((self.data / "state/outbound.lock").exists())

    def test_ingest_freeze_and_render_use_data_but_templates_stay_with_application(self):
        (self.root / "templates").mkdir()
        shutil.copy2(PROJECT / "templates/issue-email.html", self.root / "templates/issue-email.html")
        message = EmailMessage()
        message["From"] = "rowan@example.com"
        message["Subject"] = "A portable story"
        message["Message-ID"] = "<portable@example.com>"
        message.set_content("A fictional observation.")
        message.add_attachment(b"original-photo", maintype="image", subtype="png", filename="photo.png")
        record = ingest(self.root, message.as_bytes(), "gmail:portable",
                        datetime(2026, 10, 6, 12, tzinfo=timezone.utc))
        issue = record["issue_week"]
        self.assertIn("gmail:portable", existing_source_ids(self.root))
        self.assertIn(issue, issues_needing_bundle(self.root))
        self.assertTrue(build_issue(self.root, issue).is_relative_to(self.data))
        frozen = finalize_due_issues(self.root, datetime(2026, 10, 12, 0, tzinfo=timezone.utc))
        self.assertTrue(any(path.parent.name == issue for path in frozen))
        records, digest = submissions(self.root, issue)
        draft = fallback(issue, records, "offline", settings=load_settings(self.root))
        with patch("circus_newsletter.editor.caption_images", return_value=None):
            publish_draft(self.root, issue, draft, records, "fallback", "offline", digest)
        directory = self.data / "issues" / issue
        self.assertTrue((directory / "issue-draft.eml").exists())
        self.assertIn("A fictional observation.", (directory / "issue.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "issues" / issue).exists())
        self.assertTrue((self.root / "templates/issue-email.html").exists())
        self.assertFalse((self.data / "config").exists())
        self.assertFalse((self.data / "templates").exists())

    def test_alerts_and_manual_recovery_read_the_external_archive(self):
        queue_delivery_alert(self.root, "issue-size:2026-W40", "Fictional alert")
        self.assertTrue((self.data / "state/delivery-alerts.json").exists())
        self.assertFalse((self.root / "state").exists())
        with patch.object(manual_ops, "ROOT", self.root):
            self.assertEqual(manual_ops._journal("2026-W40"),
                             self.data / "issues/2026-W40/outbound/issue/delivery.json")
            manual_ops._hold("2026-W40", "Review")
        self.assertTrue(list((self.data / "revisions").glob("delivery-before-hold-*.json")))
        self.assertIn("2026-W40", json.loads((self.root / "config/delivery.json").read_text())["issue_holds"])

    def test_compaction_protects_external_archive_and_reports_relative_paths(self):
        directory = self.data / "issues/2026-W39/outbound/issue"
        directory.mkdir(parents=True)
        (directory / "delivery.json").write_text(json.dumps({"status": {"a": "sent", "b": "sent"}}))
        (directory / "a.eml").write_bytes(b"a")
        (directory / "b.eml").write_bytes(b"b")
        with self.assertRaises(ValueError):
            compact(self.root, self.data / "unsafe-backup")
        report = compact(self.root, self.data.parent / "separate-backup")
        self.assertEqual(Path(report[0]["campaign"]), Path("issues/2026-W39/outbound/issue"))
        self.assertEqual(len(list(directory.glob("*.eml"))), 2)

    def test_trial_stage_isolated_from_external_live_data(self):
        from scripts.trial_weekly import _stage
        (self.root / "prompts").mkdir()
        (self.root / "templates").mkdir()
        (self.root / "config/captioner.json").write_text('{"enabled": true}')
        for name in ("attachments", "submissions"):
            (self.data / "issues/2026-W40" / name).mkdir()
        before = (self.data / "issues/2026-W40/issue-input-final.md").read_bytes()
        trial = self.data / "output/trial"
        with patch("scripts.trial_weekly.previous_issue_summaries", return_value=[]):
            _stage(self.root, self.root, trial, "2026-W40")
        self.assertEqual(data_directory(trial), trial.resolve())
        self.assertEqual((trial / "issues/2026-W40/issue-input-final.md").read_bytes(), before)
        self.assertEqual((self.data / "issues/2026-W40/issue-input-final.md").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
