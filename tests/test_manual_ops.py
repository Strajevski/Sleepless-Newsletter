import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import manual_ops


class ManualOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.issue = "2026-W42"
        self.directory = self.root / "issues" / self.issue
        self.directory.mkdir(parents=True)
        self.root_patch = patch.object(manual_ops, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_delivery_journal_blocks_editorial_changes(self):
        journal = self.directory / "outbound" / "issue" / "delivery.json"
        journal.parent.mkdir(parents=True)
        journal.write_text('{"status":{"casey":"pending"}}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "delivery journal exists"):
            manual_ops._require_undelivered(self.issue)

    def test_task_pause_restores_only_originally_enabled_tasks(self):
        before = [
            {"name": manual_ops.TASKS[0], "enabled": True, "state": "Ready"},
            {"name": manual_ops.TASKS[1], "enabled": False, "state": "Disabled"},
            {"name": manual_ops.TASKS[2], "enabled": True, "state": "Ready"},
        ]
        with (patch.object(manual_ops, "_task_states", side_effect=[before, before]),
              patch.object(manual_ops, "_change_task") as change):
            with manual_ops._pause_tasks():
                pass
        self.assertEqual([call.args for call in change.call_args_list], [
            (manual_ops.TASKS[0], False), (manual_ops.TASKS[2], False),
            (manual_ops.TASKS[2], True), (manual_ops.TASKS[0], True),
        ])

    def test_hold_preserves_other_delivery_settings_and_backs_up(self):
        path = self.root / "config" / "delivery.json"
        path.parent.mkdir()
        initial = {"issue_send_time_utc": "07:00", "reminder_time_utc": "17:00",
                   "catch_up_minutes": 120, "auto_send_fallback": True,
                   "issue_holds": {"2026-W40": "Existing hold"}}
        path.write_text(json.dumps(initial), encoding="utf-8")
        self.assertTrue(manual_ops._hold(self.issue, "Review"))
        updated = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(updated["issue_holds"],
                         {"2026-W40": "Existing hold", self.issue: "Review"})
        backups = list((self.root / "revisions").glob("delivery-before-hold-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), initial)

    def test_retry_saves_rejected_json_without_replacing_live_draft(self):
        (self.directory / "issue-input-final.md").write_text("frozen", encoding="utf-8")
        (self.directory / "editor-result.json").write_text("old marker", encoding="utf-8")
        record = {"submission_id": "casey-001", "body": "A note"}
        candidate = {"issue_week": self.issue, "feed": [], "personal_updates": [],
                     "worth_your_time": []}
        budget = SimpleNamespace(allowed=True)
        with (patch.object(manual_ops, "_pause_tasks", return_value=nullcontext()),
              patch.object(manual_ops, "_hold"),
              patch.object(manual_ops, "read_codex_budget", return_value=budget),
              patch.object(manual_ops, "submissions", return_value=([record], "frozen-hash")),
              patch.object(manual_ops, "codex_draft", return_value=candidate),
              patch.object(manual_ops, "validate_draft", side_effect=ValueError("missing"))):
            manual_ops._retry(self.issue)
        saved = list((self.root / "revisions").glob("2026-W42-ai-retry-*.json"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(json.loads(saved[0].read_text(encoding="utf-8")), candidate)
        self.assertEqual((self.directory / "editor-result.json").read_text(encoding="utf-8"),
                         "old marker")

    def test_promote_backs_up_and_verifies_generated_artifacts(self):
        frozen = self.directory / "issue-input-final.md"
        frozen.write_bytes(b"frozen")
        old = self.directory / "issue-preview.html"
        old.write_text("old preview", encoding="utf-8")
        draft_path = self.directory / "issue-draft.eml"
        draft_path.write_bytes(b"old draft")
        candidate = self.root / "revisions" / "candidate.json"
        candidate.parent.mkdir()
        candidate.write_text(json.dumps({"issue_week": self.issue, "title": "New issue"}), encoding="utf-8")
        record = {"submission_id": "casey-001", "body": "A note"}

        def publish(root, issue, data, records, mode, reason, final_hash):
            self.assertEqual(mode, "ai")
            old.write_text("new preview", encoding="utf-8")
            draft_path.write_bytes(b"new draft")
            manual_ops._atomic_json(self.directory / "editor-result.json", {
                "draft_sha256": hashlib.sha256(b"new draft").hexdigest(),
                "final_input_sha256": hashlib.sha256(b"frozen").hexdigest(),
            })

        with (patch.object(manual_ops, "_pause_tasks", return_value=nullcontext()),
              patch.object(manual_ops, "_hold"),
              patch.object(manual_ops, "submissions", return_value=([record], "frozen-hash")),
              patch.object(manual_ops, "validate_draft"),
              patch.object(manual_ops, "issue_from_draft", return_value={}),
              patch.object(manual_ops, "publish_draft", side_effect=publish)):
            manual_ops._promote(self.issue, candidate, True)
        backups = list((self.root / "revisions").glob("2026-W42-before-promotion-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "issue-preview.html").read_text(encoding="utf-8"),
                         "old preview")
        self.assertEqual(old.read_text(encoding="utf-8"), "new preview")
        self.assertTrue((backups[0] / "promotion.json").exists())

    def test_scan_does_not_call_editor_or_delivery(self):
        connection = SimpleNamespace(logout=lambda: None)
        message = ("gmail:123", b"raw", SimpleNamespace())
        with (patch("builtins.input", return_value="y"),
              patch.object(manual_ops, "_pause_tasks", return_value=nullcontext()),
              patch.object(manual_ops, "setup_logging"),
              patch.object(manual_ops, "load_config", return_value=({}, {})),
              patch.object(manual_ops, "issue_for", return_value=(self.issue, None)),
              patch.object(manual_ops, "existing_source_ids", return_value=set()),
              patch("circus_newsletter.imap.connect", return_value=connection),
              patch("circus_newsletter.imap.messages", return_value=iter([message])),
              patch.object(manual_ops, "ingest", return_value={"submission_id": "casey-001",
                                                          "issue_week": self.issue}),
              patch.object(manual_ops, "issues_needing_bundle", return_value=set()),
              patch.object(manual_ops, "build_issue") as build):
            manual_ops._scan(False)
        build.assert_called_once_with(self.root, self.issue)

    def test_scan_rebuilds_archived_messages_after_fetch_error(self):
        connection = SimpleNamespace(logout=lambda: None)

        def messages_then_fail(*_args):
            yield ("gmail:123", b"raw", SimpleNamespace())
            raise OSError("IMAP interrupted")

        with (patch.object(manual_ops, "_pause_tasks", return_value=nullcontext()),
              patch.object(manual_ops, "setup_logging"),
              patch.object(manual_ops, "load_config", return_value=({}, {})),
              patch.object(manual_ops, "issue_for", return_value=(self.issue, None)),
              patch.object(manual_ops, "existing_source_ids", return_value=set()),
              patch("circus_newsletter.imap.connect", return_value=connection),
              patch("circus_newsletter.imap.messages", side_effect=messages_then_fail),
              patch.object(manual_ops, "ingest", return_value={"submission_id": "casey-001",
                                                          "issue_week": self.issue}),
              patch.object(manual_ops, "issues_needing_bundle", return_value=set()),
              patch.object(manual_ops, "build_issue") as build):
            with self.assertRaisesRegex(RuntimeError, "partial intake"):
                manual_ops._scan(True)
        build.assert_called_once_with(self.root, self.issue)


if __name__ == "__main__":
    unittest.main()
