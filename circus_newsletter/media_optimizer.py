"""Offline MIME image-size trial; no delivery integration."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps

HARD_LIMIT = 20 * 1024 * 1024
TARGET = int(18.5 * 1024 * 1024)
IMAGE_CAP = 2 * 1024 * 1024
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parts(message):
    return [part for part in message.walk() if part.get_filename()]


def _replace(part, data: bytes, filename: str, mime: str) -> None:
    part.set_payload(base64.encodebytes(data).decode("ascii"))
    part.set_type(mime)
    disposition = part.get_content_disposition() or "inline"
    part.replace_header("Content-Disposition", f'{disposition}; filename="{filename}"')


def _photo(data: bytes, mime: str) -> tuple[bytes, str, list[str], list[int]]:
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        oriented = ImageOps.exif_transpose(source)
        operations = []
        if oriented.size != source.size or source.getexif().get(274, 1) != 1:
            operations.append("exif_orientation")
        if max(oriented.size) > 2400:
            oriented.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
            operations.append("resize_max_2400")
        if mime == "image/png":
            operations.append("png_to_jpeg")
        else:
            operations.append("jpeg_reencode" if mime == "image/jpeg" else "webp_to_jpeg")
        if "A" in oriented.getbands() and oriented.getchannel("A").getextrema()[0] < 255:
            raise ValueError("image has transparency; automatic JPEG conversion would alter it")
        if oriented.mode == "P" and "transparency" in oriented.info:
            raise ValueError("image has transparency; automatic JPEG conversion would alter it")
        rgb = oriented.convert("RGB")
        result = io.BytesIO()
        rgb.save(result, format="JPEG", quality=85, optimize=True, progressive=True)
        encoded = result.getvalue()
        if len(encoded) > IMAGE_CAP:
            for quality in (80, 75, 70):
                result = io.BytesIO()
                rgb.save(result, format="JPEG", quality=quality, optimize=True, progressive=True)
                candidate = result.getvalue()
                if len(candidate) < len(encoded):
                    encoded = candidate
                    operations.append(f"quality_{quality}")
                if len(encoded) <= IMAGE_CAP:
                    break
        return encoded, "image/jpeg", operations, list(rgb.size)


def optimize(draft: bytes, originals: dict[str, Path], output: Path,
             *, hard_limit: int = HARD_LIMIT, target: int = TARGET,
             measure_outbound: Callable[[bytes], int] | None = None) -> dict:
    """Measure the full serialized MIME message after each image replacement."""
    if target > hard_limit:
        raise ValueError("Target must not exceed hard limit")
    message = BytesParser(policy=policy.default).parsebytes(draft)
    if message.defects:
        raise ValueError("Draft has MIME parsing defects")
    parts = _parts(message)
    rows = []
    for part in parts:
        name = part.get_filename()
        if name not in originals:
            continue
        if Path(name).name != name:
            raise ValueError(f"Unsafe attachment filename: {name}")
        original = originals[name].read_bytes()
        if part.get_payload(decode=True) != original:
            raise ValueError(f"Draft payload differs from archived original: {name}")
        dimensions = None
        if part.get_content_type().startswith("image/"):
            try:
                with Image.open(io.BytesIO(original)) as image:
                    dimensions = list(ImageOps.exif_transpose(image).size)
            except (OSError, ValueError):
                pass
        rows.append({"original_filename": name, "original_sha256": _sha(original),
                     "original_bytes": len(original), "original_dimensions": dimensions,
                     "original_mime": part.get_content_type(), "generated_filename": None,
                     "generated_sha256": None, "generated_bytes": None,
                     "generated_dimensions": None, "generated_mime": None,
                     "operations": [], "status": "unchanged"})
    if len(rows) != len(originals) or len({row["original_filename"] for row in rows}) != len(rows):
        raise ValueError("Archived originals do not match unique draft parts")

    original_size = len(message.as_bytes(policy=policy.default))
    measure = measure_outbound or len
    original_outbound_size = measure(message.as_bytes(policy=policy.default))
    current_size = original_outbound_size
    attempts = []
    if current_size > hard_limit:
        candidates = sorted((row for row in rows if row["original_mime"] in PHOTO_TYPES
                             and row["original_dimensions"]
                             and (row["original_bytes"] > IMAGE_CAP or
                                  (row["original_mime"] == "image/png" and
                                   row["original_bytes"] > 1024 * 1024) or
                                  max(row["original_dimensions"]) > 2400)),
                            key=lambda row: row["original_bytes"], reverse=True)
        for row in candidates:
            if current_size <= target:
                break
            name = row["original_filename"]
            part = next(part for part in parts if part.get_filename() == name)
            original = originals[name].read_bytes()
            try:
                converted, mime, operations, dimensions = _photo(original, row["original_mime"])
            except (OSError, ValueError) as exc:
                attempts.append({"filename": name, "outcome": "skipped", "reason": str(exc)})
                continue
            if len(converted) >= len(original):
                attempts.append({"filename": name, "outcome": "skipped", "reason": "re-encode did not save bytes"})
                continue
            generated_name = str(Path(name).with_suffix(".jpg"))
            if any(other is not part and other.get_filename() == generated_name for other in parts):
                raise ValueError(f"Generated filename collision: {generated_name}")
            _replace(part, converted, generated_name, mime)
            measured = measure(message.as_bytes(policy=policy.default))
            if measured >= current_size:
                _replace(part, original, name, row["original_mime"])
                attempts.append({"filename": name, "outcome": "skipped", "reason": "serialized email did not shrink"})
                continue
            output.mkdir(parents=True, exist_ok=True)
            (output / generated_name).write_bytes(converted)
            row.update(generated_filename=generated_name, generated_sha256=_sha(converted),
                       generated_bytes=len(converted), generated_dimensions=dimensions,
                       generated_mime=mime, operations=operations, status="optimized")
            attempts.append({"filename": name, "outcome": "optimized", "email_bytes": measured})
            current_size = measured

    final = message.as_bytes(policy=policy.default)
    final_outbound_size = measure(final)
    result = {"original_draft_bytes": original_size, "optimized_draft_bytes": len(final),
              "original_outbound_bytes": original_outbound_size,
              "optimized_outbound_bytes": final_outbound_size,
              "hard_limit_bytes": hard_limit, "target_bytes": target,
              "passes_target": final_outbound_size <= target,
              "passes_hard_limit": final_outbound_size <= hard_limit,
              "blocker": None if final_outbound_size <= hard_limit else
              "Image optimization could not bring the complete email below the hard limit; non-image material and preserved attachments require manual review.",
              "media": rows, "attempts": attempts,
              "original_draft_sha256": _sha(draft), "optimized_draft_sha256": _sha(final)}
    output.mkdir(parents=True, exist_ok=True)
    (output / "optimized-draft.eml").write_bytes(final)
    (output / "media-manifest.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def reconstruct_original(repaired: bytes, archive: Path, replacements: dict[str, str]) -> tuple[bytes, dict[str, Path]]:
    """Restore repaired MIME parts from archived originals in memory."""
    message = BytesParser(policy=policy.default).parsebytes(repaired)
    for part in _parts(message):
        repaired_name = part.get_filename()
        if repaired_name in replacements:
            name = replacements[repaired_name]
            path = archive / name
            if not path.is_file():
                raise FileNotFoundError(path)
            _replace(part, path.read_bytes(), name, "image/png")
    original = message.as_bytes(policy=policy.default)
    files = {part.get_filename(): archive / part.get_filename() for part in _parts(message)
             if part.get_content_type().startswith("image/")}
    if not all(path.is_file() for path in files.values()):
        raise FileNotFoundError("An archived image is missing")
    return original, files
