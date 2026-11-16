"""Rebuild a trial issue after a reviewed manual edit, without another AI call."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.settings import data_directory

from circus_newsletter.editor import (issue_from_draft, publish_draft, submissions,
                                      validate_draft)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trial_root", type=Path)
    parser.add_argument("issue")
    args = parser.parse_args()

    root = args.trial_root.resolve()
    issue_dir = data_directory(root) / "issues" / args.issue
    records, final_hash = submissions(root, args.issue)
    raw_path = issue_dir / "editor-raw.json"
    captions_path = issue_dir / "image-captions.json"
    draft = json.loads(raw_path.read_text(encoding="utf-8"))
    captions = json.loads(captions_path.read_text(encoding="utf-8"))
    validate_draft(draft, args.issue, records)
    if captions.get("issue_week") != args.issue or captions.get("status") != "complete":
        raise ValueError("A complete caption manifest for this issue is required")

    publish_draft(root, args.issue, issue_from_draft(draft, records), records,
                  "ai", None, final_hash, caption_manifest_override=captions)
    draft_path = issue_dir / "issue-draft.eml"
    report_path = data_directory(root) / "trial-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update({
        "manually_edited": True,
        "rerendered_at": datetime.now(timezone.utc).isoformat(),
        "editor_raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "draft_sha256": hashlib.sha256(draft_path.read_bytes()).hexdigest(),
        "draft_bytes": draft_path.stat().st_size,
        "captions_reused_without_model_call": True,
    })
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
