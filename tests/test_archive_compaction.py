import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.compact_outbound_archive import compact


class ArchiveCompactionTests(unittest.TestCase):
    def test_completed_campaign_is_backed_up_before_compaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, backup = base / "project", base / "backup"
            directory = root / "issues/2026-W41/outbound/issue"
            directory.mkdir(parents=True)
            (directory / "delivery.json").write_text(json.dumps({
                "status": {"a": "sent", "b": "sent"}}), encoding="utf-8")
            (directory / "a.eml").write_bytes(b"message a")
            (directory / "b.eml").write_bytes(b"message b")
            self.assertEqual(compact(root, backup)[0]["action"], "would compact")
            self.assertEqual(len(list(directory.glob("*.eml"))), 2)
            self.assertEqual(compact(root, backup, apply=True)[0]["action"], "compact")
            self.assertEqual((directory / "message.eml").read_bytes(), b"message a")
            self.assertEqual(len(list(directory.glob("*.eml"))), 1)
            archive = backup / "issues/2026-W41/outbound/issue-legacy-eml.zip"
            with zipfile.ZipFile(archive) as saved:
                self.assertEqual(saved.read("a.eml"), b"message a")
                self.assertEqual(saved.read("b.eml"), b"message b")
                self.assertEqual(json.loads(saved.read("delivery.json"))["status"],
                                 {"a": "sent", "b": "sent"})

    def test_incomplete_campaign_is_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            directory = base / "project/issues/2026-W41/outbound/issue"
            directory.mkdir(parents=True)
            (directory / "delivery.json").write_text(json.dumps({
                "status": {"a": "sent", "b": "uncertain"}}), encoding="utf-8")
            (directory / "a.eml").write_bytes(b"a")
            (directory / "b.eml").write_bytes(b"b")
            self.assertIn("skipped", compact(base / "project", base / "backup", apply=True)[0]["action"])
            self.assertEqual(len(list(directory.glob("*.eml"))), 2)


if __name__ == "__main__":
    unittest.main()
