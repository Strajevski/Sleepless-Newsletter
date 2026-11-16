import json
import subprocess
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path


class CommandLineSmokeTest(unittest.TestCase):
    def test_local_email_then_bundle(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config/roster.json").write_text(json.dumps({"members": {
                "rowan": {"name": "Rowan", "emails": ["rowan@example.com"]}}}), encoding="utf-8")
            (root / "config/schedule.json").write_text(json.dumps({
                "timezone": "Europe/London", "cutoff_weekday": "Sunday", "cutoff_time": "18:00"
            }), encoding="utf-8")
            email = EmailMessage()
            email["From"] = "rowan@example.com"
            email["To"] = "newsletter@example.com"
            email["Date"] = "Tue, 29 Sep 2026 11:00:00 +0100"
            email["Message-ID"] = "<cli-test@example.com>"
            email["Subject"] = "CLI sample"
            email.set_content("A sample submission https://example.com/sample")
            sample = root / "sample.eml"
            sample.write_bytes(email.as_bytes())
            intake = [sys.executable, str(project / "scripts/intake.py"), "--root", str(root), "--eml", str(sample)]
            subprocess.run(intake, check=True, capture_output=True, text=True)
            subprocess.run(intake, check=True, capture_output=True, text=True)
            subprocess.run([sys.executable, str(project / "scripts/build_issue.py"),
                            "2026-W40", "--root", str(root)], check=True, capture_output=True, text=True)
            bundle = (root / "issues/2026-W40/issue-input.md").read_text(encoding="utf-8")
            self.assertEqual(bundle.count("### Submission rowan-001"), 1)
            self.assertIn("https://example.com/sample", bundle)


if __name__ == "__main__":
    unittest.main()
