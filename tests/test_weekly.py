import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from circus_newsletter.core import build_issue, finalize_due_issues, ingest
from circus_newsletter.discord import _events
from circus_newsletter.editor import (EditorFailure, editorial_input,
                                      previous_issue_summaries, submissions)
from circus_newsletter.editor_gate import parse_budget
from circus_newsletter.weekly import latest_closed_issue, process_issue


def when(day, hour):
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


def budget(weekly, five_hour):
    return parse_budget({"rateLimits": {
        "rateLimitReachedType": None, "credits": {"hasCredits": False},
        "primary": {"windowDurationMins": 300, "usedPercent": five_hour, "resetsAt": 1790676299},
        "secondary": {"windowDurationMins": 10080, "usedPercent": weekly, "resetsAt": 1791171040}
    }})


class WeeklyDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        project = Path(__file__).resolve().parents[1]
        for directory in ("config", "templates", "prompts"):
            (self.root / directory).mkdir()
        (self.root / "config/mail.json").write_text(json.dumps({"account": "newsletter@example.com", "host": "imap.gmail.com", "port": 993, "mailbox": "INBOX"}), encoding="utf-8")
        for name in ("editor.json", "editor-output.schema.json", "delivery.json"):
            shutil.copy2(project / "config" / name, self.root / "config" / name)
        # These tests intentionally exercise the previous Sunday-cutoff format.
        delivery_path = self.root / "config/delivery.json"
        delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
        delivery.update(issue_send_time_utc="08:00", reminder_time_utc="17:00",
                        catch_up_minutes=120, delivery_retry_minutes=20, delivery_retry_limit=3)
        delivery_path.write_text(json.dumps(delivery), encoding="utf-8")
        shutil.copy2(project / "templates/issue-email.html", self.root / "templates/issue-email.html")
        prompt_path = json.loads((project / "config/editor.json").read_text(encoding="utf-8"))["prompt"]
        shutil.copy2(project / prompt_path, self.root / prompt_path)
        (self.root / "config/roster.json").write_text(json.dumps({"members": {
            "rowan": {"name": "Rowan", "emails": ["rowan@example.com"], "pronouns": "he/him"}
        }}), encoding="utf-8")
        (self.root / "config/schedule.json").write_text(json.dumps({
            "timezone": "Europe/London", "cutoff_timezone": "UTC", "cutoff_weekday": "Sunday",
            "cutoff_time": "23:00", "first_issue_week": "2026-W40", "first_issue_number": 2
        }), encoding="utf-8")
        for index, body in enumerate(("First note with https://example.com/a", "Second note"), 1):
            message = EmailMessage()
            message["From"] = "Rowan <rowan@example.com>"
            message["To"] = "newsletter@example.com"
            message["Subject"] = f"Note {index}"
            message["Message-ID"] = f"<weekly-{index}@example.com>"
            message.set_content(body)
            if index == 2:
                message.add_attachment(b"\x89PNG\r\n\x1a\n", maintype="image", subtype="png",
                                       filename="photo.png")
                message.add_attachment(b"sample-video", maintype="video", subtype="mp4",
                                       filename="clip.mp4")
            ingest(self.root, message.as_bytes(), f"gmail:{index}", when(1, index))
        build_issue(self.root, "2026-W40")
        finalize_due_issues(self.root, when(4, 23))

    def test_limit_waits_until_five_then_uses_normal_template(self):
        self.assertIsNone(latest_closed_issue(self.root, when(4, 22)))
        self.assertEqual(latest_closed_issue(self.root, when(4, 23)), "2026-W40")
        with patch("circus_newsletter.weekly.read_codex_budget", return_value=budget(91, 20)):
            self.assertEqual(process_issue(self.root, "2026-W40", when(4, 23)), "deferred")
            self.assertFalse((self.root / "issues/2026-W40/editor-result.json").exists())
            self.assertEqual(process_issue(self.root, "2026-W40", when(5, 4), retry=True), "fallback")
        issue_dir = self.root / "issues/2026-W40"
        data = json.loads((issue_dir / "issue-data.json").read_text(encoding="utf-8"))
        self.assertEqual([x["text"] for x in data["feed"]],
                         ["First note with https://example.com/a", "Second note"])
        self.assertEqual(data["sleepless_take"], "")
        self.assertEqual(len(data["gallery"]), 1)
        self.assertIn("SLEEPLESS’S TAKE", (issue_dir / "issue-email.html").read_text(encoding="utf-8"))
        self.assertIn('src="attachments/', (issue_dir / "issue-preview.html").read_text(encoding="utf-8"))
        self.assertIn(b"Content-ID:", (issue_dir / "issue-draft.eml").read_bytes())
        from email import policy
        from email.parser import BytesParser
        mail = BytesParser(policy=policy.default).parsebytes((issue_dir / "issue-draft.eml").read_bytes())
        self.assertTrue(str(mail["Subject"]).startswith("Circus Newsletter №2:"))
        video = next(part for part in mail.iter_attachments() if part.get_content_type() == "video/mp4")
        self.assertEqual(video.get_payload(decode=True), b"sample-video")
        self.assertTrue(video.get_filename().endswith("clip.mp4"))
        text_files = [part for part in mail.iter_attachments() if part.get_filename() == "circus-2026-W40-submissions.txt"]
        self.assertEqual(len(text_files), 1)
        submitted = text_files[0].get_content()
        self.assertEqual(submitted.count("=== Rowan - "), 2)
        self.assertIn("First note with https://example.com/a", submitted)
        self.assertIn("Second note", submitted)
        self.assertEqual((issue_dir / "submitted-text.txt").read_text(encoding="utf-8"), submitted)
        self.assertEqual(len(list(mail.iter_attachments())), 2)  # Video plus text; the image is inline.
        result = json.loads((issue_dir / "editor-result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["mode"], "fallback")
        self.assertIn("weekly 91%", result["reason"])
        alerts = dict(_events(self.root))
        self.assertIn("weekly 91%", alerts["editor-fallback:2026-W40"])

    def test_successful_codex_draft_is_validated_and_rendered(self):
        draft = {"issue_week": "2026-W40", "title": "Two notes", "summary": "Rowan sent two notes.",
                 "feed": [{"member_id": "rowan", "source_ids": ["rowan-001", "rowan-002"],
                           "headline": "News", "text": "First note with https://example.com/a\n\nSecond note"}],
                 "worth_your_time": [], "personal_updates": [], "gallery": [],
                 "sleepless_take": "The two notes arrive together.", "take_source_ids": ["rowan-001"],
                 "editorial_notes": {"ranking_reason": "Only one entry", "word_cap_exceptions": [],
                                     "review_flags": ["Mixed passage needs human review"]}}
        with patch("circus_newsletter.weekly.read_codex_budget", return_value=budget(10, 10)):
            with patch("circus_newsletter.weekly.codex_draft", return_value=draft):
                self.assertEqual(process_issue(self.root, "2026-W40", when(4, 23)), "ai")
        issue_dir = self.root / "issues/2026-W40"
        self.assertEqual(json.loads((issue_dir / "editor-result.json").read_text(encoding="utf-8"))["mode"], "ai")
        self.assertEqual(json.loads((issue_dir / "editor-raw.json").read_text(encoding="utf-8"))
                         ["editorial_notes"]["review_flags"], ["Mixed passage needs human review"])
        self.assertIn("The two notes arrive together.", (issue_dir / "issue-email.html").read_text(encoding="utf-8"))


    def test_runtime_limit_gets_one_retry_then_fallback(self):
        with patch("circus_newsletter.weekly.read_codex_budget", return_value=budget(10, 10)):
            with patch("circus_newsletter.weekly.codex_draft",
                       side_effect=EditorFailure("Codex usage limit reached", usage_limit=True)) as editor:
                self.assertEqual(process_issue(self.root, "2026-W40", when(4, 23)), "deferred")
                self.assertEqual(process_issue(self.root, "2026-W40", when(5, 4), retry=True), "fallback")
                self.assertEqual(editor.call_count, 2)

    def test_positions_survive_editor_pipeline_and_email_draft(self):
        draft = {"issue_week": "2026-W40", "title": "Positions", "summary": "Two notes.",
                 "feed": [], "personal_updates": [], "worth_your_time": [], "gallery": [],
                 "sleepless_take": "A short editorial.",
                 "long_and_short": {
                     "long": [{"text": "Lorem ipsum", "source_ids": ["rowan-001", "rowan-002"]}],
                     "short": [{"text": "Dolor sit amet", "source_ids": ["rowan-002"]}]}}
        with patch("circus_newsletter.weekly.read_codex_budget", return_value=budget(10, 10)), \
                patch("circus_newsletter.weekly.codex_draft", return_value=draft):
            self.assertEqual(process_issue(self.root, "2026-W40", when(4, 23)), "ai")
        directory = self.root / "issues/2026-W40"
        data = json.loads((directory / "issue-data.json").read_text(encoding="utf-8"))
        entry = data["long_and_short"]["long"][0]
        self.assertEqual(entry["contributors"], ["Rowan"])
        self.assertEqual(entry["source_ids"], ["rowan-001", "rowan-002"])
        self.assertIn("Lorem ipsum - Rowan", (directory / "issue.txt").read_text(encoding="utf-8"))
        self.assertIn("Dolor sit amet", (directory / "issue-preview.html").read_text(encoding="utf-8"))
        from email import policy
        from email.parser import BytesParser
        mail = BytesParser(policy=policy.default).parsebytes((directory / "issue-draft.eml").read_bytes())
        self.assertIn("Lorem ipsum - Rowan", mail.get_body(preferencelist=("plain",)).get_content())
        self.assertIn("- Rowan</span>", mail.get_body(preferencelist=("html",)).get_content())

    def test_missed_five_am_run_does_not_wait_another_week(self):
        with patch("circus_newsletter.weekly.read_codex_budget", return_value=budget(91, 20)):
            self.assertEqual(process_issue(self.root, "2026-W40", when(4, 23)), "deferred")
            self.assertEqual(process_issue(self.root, "2026-W40", when(6, 10)), "fallback")

    def test_editor_reads_clean_body_without_rewriting_archived_submission(self):
        path = self.root / "issues/2026-W40/submissions/rowan-001.md"
        original = path.read_bytes()
        dirty = (original.decode("utf-8").rstrip() +
                 "\n\nOn 1 Oct 2026, at 08:52, newsletter@example.com wrote:\n"
                 "> Old invitation https://example.com/invitation\n")
        path.write_text(dirty, encoding="utf-8")
        archived = path.read_bytes()

        records, _ = submissions(self.root, "2026-W40")
        record = next(item for item in records if item["submission_id"] == "rowan-001")

        self.assertEqual(record["body"], "First note with https://example.com/a")
        self.assertEqual(record["submitted_links"], ["https://example.com/a"])
        self.assertEqual(path.read_bytes(), archived)

    def test_editor_uses_four_previous_sent_issues_only(self):
        for week in range(40, 46):
            name = f"2026-W{week:02d}"
            directory = self.root / "issues" / name
            (directory / "outbound/issue").mkdir(parents=True, exist_ok=True)
            (directory / "issue-data.json").write_text(json.dumps({
                "issue_week": name, "title": f"Title {week}",
                "summary": f"Summary {week}", "sleepless_take": f"Take {week}"}),
                encoding="utf-8")
            status = {"rowan": "pending" if week == 43 else "sent"}
            (directory / "outbound/issue/delivery.json").write_text(json.dumps({
                "issue_week": name, "kind": "issue", "status": status}), encoding="utf-8")
        history = previous_issue_summaries(self.root, "2026-W46")
        self.assertEqual([item["issue_week"] for item in history],
                         ["2026-W41", "2026-W42", "2026-W44", "2026-W45"])
        self.assertEqual(history[-1]["sleepless_take"], "Take 45")
        prompt = editorial_input(self.root, "2026-W46", [])
        self.assertIn('"previous_issue_summaries": [', prompt)
        self.assertIn('"summary": "Summary 45"', prompt)
        self.assertNotIn('"summary": "Summary 40"', prompt)
        self.assertNotIn('"summary": "Summary 43"', prompt)


if __name__ == "__main__":
    unittest.main()
