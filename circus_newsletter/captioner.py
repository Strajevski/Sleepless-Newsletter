"""Optional, non-blocking vision captions for newsletter gallery images."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from circus_newsletter.settings import data_directory
from circus_newsletter.settings import load_settings, prompt_text, codex_executable, codex_environment


STILL_IMAGE_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}
CONFIDENCE = {"high", "medium", "low"}
BASIS = {"visual", "submission", "issue"}
LIMIT_ERROR = re.compile(r"rate.?limit|usage.?limit|quota|too many requests|\b429\b", re.I)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _cli_error(stdout: str, stderr: str) -> str:
    messages = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") not in {"error", "turn.failed"}:
            continue
        detail = event.get("error") or event.get("message") or ""
        messages.append(detail.get("message", "") if isinstance(detail, dict) else str(detail))
    text = " ".join(message.strip() for message in messages if message.strip())
    if not text:
        text = stderr.strip().splitlines()[-1] if stderr.strip() else "unknown CLI error"
    return text[:500]


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".circus-caption-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _config(root: Path) -> dict[str, Any]:
    path = root / "config/captioner.json"
    if not path.exists():
        return {"enabled": False}
    return json.loads(path.read_text(encoding="utf-8"))


def _issue_context(data: dict[str, Any], source_id: str) -> list[dict[str, Any]]:
    relevant = []
    for section in ("feed", "worth_your_time", "personal_updates"):
        for item in data.get(section, []):
            if source_id not in item.get("source_ids", []):
                continue
            excerpt = {key: item[key] for key in ("headline", "text", "label", "note", "url", "name")
                       if isinstance(item.get(key), str) and item[key].strip()}
            relevant.append({"section": section, **excerpt})
    return relevant


def _human_caption(attachment: dict[str, Any]) -> str | None:
    for key in ("caption", "description"):
        value = attachment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _model_context(issue: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    submissions: dict[str, dict[str, Any]] = {}
    images = []
    for candidate in candidates:
        item = candidate["context"]
        submissions.setdefault(item["submission_id"], {
            "submission_id": item["submission_id"],
            "display_name": item["display_name"],
            "subject": item["subject"],
            "cleaned_submission": item["cleaned_submission"],
            "relevant_final_issue_items": item["relevant_final_issue_items"],
        })
        images.append({key: item[key] for key in
                       ("image_number", "filename", "submission_id", "human_caption")})
    return {"issue_week": issue, "submissions": list(submissions.values()), "images": images}


def _prepare_candidate(attachments_dir: Path, record: dict[str, Any],
                       attachment: dict[str, Any], number: int, temporary: Path,
                       config: dict[str, Any], issue_data: dict[str, Any]) -> dict[str, Any] | None:
    if attachment.get("content_type") not in STILL_IMAGE_MIME:
        return None
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise RuntimeError("Pillow is unavailable") from exc

    source = attachments_dir / attachment["filename"]
    try:
        with Image.open(source) as opened:
            if attachment["content_type"] == "image/gif" and getattr(opened, "is_animated", False):
                return None
            opened.seek(0)
            original_dimensions = [opened.width, opened.height]
            image = ImageOps.exif_transpose(opened)
            maximum = int(config.get("inspection_max_side", 1024))
            image.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
            inspection_dimensions = [image.width, image.height]
            if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                background.alpha_composite(rgba)
                image = background.convert("RGB")
            else:
                image = image.convert("RGB")
            inspection = temporary / f"image-{number:03d}.jpg"
            image.save(inspection, format="JPEG", quality=int(config.get("inspection_jpeg_quality", 82)),
                       optimize=True)
    except (OSError, UnidentifiedImageError):
        return None

    source_bytes = source.read_bytes()
    context = {
        "image_number": number,
        "filename": attachment["filename"],
        "submission_id": record["submission_id"],
        "display_name": record["display_name"],
        "subject": record.get("subject", ""),
        "cleaned_submission": record.get("body", ""),
        "relevant_final_issue_items": _issue_context(issue_data, record["submission_id"]),
        "human_caption": _human_caption(attachment),
    }
    return {
        "key": (record["submission_id"], attachment["filename"]),
        "source": source,
        "inspection": inspection,
        "context": context,
        "original_sha256": _sha256(source_bytes),
        "original_bytes": len(source_bytes),
        "original_dimensions": original_dimensions,
        "inspection_sha256": _sha256(inspection.read_bytes()),
        "inspection_bytes": inspection.stat().st_size,
        "inspection_dimensions": inspection_dimensions,
        "mime_type": attachment["content_type"],
    }


def _fallback_entry(candidate: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
    context = candidate["context"]
    flags = [reason] if reason else []
    return {
        "submission_id": context["submission_id"],
        "filename": context["filename"],
        "alt_text": f"Image submitted by {context['display_name']}",
        "caption": context["human_caption"] or f"Image from submission {context['submission_id']}",
        "confidence": "low",
        "basis": ["submission"] if context["human_caption"] else [],
        "review_flags": flags,
        "accepted": bool(context["human_caption"]),
        "source": "human" if context["human_caption"] else "fallback",
        "original_sha256": candidate["original_sha256"],
        "original_bytes": candidate["original_bytes"],
        "original_dimensions": candidate["original_dimensions"],
        "mime_type": candidate["mime_type"],
        "inspection_sha256": candidate["inspection_sha256"],
        "inspection_bytes": candidate["inspection_bytes"],
        "inspection_dimensions": candidate["inspection_dimensions"],
    }


def _validate_response(response: dict[str, Any], issue: str,
                       candidates: list[dict[str, Any]], config: dict[str, Any] | None = None
                       ) -> tuple[list[dict[str, Any]], list[str]]:
    config = config or {}
    if not isinstance(response, dict) or response.get("issue_week") != issue:
        return ([_fallback_entry(item, "Captioner returned the wrong issue week")
                 for item in candidates], ["Captioner returned the wrong issue week"])
    raw = response.get("captions")
    if not isinstance(raw, list):
        return ([_fallback_entry(item, "Captioner returned no caption list")
                 for item in candidates], ["Captioner returned no caption list"])

    expected = {item["key"]: item for item in candidates}
    by_key: dict[tuple[str, str], dict[str, Any] | None] = {}
    global_flags = []
    for value in raw:
        if not isinstance(value, dict):
            global_flags.append("Captioner returned a non-object caption")
            continue
        key = (value.get("submission_id"), value.get("filename"))
        if key not in expected:
            global_flags.append("Captioner returned an unknown image")
            continue
        if key in by_key:
            by_key[key] = None
            global_flags.append(f"Captioner duplicated {key[1]}")
        else:
            by_key[key] = value

    entries = []
    for key, candidate in expected.items():
        value = by_key.get(key)
        problem = None
        if not isinstance(value, dict):
            problem = "Caption is missing or duplicated"
        elif not isinstance(value.get("alt_text"), str) or not value["alt_text"].strip():
            problem = "Caption has no alt text"
        elif not isinstance(value.get("caption"), str) or not value["caption"].strip():
            problem = "Caption text is empty"
        elif value.get("confidence") not in CONFIDENCE:
            problem = "Caption confidence is invalid"
        elif not isinstance(value.get("basis"), list) or not set(value["basis"]).issubset(BASIS):
            problem = "Caption evidence basis is invalid"
        elif not isinstance(value.get("review_flags"), list) or not all(
                isinstance(flag, str) for flag in value["review_flags"]):
            problem = "Caption review flags are invalid"
        elif (len(value["alt_text"].strip()) > int(config.get("max_alt_characters", 320))
              or len(value["caption"].strip()) > int(config.get("max_caption_characters", 240))):
            problem = "Caption exceeds the configured length limit"
        if problem:
            entries.append(_fallback_entry(candidate, problem))
            continue
        context = candidate["context"]
        entries.append({
            "submission_id": key[0], "filename": key[1],
            "alt_text": value["alt_text"].strip(),
            "caption": context["human_caption"] or value["caption"].strip(),
            "confidence": value["confidence"], "basis": value["basis"],
            "review_flags": value["review_flags"], "accepted": True,
            "source": "human+ai-alt" if context["human_caption"] else "ai",
            "original_sha256": candidate["original_sha256"],
            "original_bytes": candidate["original_bytes"],
            "original_dimensions": candidate["original_dimensions"],
            "mime_type": candidate["mime_type"],
            "inspection_sha256": candidate["inspection_sha256"],
            "inspection_bytes": candidate["inspection_bytes"],
            "inspection_dimensions": candidate["inspection_dimensions"],
        })
    return entries, global_flags


def _missing_candidates(response: dict[str, Any], issue: str,
                        candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only images omitted or duplicated in an otherwise usable response."""
    if (not isinstance(response, dict) or response.get("issue_week") != issue
            or not isinstance(response.get("captions"), list)):
        return []
    expected = {item["key"]: item for item in candidates}
    counts: dict[tuple[str, str], int] = {}
    for value in response["captions"]:
        if not isinstance(value, dict):
            continue
        key = (value.get("submission_id"), value.get("filename"))
        if key in expected:
            counts[key] = counts.get(key, 0) + 1
    return [candidate for key, candidate in expected.items() if counts.get(key, 0) != 1]


