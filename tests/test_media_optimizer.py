import hashlib
import io
import tempfile
import unittest
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

from PIL import Image

from circus_newsletter.media_optimizer import _photo, optimize


def png(size):
    image = Image.effect_noise(size, 80).convert("RGB")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


class MediaOptimizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.large = png((1200, 1200))
        self.small = png((400, 400))
        self.files = {}
        mail = EmailMessage()
        mail["From"] = "editor@example.com"
        mail["Subject"] = "Size trial"
        mail.set_content("The unchanged body")
        mail.add_alternative('<img src="cid:large@local"><img src="cid:small@local">', subtype="html")
        html = mail.get_payload()[-1]
        for name, data, cid in (("large.png", self.large, "large@local"),
                                ("small.png", self.small, "small@local")):
            path = self.root / name
            path.write_bytes(data)
            self.files[name] = path
            html.add_related(data, maintype="image", subtype="png", cid=f"<{cid}>", filename=name)
        self.draft = mail.as_bytes()

    def test_below_hard_limit_keeps_complete_draft_and_images(self):
        result = optimize(self.draft, self.files, self.root / "below")
        self.assertEqual(result["attempts"], [])
        self.assertEqual(result["original_draft_bytes"], result["optimized_draft_bytes"])
        self.assertTrue(all(row["status"] == "unchanged" for row in result["media"]))
        self.assertEqual(len(list((self.root / "below").glob("*.jpg"))), 0)

    def test_complete_outbound_size_controls_decision(self):
        draft_size = len(self.draft)
        result = optimize(self.draft, self.files, self.root / "outbound",
                          hard_limit=draft_size + 1000, target=draft_size,
                          measure_outbound=lambda serialized: len(serialized) + 2000)
        self.assertEqual(result["original_draft_bytes"], draft_size)
        self.assertEqual(result["original_outbound_bytes"], draft_size + 2000)
        self.assertEqual(result["attempts"][0]["filename"], "large.png")

    def test_largest_first_stops_at_target_and_preserves_originals(self):
        before = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for name, path in self.files.items()}
        original_size = len(self.draft)
        output = self.root / "large-first"
        result = optimize(self.draft, self.files, output,
                          hard_limit=original_size - 1, target=original_size - 1000)
        self.assertEqual([attempt["filename"] for attempt in result["attempts"]], ["large.png"])
        self.assertTrue(result["passes_target"])
        large = result["media"][0]
        self.assertEqual(large["status"], "optimized")
        self.assertEqual(large["original_dimensions"], [1200, 1200])
        self.assertEqual(large["generated_dimensions"], [1200, 1200])
        self.assertEqual(large["generated_mime"], "image/jpeg")
        self.assertIn("png_to_jpeg", large["operations"])
        generated = (output / large["generated_filename"]).read_bytes()
        self.assertEqual(large["generated_sha256"], hashlib.sha256(generated).hexdigest())
        self.assertEqual(large["generated_bytes"], len(generated))
        self.assertEqual(large["original_sha256"], before["large.png"])
        self.assertTrue(all(hashlib.sha256(path.read_bytes()).hexdigest() == before[name]
                            for name, path in self.files.items()))
        mail = BytesParser(policy=policy.default).parsebytes((output / "optimized-draft.eml").read_bytes())
        parts = {part.get_filename(): part for part in mail.walk() if part.get_filename()}
        self.assertEqual(parts["large.jpg"].get_payload(decode=True), generated)
        self.assertEqual(parts["large.jpg"]["Content-ID"], "<large@local>")
        self.assertEqual(parts["small.png"].get_payload(decode=True), self.small)

    def test_non_image_material_blocks_hard_limit_gracefully(self):
        mail = BytesParser(policy=policy.default).parsebytes(self.draft)
        mail.add_attachment(b"v" * 100_000, maintype="video", subtype="mp4", filename="clip.mp4")
        draft = mail.as_bytes()
        result = optimize(draft, self.files, self.root / "blocked", hard_limit=1000, target=800)
        self.assertFalse(result["passes_hard_limit"])
        self.assertIn("non-image", result["blocker"])
        final = BytesParser(policy=policy.default).parsebytes((self.root / "blocked/optimized-draft.eml").read_bytes())
        video = next(part for part in final.walk() if part.get_filename() == "clip.mp4")
        self.assertEqual(video.get_payload(decode=True), b"v" * 100_000)

    def test_opaque_alpha_png_is_safe_to_convert(self):
        image = Image.effect_noise((1200, 1200), 80).convert("RGBA")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        self.files["large.png"].write_bytes(stream.getvalue())
        mail = BytesParser(policy=policy.default).parsebytes(self.draft)
        part = next(part for part in mail.walk() if part.get_filename() == "large.png")
        part.set_content(stream.getvalue(), maintype="image", subtype="png", filename="large.png")
        result = optimize(mail.as_bytes(), self.files, self.root / "opaque",
                          hard_limit=1, target=0)
        row = next(row for row in result["media"] if row["original_filename"] == "large.png")
        self.assertEqual(row["status"], "optimized")

    def test_exif_orientation_precedes_resize(self):
        image = Image.effect_noise((2500, 1200), 80).convert("RGB")
        exif = Image.Exif()
        exif[274] = 6
        stream = io.BytesIO()
        image.save(stream, format="JPEG", quality=95, exif=exif)
        converted, mime, operations, dimensions = _photo(stream.getvalue(), "image/jpeg")
        self.assertEqual(mime, "image/jpeg")
        self.assertIn("exif_orientation", operations)
        self.assertIn("resize_max_2400", operations)
        self.assertEqual(dimensions, [1152, 2400])
        with Image.open(io.BytesIO(converted)) as result:
            self.assertEqual(result.size, (1152, 2400))


if __name__ == "__main__":
    unittest.main()
