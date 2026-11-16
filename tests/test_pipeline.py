import json
import tempfile
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from circus_newsletter.core import (build_issue, finalize_due_issues, ingest, issue_for,
                                    issues_needing_bundle, strip_reply_history)
from circus_newsletter.editor import submissions


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / "config/roster.json").write_text(json.dumps({"members": {
            "rowan": {"name": "Rowan", "emails": ["rowan@example.com"]}}}), encoding="utf-8")
        self.schedule = {"timezone": "Europe/London", "cutoff_weekday": "Sunday", "cutoff_time": "18:00"}
        (self.root / "config/schedule.json").write_text(json.dumps(self.schedule), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def email(self, sender="rowan@example.com", body="Hello https://example.com/story", message_id="<one@example.com>"):
        mail = EmailMessage()
        mail["From"] = sender
        mail["To"] = "newsletter@example.com"
        mail["Subject"] = "A story"
        mail["Message-ID"] = message_id
        mail.set_content(body)
        return mail

    def test_cutoff_and_dst(self):
        before = datetime(2026, 10, 25, 17, 59, tzinfo=timezone.utc)
        at = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(issue_for(before, self.schedule)[0], "2026-W43")
        self.assertEqual(issue_for(at, self.schedule)[0], "2026-W44")
        summer = datetime(2026, 6, 28, 17, 0, tzinfo=timezone.utc)
        self.assertEqual(issue_for(summer, self.schedule)[0], "2026-W27")

    def test_separate_submissions_idempotency_bundle_and_attachment(self):
        time = datetime(2026, 9, 29, 10, tzinfo=timezone.utc)
        first = self.email()
        first.add_attachment(b"\x00\x01original", maintype="image", subtype="png", filename="pic.png")
        one = ingest(self.root, first.as_bytes(), "gmail:one", time)
        replay = ingest(self.root, first.as_bytes(), "gmail:one", time)
        two = ingest(self.root, self.email(body="Second note", message_id="<two@example.com>").as_bytes(), "gmail:two", time)
        self.assertEqual(one["submission_id"], replay["submission_id"])
        self.assertEqual(two["submission_id"], "rowan-002")
        self.assertEqual((self.root / "issues/2026-W40/originals/rowan-001.eml").read_bytes(), first.as_bytes())
        self.assertEqual((self.root / "issues/2026-W40/attachments" / one["attachments"][0]["filename"]).read_bytes(), b"\x00\x01original")
        self.assertEqual(one["submitted_links"], ["https://example.com/story"])
        bundle = build_issue(self.root, "2026-W40").read_text(encoding="utf-8")
        self.assertIn("### Submission rowan-001", bundle)
        self.assertIn("### Submission rowan-002", bundle)

    def test_review_flags_preserve_sources(self):
        time = datetime(2026, 9, 29, 10, tzinfo=timezone.utc)
        one = ingest(self.root, self.email(body="First").as_bytes(), "gmail:a", time)
        duplicate = ingest(self.root, self.email(body="Other").as_bytes(), "gmail:b", time)
        unknown = ingest(self.root, self.email(sender="other@example.com", body="https://bad").as_bytes(), "gmail:c", time)
        empty = ingest(self.root, self.email(body="", message_id="<empty@example.com>").as_bytes(), "gmail:d", time)
        self.assertIn("duplicate_message_id", duplicate["flags"])
        self.assertIn("unknown_sender", unknown["flags"])
        self.assertIn("malformed_url", unknown["flags"])
        self.assertIn("empty_submission", empty["flags"])
        self.assertTrue((self.root / "issues/2026-W40/originals/unknown-001.eml").exists())
        bundle = build_issue(self.root, "2026-W40").read_text(encoding="utf-8")
        self.assertIn("### Submission rowan-001", bundle)
        self.assertIn("## Held for review", bundle)
        self.assertNotIn("### Submission rowan-002", bundle)

    def test_test_subject_is_archived_but_held(self):
        time = datetime(2026, 9, 29, 10, tzinfo=timezone.utc)
        sample = self.email(body="Trial story https://example.com/trial")
        sample.replace_header("Subject", "[TEST] Trial submission")
        held = ingest(self.root, sample.as_bytes(), "gmail:trial", time)
        self.assertIn("test_submission", held["flags"])
        self.assertTrue((self.root / "issues/2026-W40/originals/rowan-001.eml").exists())
        self.assertTrue((self.root / "issues/2026-W40/submissions/rowan-001.md").exists())
        normal = self.email(body="Real story", message_id="<normal@example.com>")
        included = ingest(self.root, normal.as_bytes(), "gmail:normal", time)
        bundle = build_issue(self.root, "2026-W40").read_text(encoding="utf-8")
        self.assertNotIn("### Submission " + held["submission_id"], bundle)
        self.assertIn("### Submission " + included["submission_id"], bundle)
        self.assertIn("rowan-001: test_submission", bundle)

    def test_administrative_flag_is_held_out_of_frozen_editor_input(self):
        self.schedule["first_issue_week"] = "2026-W40"
        (self.root / "config/schedule.json").write_text(json.dumps(self.schedule), encoding="utf-8")
        received = datetime(2026, 9, 29, 10, tzinfo=timezone.utc)
        normal = ingest(self.root, self.email(body="Real story").as_bytes(), "gmail:real", received)
        admin = ingest(self.root, self.email(body="Private admin note", message_id="<admin@example.com>").as_bytes(),
                       "gmail:admin", received)
        metadata_path = self.root / "issues/2026-W40/submissions" / f"{admin['submission_id']}.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["flags"].append("administrative_email")
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        frozen_path = finalize_due_issues(self.root, datetime(2026, 10, 4, 18, tzinfo=timezone.utc))[0]
        frozen = frozen_path.read_text(encoding="utf-8")
        self.assertIn(f"### Submission {normal['submission_id']}", frozen)
        self.assertNotIn("Private admin note", frozen)
        self.assertIn(f"{admin['submission_id']}: administrative_email", frozen)
        eligible, _ = submissions(self.root, "2026-W40")
        self.assertEqual([record["submission_id"] for record in eligible], [normal["submission_id"]])

    def test_example_relay_uses_subject_name_without_erasing_real_sender(self):
        (self.root / "config/relays.json").write_text(json.dumps({"relays": {
            "relay@example.com": {"name": "Example relay"}
        }}), encoding="utf-8")
        time = datetime(2026, 9, 29, 10, tzinfo=timezone.utc)
        forward = self.email(sender="Example relay <relay@example.com>",
                             body="Original submission https://example.com/story")
        forward.replace_header("Subject", "Circus submission - Rowan")
        record = ingest(self.root, forward.as_bytes(), "gmail:relay-1", time)
        self.assertEqual(record["member_id"], "rowan")
        self.assertEqual(record["sender_email"], "relay@example.com")
        self.assertEqual(record["claimed_contributor"], "Rowan")
        self.assertEqual(record["source_kind"], "relay")
        self.assertIn("relayed_submission", record["flags"])
        self.assertNotIn("unknown_sender", record["flags"])
        self.assertEqual((self.root / "issues/2026-W40/originals/rowan-001.eml").read_bytes(),
                         forward.as_bytes())
        markdown = (self.root / "issues/2026-W40/submissions/rowan-001.md").read_text(encoding="utf-8")
        self.assertIn('relayed_by: "Example relay"', markdown)
        bundle = build_issue(self.root, "2026-W40").read_text(encoding="utf-8")
        self.assertIn("### Submission rowan-001", bundle)
        self.assertIn("Relay: Example relay", bundle)

        trial = self.email(sender="relay@example.com", message_id="<relay-test@example.com>")
        trial.replace_header("Subject", "[TEST] [Circus forward: Rowan] Trial")
        test_record = ingest(self.root, trial.as_bytes(), "gmail:relay-test", time)
        self.assertEqual(test_record["member_id"], "rowan")
        self.assertIn("test_submission", test_record["flags"])

        bare = self.email(sender="relay@example.com", message_id="<relay-bare@example.com>")
        bare.replace_header("Subject", "Rowan")
        bare_record = ingest(self.root, bare.as_bytes(), "gmail:relay-bare", time)
        self.assertEqual(bare_record["member_id"], "rowan")
        self.assertEqual(bare_record["claimed_contributor"], "Rowan")
        self.assertEqual(bare_record["sender_email"], "relay@example.com")

        unclear = self.email(sender="relay@example.com", message_id="<relay-2@example.com>")
        unclear.replace_header("Subject", "Fwd: Original story")
        held = ingest(self.root, unclear.as_bytes(), "gmail:relay-2", time)
        self.assertEqual(held["member_id"], "unknown")
        self.assertIn("unresolved_relay_identity", held["flags"])
        self.assertTrue((self.root / "issues/2026-W40/originals/unknown-001.eml").exists())
        bundle = build_issue(self.root, "2026-W40").read_text(encoding="utf-8")
        self.assertIn("unknown-001: relayed_submission, unresolved_relay_identity", bundle)

    def test_inline_image_and_unsupported_file_are_saved(self):
        time = datetime(2026, 9, 29, 10, tzinfo=timezone.utc)
        mail = self.email(body="An image")
        mail.add_related(b"inline-image", maintype="image", subtype="png", cid="photo")
        mail.add_attachment(b"binary-data", maintype="application", subtype="octet-stream", filename="notes.bin")
        record = ingest(self.root, mail.as_bytes(), "gmail:inline", time)
        self.assertEqual(len(record["attachments"]), 2)
        self.assertIn("unsupported_attachment_type", record["flags"])
        for attachment in record["attachments"]:
            self.assertTrue((self.root / "issues/2026-W40/attachments" / attachment["filename"]).exists())

    def test_interrupted_run_repairs_missing_bundle(self):
        time = datetime(2026, 9, 29, 10, tzinfo=timezone.utc)
        ingest(self.root, self.email().as_bytes(), "gmail:one", time)
        self.assertEqual(issues_needing_bundle(self.root), {"2026-W40"})
        build_issue(self.root, "2026-W40")
        self.assertEqual(issues_needing_bundle(self.root), set())

    def test_fixed_gmt_cutoff_and_frozen_final_bundle(self):
        self.schedule.update({"cutoff_timezone": "UTC", "cutoff_time": "23:00",
                              "first_issue_week": "2026-W40"})
        (self.root / "config/schedule.json").write_text(json.dumps(self.schedule), encoding="utf-8")
        before = datetime(2026, 10, 4, 22, 59, tzinfo=timezone.utc)
        cutoff = datetime(2026, 10, 4, 23, 0, tzinfo=timezone.utc)
        self.assertEqual(issue_for(before, self.schedule)[0], "2026-W40")
        self.assertEqual(issue_for(cutoff, self.schedule)[0], "2026-W41")
        first = ingest(self.root, self.email(body="Before cutoff").as_bytes(), "gmail:first", before)
        self.assertEqual(first["issue_cutoff"], "2026-10-04T23:00:00+00:00")
        self.assertEqual(finalize_due_issues(self.root, before), [])
        final_path = finalize_due_issues(self.root, cutoff)[0]
        snapshot = final_path.read_bytes()
        self.assertIn(b"Submission rowan-001", snapshot)
        late = ingest(self.root, self.email(body="Delivered late", message_id="<late@example.com>").as_bytes(),
                      "gmail:late", before)
        self.assertIn("after_finalization", late["flags"])
        build_issue(self.root, "2026-W40")
        self.assertEqual(final_path.read_bytes(), snapshot)
        self.assertEqual(finalize_due_issues(self.root, cutoff), [])

    def test_explicit_pronouns_reach_metadata_and_editor_bundle(self):
        (self.root / "config/roster.json").write_text(json.dumps({"members": {
            "rowan": {"name": "Rowan", "emails": ["rowan@example.com"], "pronouns": "he/him"}
        }}), encoding="utf-8")
        record = ingest(self.root, self.email().as_bytes(), "gmail:one",
                        datetime(2026, 9, 29, 10, tzinfo=timezone.utc))
        self.assertEqual(record["pronouns"], "he/him")
        markdown = (self.root / "issues/2026-W40/submissions/rowan-001.md").read_text(encoding="utf-8")
        self.assertIn('pronouns: "he/him"', markdown)
        self.assertIn("Editorial pronouns: he/him", build_issue(self.root, "2026-W40").read_text(encoding="utf-8"))

    def test_reply_history_patterns_are_trimmed_conservatively(self):
        gmail = ("My actual submission.\n\nOn Sat, 17 Oct 2026 at 18:00, "
                 "<newsletter@example.com> wrote:\n> Old reminder")
        apple = ("My actual submission.\n\n> On 12 Oct 2026, at 08:52, "
                 "newsletter@example.com wrote:\n> Old invitation")
        outlook = ("My actual submission.\n[cid:inline-photo]\n"
                   "________________________________\n"
                   "From: Sleepless <newsletter@example.com>\n"
                   "Sent: 17 October 2026 18:00\nTo: Rowan <rowan@example.com>\n"
                   "Subject: Circus reminder\n\nOld reminder")
        original = "My actual submission.\n\n-----Original Message-----\nFrom: old@example.com\nOld"
        for value in (gmail, apple, outlook, original):
            self.assertEqual(strip_reply_history(value), "My actual submission.")
        self.assertEqual(strip_reply_history("On balance, this is what I wrote: useful analysis"),
                         "On balance, this is what I wrote: useful analysis")
        forwarded = "My note\n\n---------- Forwarded message ---------\nOriginal contribution"
        self.assertEqual(strip_reply_history(forwarded), forwarded)

    def test_ingest_stores_only_new_reply_text_and_links(self):
        body = ("New note https://example.com/new\n\n"
                "On Sat, 17 Oct 2026 at 18:00, <newsletter@example.com> wrote:\n"
                "> Old reminder https://example.com/old")
        record = ingest(self.root, self.email(body=body).as_bytes(), "gmail:reply",
                        datetime(2026, 9, 29, 10, tzinfo=timezone.utc))
        markdown = (self.root / "issues/2026-W40" / record["parsed_markdown"]).read_text(encoding="utf-8")
        self.assertIn("New note https://example.com/new", markdown)
        self.assertNotIn("Old reminder", markdown)
        self.assertEqual(record["submitted_links"], ["https://example.com/new"])

    def test_existing_dirty_markdown_is_cleaned_for_bundle_without_rewriting_it(self):
        record = ingest(self.root, self.email(body="New note").as_bytes(), "gmail:dirty",
                        datetime(2026, 9, 29, 10, tzinfo=timezone.utc))
        path = self.root / "issues/2026-W40" / record["parsed_markdown"]
        dirty = path.read_text(encoding="utf-8").rstrip() + (
            "\n\nOn Sat, 17 Oct 2026 at 18:00, <newsletter@example.com> wrote:\n"
            "> Old reminder\n")
        path.write_text(dirty, encoding="utf-8")
        bundle = build_issue(self.root, "2026-W40").read_text(encoding="utf-8")
        self.assertNotIn("Old reminder", bundle)
        self.assertEqual(path.read_text(encoding="utf-8"), dirty)


if __name__ == "__main__":
    unittest.main()
