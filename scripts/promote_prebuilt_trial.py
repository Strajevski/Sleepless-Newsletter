"""Atomically promote a verified local trial to an undelivered NAS issue."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.settings import data_directory

from circus_newsletter.editor import submissions, validate_draft
from manual_ops import _pause_tasks


ARTIFACTS = (
    "editor-raw.json", "editor-result.json", "issue-data.json", "issue-email.html",
    "issue-preview.html", "issue.txt", "issue-draft.eml", "image-captions.json",
    "submitted-text.txt",
)
PRESERVED_ARTIFACTS = ARTIFACTS + ("editor-status.json",)
MAX_BYTES = 20 * 1024 * 1024


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".promotion-", dir=destination.parent)
    os.close(handle)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_json(path: Path, value: dict) -> None:
    handle, temporary = tempfile.mkstemp(prefix=".promotion-", dir=path.parent)
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issue")
    parser.add_argument("--source", type=Path, required=True, help="Trial project root")
    parser.add_argument("--target", type=Path, required=True, help="Live NAS project root")
    parser.add_argument("--release", action="store_true", help="Release this issue's delivery hold")
    args = parser.parse_args()

    source = args.source.resolve()
    target = args.target.resolve()
    source_issue = data_directory(source) / "issues" / args.issue
    target_issue = data_directory(target) / "issues" / args.issue
    journal = target_issue / "outbound/issue/delivery.json"
    if journal.exists():
        raise RuntimeError("Issue delivery journal exists; refusing editorial promotion")

    records, _ = submissions(source, args.issue)
    raw = json.loads((source_issue / "editor-raw.json").read_text(encoding="utf-8"))
    validate_draft(raw, args.issue, records)
    manifest = json.loads((source_issue / "editor-result.json").read_text(encoding="utf-8"))
    draft = source_issue / "issue-draft.eml"
    if _sha(draft) != manifest.get("draft_sha256") or draft.stat().st_size > MAX_BYTES:
        raise RuntimeError("Source draft failed hash or size validation")
    if _sha(target_issue / "issue-input-final.md") != manifest.get("final_input_sha256"):
        raise RuntimeError("Live frozen input does not match the reviewed trial")
    for record in records:
        for attachment in record.get("attachments", []):
            name = attachment["filename"]
            if _sha(source_issue / "attachments" / name) != _sha(target_issue / "attachments" / name):
                raise RuntimeError(f"Live attachment differs from the trial: {name}")

    delivery_path = target / "config/delivery.json"
    delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
    if args.release and args.issue not in delivery.get("issue_holds", {}):
        raise RuntimeError("The live issue has no delivery hold to release")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = data_directory(target) / "revisions" / f"{args.issue}-before-prebuilt-promotion-{stamp}"
    backup.mkdir(parents=True)
    existed = {name for name in PRESERVED_ARTIFACTS if (target_issue / name).exists()}
    for name in existed:
        shutil.copy2(target_issue / name, backup / name)
    shutil.copy2(delivery_path, backup / "delivery.json")

    with _pause_tasks():
        if journal.exists():
            raise RuntimeError("Issue delivery began while preparing promotion")
        try:
            for name in ARTIFACTS:
                _atomic_copy(source_issue / name, target_issue / name)
            _atomic_json(target_issue / "editor-status.json", {"status": "complete", "mode": "ai"})
            if args.release:
                delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
                del delivery["issue_holds"][args.issue]
                _atomic_json(delivery_path, delivery)
            live_manifest = json.loads((target_issue / "editor-result.json").read_text(encoding="utf-8"))
            if (_sha(target_issue / "issue-draft.eml") != live_manifest.get("draft_sha256")
                    or _sha(target_issue / "issue-input-final.md") != live_manifest.get("final_input_sha256")
                    or (target_issue / "issue-draft.eml").stat().st_size > MAX_BYTES
                    or journal.exists()):
                raise RuntimeError("Promoted issue failed final integrity checks")
        except Exception:
            for name in PRESERVED_ARTIFACTS:
                destination = target_issue / name
                if name in existed:
                    _atomic_copy(backup / name, destination)
                elif destination.exists():
                    destination.unlink()
            _atomic_copy(backup / "delivery.json", delivery_path)
            raise

    _atomic_json(backup / "promotion.json", {
        "issue_week": args.issue,
        "source": str(source),
        "promoted_at": datetime.now(timezone.utc).isoformat(),
        "draft_sha256": manifest["draft_sha256"],
        "draft_bytes": draft.stat().st_size,
        "delivery_hold_released": args.release,
    })
    print(json.dumps({"status": "promoted", "issue_week": args.issue,
                      "draft_sha256": manifest["draft_sha256"],
                      "draft_bytes": draft.stat().st_size,
                      "delivery_hold_released": args.release,
                      "backup": str(backup)}, indent=2))


if __name__ == "__main__":
    main()
