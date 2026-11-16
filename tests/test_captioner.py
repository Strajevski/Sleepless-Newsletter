import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from circus_newsletter.captioner import caption_images, caption_index
from circus_newsletter.editor import gallery


ISSUE = "2026-W42"


def jpeg(size=(2400, 1200), orientation=None):
    image = Image.new("RGB", size, "#6b8f71")
    stream = io.BytesIO()
    exif = Image.Exif()
    if orientation:
        exif[274] = orientation
    image.save(stream, format="JPEG", quality=92, exif=exif)
    return stream.getvalue()


class CaptionerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / "prompts").mkdir()
        (self.root / "issues" / ISSUE / "attachments").mkdir(parents=True)
        project = Path(__file__).resolve().parents[1]
        shutil.copy2(project / "prompts/captions-v1.md", self.root / "prompts/captions-v1.md")
        shutil.copy2(project / "config/captioner-output.schema.json",
                     self.root / "config/captioner-output.schema.json")
        self.config = {
            "enabled": True, "model": "gpt-6-luna", "reasoning_effort": "low",
            "prompt": "prompts/captions-v1.md",
            "output_schema": "config/captioner-output.schema.json",
            "inspection_max_side": 1024, "inspection_jpeg_quality": 82,
            "max_input_characters": 40000, "timeout_seconds": 30,
        }
        self.image = jpeg(orientation=6)
        self.path = self.root / "issues" / ISSUE / "attachments/rowan-001-photo.jpg"
        self.path.write_bytes(self.image)
        self.records = [{
            "submission_id": "rowan-001", "display_name": "Rowan", "subject": "A roof update",
            "body": "The roof is finally finished.",
            "attachments": [{"filename": self.path.name, "content_type": "image/jpeg"}],
        }]
        self.issue_data = {
            "feed": [{"headline": "A finished roof", "text": "Rowan finished the roof.",
                      "source_ids": ["rowan-001"]},
                     {"headline": "Unrelated", "text": "This belongs to Casey.",
                      "source_ids": ["casey-001"]}],
            "worth_your_time": [], "personal_updates": [],
        }

    def test_no_images_makes_no_model_call(self):
        records = [{**self.records[0], "attachments": []}]
        with patch("circus_newsletter.captioner._codex_response") as call:
            manifest = caption_images(self.root, ISSUE, records, self.issue_data,
                                      config_override=self.config)
        call.assert_not_called()
        self.assertEqual(manifest["status"], "no_images")
        self.assertEqual(manifest["entries"], [])

    def test_context_is_scoped_and_original_is_preserved(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        captured = {}

        def response(_root, _config, prompt, candidates, _temporary):
            captured["prompt"] = prompt
            captured["candidate"] = candidates[0]
            return {"issue_week": ISSUE, "captions": [{
                "submission_id": "rowan-001", "filename": self.path.name,
                "alt_text": "Green roof panels viewed from below",
                "caption": "Rowan's newly finished roof.", "confidence": "high",
                "basis": ["visual", "submission", "issue"], "review_flags": [],
            }]}

        with patch("circus_newsletter.captioner._codex_response", side_effect=response):
            manifest = caption_images(self.root, ISSUE, self.records, self.issue_data,
                                      config_override=self.config)
        entry = manifest["entries"][0]
        self.assertTrue(entry["accepted"])
        self.assertEqual(entry["original_dimensions"], [2400, 1200])
        self.assertEqual(entry["inspection_dimensions"], [512, 1024])
        self.assertIn("Rowan finished the roof.", captured["prompt"])
        self.assertNotIn("This belongs to Casey.", captured["prompt"])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        self.assertEqual(entry["original_sha256"], before)

    def test_low_confidence_keeps_model_caption_and_review_flag(self):
        response = {"issue_week": ISSUE, "captions": [{
            "submission_id": "rowan-001", "filename": self.path.name,
            "alt_text": "A green surface", "caption": "Possibly a roof.",
            "confidence": "low", "basis": ["visual"], "review_flags": ["Ambiguous"],
        }]}
        with patch("circus_newsletter.captioner._codex_response", return_value=response):
            manifest = caption_images(self.root, ISSUE, self.records, self.issue_data,
                                      config_override=self.config)
        entry = manifest["entries"][0]
        self.assertTrue(entry["accepted"])
        self.assertEqual(entry["alt_text"], "A green surface")
        self.assertEqual(entry["caption"], "Possibly a roof.")
        self.assertEqual(entry["confidence"], "low")
        self.assertEqual(entry["review_flags"], ["Ambiguous"])
        self.assertEqual(caption_index(manifest)[("rowan-001", self.path.name)], entry)
        images, _ = gallery(self.root, ISSUE, self.records, caption_index(manifest))
        self.assertEqual(images[0]["caption"], "Possibly a roof.")

    def test_review_flag_keeps_model_caption(self):
        response = {"issue_week": ISSUE, "captions": [{
            "submission_id": "rowan-001", "filename": self.path.name,
            "alt_text": "Two people stand beside a roof",
            "caption": "Rowan and a friend beside the roof.", "confidence": "medium",
            "basis": ["visual", "submission"],
            "review_flags": ["The people are not identified"],
        }]}
        with patch("circus_newsletter.captioner._codex_response", return_value=response):
            manifest = caption_images(self.root, ISSUE, self.records, self.issue_data,
                                      config_override=self.config)
        self.assertTrue(manifest["entries"][0]["accepted"])
        self.assertEqual(manifest["entries"][0]["caption"],
                         "Rowan and a friend beside the roof.")
        self.assertEqual(manifest["entries"][0]["review_flags"],
                         ["The people are not identified"])

    def test_omitted_image_is_retried_alone_and_usage_is_combined(self):
        second = self.path.with_name("rowan-001-second.jpg")
        second.write_bytes(jpeg((800, 600)))
        self.records[0]["attachments"].append({"filename": second.name,
                                               "content_type": "image/jpeg"})
        calls = []

        def response(_root, _config, prompt, candidates, _temporary):
            calls.append(([item["context"]["filename"] for item in candidates], prompt))
            if len(calls) == 1:
                result = [{"submission_id": "rowan-001", "filename": self.path.name,
                           "alt_text": "Green panels", "caption": "The finished roof.",
                           "confidence": "high", "basis": ["visual"], "review_flags": []}]
            else:
                result = [{"submission_id": "rowan-001", "filename": second.name,
                           "alt_text": "A green roof", "caption": "The roof from another angle.",
                           "confidence": "low", "basis": ["visual"], "review_flags": []}]
            return ({"issue_week": ISSUE, "captions": result},
                    {"input_tokens": 100, "output_tokens": 20})

        with patch("circus_newsletter.captioner._codex_response", side_effect=response):
            manifest = caption_images(self.root, ISSUE, self.records, self.issue_data,
                                      config_override=self.config)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], [self.path.name, second.name])
        self.assertEqual(calls[1][0], [second.name])
        self.assertIn("This retry contains only those images", calls[1][1])
        self.assertEqual(manifest["retry_calls"], 1)
        self.assertEqual(manifest["retried_images"], 1)
        self.assertEqual(manifest["usage"], {"input_tokens": 200, "output_tokens": 40})
        self.assertTrue(all(entry["accepted"] for entry in manifest["entries"]))
        self.assertEqual(manifest["entries"][1]["confidence"], "low")

    def test_missing_model_text_still_uses_neutral_fallback(self):
        response = {"issue_week": ISSUE, "captions": [{
            "submission_id": "rowan-001", "filename": self.path.name,
            "alt_text": "", "caption": "Possibly a roof.",
            "confidence": "low", "basis": ["visual"],
            "review_flags": ["Ambiguous"],
        }]}
        with patch("circus_newsletter.captioner._codex_response", return_value=response):
            manifest = caption_images(self.root, ISSUE, self.records, self.issue_data,
                                      config_override=self.config)
        self.assertFalse(manifest["entries"][0]["accepted"])
        self.assertEqual(manifest["entries"][0]["caption"],
                         "Image from submission rowan-001")

    def test_human_caption_survives_model_failure(self):
        self.records[0]["attachments"][0]["caption"] = "My finished roof"
        with patch("circus_newsletter.captioner._codex_response", side_effect=RuntimeError("offline")):
            manifest = caption_images(self.root, ISSUE, self.records, self.issue_data,
                                      config_override=self.config)
        entry = manifest["entries"][0]
        self.assertTrue(entry["accepted"])
        self.assertEqual(entry["caption"], "My finished roof")
        images, _ = gallery(self.root, ISSUE, self.records, caption_index(manifest))
        self.assertEqual(images[0]["caption"], "My finished roof")

    def test_animated_gif_and_video_are_not_captioned(self):
        animation = io.BytesIO()
        frames = [Image.new("RGB", (20, 20), colour) for colour in ("red", "blue")]
        frames[0].save(animation, format="GIF", save_all=True, append_images=frames[1:], duration=50, loop=0)
        gif_path = self.path.with_name("rowan-001-animation.gif")
        gif_path.write_bytes(animation.getvalue())
        records = [{**self.records[0], "attachments": [
            {"filename": gif_path.name, "content_type": "image/gif"},
            {"filename": "clip.mp4", "content_type": "video/mp4"},
        ]}]
        with patch("circus_newsletter.captioner._codex_response") as call:
            manifest = caption_images(self.root, ISSUE, records, self.issue_data,
                                      config_override=self.config)
        call.assert_not_called()
        self.assertEqual(manifest["status"], "no_images")

    def test_matching_successful_manifest_is_reused(self):
        response = {"issue_week": ISSUE, "captions": [{
            "submission_id": "rowan-001", "filename": self.path.name,
            "alt_text": "Green roof panels", "caption": "The finished roof.",
            "confidence": "high", "basis": ["visual"], "review_flags": [],
        }]}
        with patch("circus_newsletter.captioner._codex_response", return_value=response) as call:
            first = caption_images(self.root, ISSUE, self.records, self.issue_data,
                                   config_override=self.config)
            second = caption_images(self.root, ISSUE, self.records, self.issue_data,
                                    config_override=self.config)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(first["input_sha256"], second["input_sha256"])

    def test_submission_context_is_not_duplicated_for_multiple_images(self):
        second = self.path.with_name("rowan-001-second.jpg")
        second.write_bytes(jpeg((800, 600)))
        self.records[0]["attachments"].append({"filename": second.name, "content_type": "image/jpeg"})
        captured = {}

        def response(_root, _config, prompt, _candidates, _temporary):
            captured["prompt"] = prompt
            return {"issue_week": ISSUE, "captions": [
                {"submission_id": "rowan-001", "filename": name,
                 "alt_text": "A green surface", "caption": "A roof update.",
                 "confidence": "medium", "basis": ["visual", "submission"], "review_flags": []}
                for name in (self.path.name, second.name)
            ]}

        with patch("circus_newsletter.captioner._codex_response", side_effect=response):
            caption_images(self.root, ISSUE, self.records, self.issue_data,
                           config_override=self.config)
        material = captured["prompt"].split("## Captioning material", 1)[1]
        self.assertEqual(material.count('"cleaned_submission"'), 1)
        self.assertEqual(material.count('"image_number"'), 2)

    def test_disabled_captioner_leaves_no_artifact(self):
        manifest = caption_images(self.root, ISSUE, self.records, self.issue_data,
                                  config_override={"enabled": False})
        self.assertIsNone(manifest)
        self.assertFalse((self.root / "issues" / ISSUE / "image-captions.json").exists())

    def test_codex_usage_is_preserved_in_manifest(self):
        response = {"issue_week": ISSUE, "captions": [{
            "submission_id": "rowan-001", "filename": self.path.name,
            "alt_text": "Green roof panels", "caption": "The finished roof.",
            "confidence": "high", "basis": ["visual"], "review_flags": [],
        }]}
        usage = {"input_tokens": 1200, "cached_input_tokens": 300,
                 "output_tokens": 90}
        with patch("circus_newsletter.captioner._codex_response", return_value=(response, usage)):
            manifest = caption_images(self.root, ISSUE, self.records, self.issue_data,
                                      config_override=self.config)
        self.assertEqual(manifest["usage"], usage)


if __name__ == "__main__":
    unittest.main()
