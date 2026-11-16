"""Prepare a size-safe outgoing issue draft without changing archived originals."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from circus_newsletter.settings import data_directory


HARD_LIMIT = 20 * 1024 * 1024


def optimized_draft(root: Path, issue: str, original: bytes) -> bytes | None:
    """Return a previously prepared draft only when it matches the current original."""
    directory = data_directory(root) / "issues" / issue / "media-optimization"
    manifest_path = directory / "media-manifest.json"
    draft_path = directory / "optimized-draft.eml"
    if not manifest_path.is_file() or not draft_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidate = draft_path.read_bytes()
    if (manifest.get("original_draft_sha256") != hashlib.sha256(original).hexdigest()
            or manifest.get("optimized_draft_sha256") != hashlib.sha256(candidate).hexdigest()
            or not manifest.get("passes_hard_limit")
            or len(candidate) > HARD_LIMIT):
        return None
    return candidate


def prepare_issue_media(root: Path, issue: str) -> str:
    """Shrink eligible outgoing images if the complete issue email exceeds the limit."""
    issue_dir = data_directory(root) / "issues" / issue
    result_path = issue_dir / "editor-result.json"
    if not result_path.is_file():
        return "not_ready"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    original = (issue_dir / "issue-draft.eml").read_bytes()
    if result["draft_sha256"] != hashlib.sha256(original).hexdigest():
        raise ValueError("Issue draft changed after editorial completion")
    if len(original) <= HARD_LIMIT:
        return "not_needed"
    if optimized_draft(root, issue, original) is not None:
        return "ready"

    from circus_newsletter.delivery import _message
    from circus_newsletter.editor import submissions
    from circus_newsletter.media_optimizer import optimize

    records, final_hash = submissions(root, issue)
    if final_hash != result["final_input_sha256"]:
        raise ValueError("Final editor input changed after drafting")
    originals = {attachment["filename"]: issue_dir / "attachments" / attachment["filename"]
                 for record in records for attachment in record["attachments"]
                 if attachment["content_type"].startswith("image/")}
    if not originals:
        return "too_large"

    def measure_outbound(draft: bytes) -> int:
        return len(_message(root, issue, "issue", "size-check",
                            "size-check@example.invalid", datetime.now(timezone.utc),
                            draft, {}))

    report = optimize(original, originals, issue_dir / "media-optimization",
                      measure_outbound=measure_outbound)
    return "ready" if report["passes_hard_limit"] else "too_large"
