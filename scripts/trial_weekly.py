"""Build an isolated issue trial from an existing frozen issue."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.settings import data_directory

from circus_newsletter.editor import (codex_draft_with_usage, editorial_input,
                                      issue_from_draft, previous_issue_summaries,
                                      publish_draft, submissions,
                                      validate_draft)


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _stage(project: Path, data: Path, output: Path, issue: str, *,
           skip_captioner: bool = False) -> None:
    source_issue = data_directory(data) / "issues" / issue
    if output.exists():
        raise FileExistsError(f"Trial output already exists: {output}")
    for directory in ("config", "prompts", "templates"):
        shutil.copytree(project / directory, output / directory)
    settings_path = output / "config/settings.json"
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        settings["storage"] = {"data_directory": "."}
        # A trial is a staged copy, not a new installation. Preserve the original
        # locations of explicit tool/login overrides when its config is copied.
        for key in ("executable", "home_directory"):
            value = settings.get("codex", {}).get(key)
            if value is not None:
                path = Path(value).expanduser()
                settings["codex"][key] = str((project / path).resolve() if not path.is_absolute() else path)
        settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name in ("roster.json", "schedule.json"):
        _copy(data / "config" / name, output / "config" / name)
    destination_issue = output / "issues" / issue
    destination_issue.mkdir(parents=True)
    for directory in ("attachments", "submissions"):
        shutil.copytree(source_issue / directory, destination_issue / directory)
    _copy(source_issue / "issue-input-final.md", destination_issue / "issue-input-final.md")
    for past in previous_issue_summaries(data, issue):
        past_week = past["issue_week"]
        for name in ("issue-data.json", "outbound/issue/delivery.json"):
            _copy(data_directory(data) / "issues" / past_week / name,
                  output / "issues" / past_week / name)
    caption_config = json.loads((output / "config/captioner.json").read_text(encoding="utf-8"))
    caption_config["enabled"] = not skip_captioner
    (output / "config/captioner.json").write_text(
        json.dumps(caption_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issue")
    parser.add_argument("--root", type=Path, required=True,
                        help="Read-only data root containing the frozen issue")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skip-captioner", action="store_true",
                        help="Skip the vision call; use neutral Gallery attribution")
    args = parser.parse_args()

    project = Path(__file__).resolve().parents[1]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = (args.output or data_directory(project) / "output" / f"{args.issue}-cleaned-trial-{stamp}").resolve()
    _stage(project, args.root.resolve(), output, args.issue,
           skip_captioner=args.skip_captioner)

    records, final_hash = submissions(output, args.issue)
    editor_characters = len(editorial_input(output, args.issue, records))
    draft, editor_usage = codex_draft_with_usage(output, args.issue, records)
    validate_draft(draft, args.issue, records)
    issue_data = issue_from_draft(draft, records)
    issue_dir = output / "issues" / args.issue
    (issue_dir / "editor-raw.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    publish_draft(output, args.issue, issue_data, records, "ai", None, final_hash)
    caption_manifest_path = issue_dir / "image-captions.json"
    caption_manifest = (json.loads(caption_manifest_path.read_text(encoding="utf-8"))
                        if caption_manifest_path.exists() else None)
    report = {
        "issue_week": args.issue,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(args.root.resolve()),
        "output_root": str(output),
        "editor_input_characters": editor_characters,
        "editor_usage": editor_usage,
        "captioner_usage": caption_manifest.get("usage") if caption_manifest else None,
        "captioner_status": caption_manifest.get("status") if caption_manifest else "disabled",
        "accepted_captions": sum(bool(item.get("accepted"))
                                  for item in caption_manifest.get("entries", []))
                             if caption_manifest else 0,
        "caption_candidates": len(caption_manifest.get("entries", []))
                              if caption_manifest else 0,
        "captioner_retry_calls": caption_manifest.get("retry_calls", 0)
                                 if caption_manifest else 0,
        "captioner_retried_images": caption_manifest.get("retried_images", 0)
                                    if caption_manifest else 0,
        "preview": str(issue_dir / "issue-preview.html"),
        "draft": str(issue_dir / "issue-draft.eml"),
    }
    (output / "trial-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