def _combined_usage(*usages: dict[str, Any] | None) -> dict[str, Any] | None:
    present = [usage for usage in usages if isinstance(usage, dict)]
    if not present:
        return None
    combined: dict[str, Any] = {}
    for usage in present:
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                combined[key] = combined.get(key, 0) + value
            elif key not in combined:
                combined[key] = value
    return combined


def _codex_response(root: Path, config: dict[str, Any], prompt: str,
                    candidates: list[dict[str, Any]], temporary: Path
                    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    executable = codex_executable(root)
    if not executable:
        raise RuntimeError("Codex CLI is unavailable")
    output = temporary / "captions.json"
    command = [executable, "exec", "--model", config["model"],
               "-c", f"model_reasoning_effort={config.get('reasoning_effort', 'low')}",
               "--sandbox", "read-only", "--ignore-user-config", "--ignore-rules",
               "--skip-git-repo-check", "--ephemeral", "--json",
               "--output-schema", str(root / config["output_schema"]),
               "--output-last-message", str(output)]
    for candidate in candidates:
        command.extend(["--image", str(candidate["inspection"])])
    command.append("-")
    environment = codex_environment(root)
    try:
        result = subprocess.run(command, input=prompt, text=True, encoding="utf-8",
                                capture_output=True, cwd=temporary,
                                timeout=int(config.get("timeout_seconds", 180)), check=False,
                                env=environment)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Captioner timed out") from exc
    if result.returncode:
        combined = result.stderr + "\n" + result.stdout
        reason = ("Captioner usage limit reached" if LIMIT_ERROR.search(combined)
                  else f"Codex captioner failed: {_cli_error(result.stdout, result.stderr)}")
        raise RuntimeError(reason)
    if not output.exists():
        raise RuntimeError("Captioner returned no output")
    try:
        response = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Captioner returned invalid JSON") from exc
    usage = None
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
    return response, usage


def caption_images(root: Path, issue: str, records: list[dict[str, Any]],
                   issue_data: dict[str, Any], *, config_override: dict[str, Any] | None = None,
                   manifest_path: Path | None = None, attachments_dir: Path | None = None,
                   force: bool = False) -> dict[str, Any] | None:
    """Caption eligible images without ever making draft generation depend on AI vision."""
    try:
        config = dict(config_override) if config_override is not None else _config(root)
    except Exception as exc:
        logging.warning("Image caption configuration failed for %s: %s", issue, exc)
        return None
    if not config.get("enabled", False):
        return None
    destination = manifest_path or data_directory(root) / "issues" / issue / "image-captions.json"
    source_directory = attachments_dir or data_directory(root) / "issues" / issue / "attachments"
    created_at = datetime.now(timezone.utc).isoformat()
    candidates: list[dict[str, Any]] = []

    try:
        prompt_path = root / config["prompt"]
        prompt_template = prompt_text(prompt_path.read_text(encoding="utf-8"), load_settings(root))
        with tempfile.TemporaryDirectory(prefix="circus-captioner-") as name:
            temporary = Path(name)
            for record in records:
                for attachment in record.get("attachments", []):
                    candidate = _prepare_candidate(source_directory, record, attachment, len(candidates) + 1,
                                                   temporary, config, issue_data)
                    if candidate:
                        candidates.append(candidate)
            if not candidates:
                manifest = {"issue_week": issue, "status": "no_images", "created_at": created_at,
                            "model": None, "input_sha256": None, "entries": [], "review_flags": []}
                _atomic_json(destination, manifest)
                return manifest

            context = _model_context(issue, candidates)
            context_json = json.dumps(context, ensure_ascii=False, indent=2)
            maximum = int(config.get("max_input_characters", 40000))
            if len(context_json) > maximum:
                raise RuntimeError("Caption context exceeds the configured size limit")
            prompt = (prompt_template + "\n\n## Captioning material\n\n" + context_json
                      + "\n\nThe attached images are in image_number order. Return only the requested JSON.\n")
            input_hash = _sha256(json.dumps({
                "model": config["model"], "reasoning_effort": config.get("reasoning_effort", "low"),
                "prompt_sha256": _sha256(prompt_template.encode("utf-8")),
                "context": context,
                "images": [item["original_sha256"] for item in candidates],
                "inspection_images": [item["inspection_sha256"] for item in candidates],
                "validation": {
                    "max_alt_characters": config.get("max_alt_characters", 320),
                    "max_caption_characters": config.get("max_caption_characters", 240),
                    "allow_uncertain_results": True,
                    "missing_caption_retry_attempts": 1,
                },
            }, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            if not force and destination.exists():
                cached = json.loads(destination.read_text(encoding="utf-8"))
                if cached.get("status") == "complete" and cached.get("input_sha256") == input_hash:
                    return cached
            result = _codex_response(root, config, prompt, candidates, temporary)
            response, usage = result if isinstance(result, tuple) else (result, None)
            entries, flags = _validate_response(response, issue, candidates, config)
            retry_candidates = _missing_candidates(response, issue, candidates)
            retry_calls = 0
            retried_images = 0
            retry_failures: list[str] = []
            if retry_candidates:
                retry_calls = 1
                retried_images = len(retry_candidates)
                retry_prompt = (prompt + "\nThe previous response omitted or duplicated some image entries. "
                                "This retry contains only those images. Return exactly one valid result "
                                "for every supplied image, in image-number order. If uncertain, provide "
                                "your best supported description and mark confidence low or add a review flag.\n")
                try:
                    retry_result = _codex_response(
                        root, config, retry_prompt, retry_candidates, temporary)
                    retry_response, retry_usage = (retry_result if isinstance(retry_result, tuple)
                                                   else (retry_result, None))
                    retry_entries, retry_flags = _validate_response(
                        retry_response, issue, retry_candidates, config)
                    retry_by_key = {(item["submission_id"], item["filename"]): item
                                    for item in retry_entries}
                    entries = [retry_by_key.get((item["submission_id"], item["filename"]), item)
                               for item in entries]
                    flags.extend(retry_flags)
                    usage = _combined_usage(usage, retry_usage)
                except Exception as exc:
                    reason = str(exc) if str(exc) else type(exc).__name__
                    retry_failures.append(reason)
                    flags.append(f"Missing-caption retry failed: {reason}")
            manifest = {"issue_week": issue, "status": "complete", "created_at": created_at,
                        "model": config["model"], "reasoning_effort": config.get("reasoning_effort", "low"),
                        "prompt": config["prompt"], "prompt_sha256": _sha256(prompt_template.encode("utf-8")),
                        "input_sha256": input_hash, "usage": usage,
                        "retry_calls": retry_calls, "retried_images": retried_images,
                        "retry_failures": retry_failures,
                        "entries": entries, "review_flags": flags}
    except Exception as exc:
        logging.warning("Image captioning fell back for %s: %s", issue, exc)
        reason = str(exc) if str(exc) else type(exc).__name__
        manifest = {"issue_week": issue, "status": "fallback", "created_at": created_at,
                    "model": config.get("model"), "input_sha256": None, "entries": [],
                    "review_flags": [reason]}
        if candidates:
            manifest["entries"] = [_fallback_entry(item, reason) for item in candidates]
    _atomic_json(destination, manifest)
    return manifest


def caption_index(manifest: dict[str, Any] | None) -> dict[tuple[str, str], dict[str, Any]]:
    if not manifest:
        return {}
    return {(entry["submission_id"], entry["filename"]): entry
            for entry in manifest.get("entries", []) if entry.get("accepted")}
