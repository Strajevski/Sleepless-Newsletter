import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.settings import data_directory

from circus_newsletter.captioner import caption_images
from circus_newsletter.editor import submissions


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an isolated image-caption trial without rebuilding or sending an issue")
    parser.add_argument("issue", help="Issue week, for example 2026-W41")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="Read-only project/data root containing issues")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--force", action="store_true", help="Ignore a matching cached successful trial")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    data_root = args.root.resolve()
    issue_dir = data_directory(data_root) / "issues" / args.issue
    issue_data_path = issue_dir / "issue-data.json"
    if not issue_data_path.exists():
        raise SystemExit(f"Missing {issue_data_path}; the issue must already have a rendered draft")
    records, _ = submissions(data_root, args.issue)
    issue_data = json.loads(issue_data_path.read_text(encoding="utf-8"))
    config = json.loads((project_root / "config/captioner.json").read_text(encoding="utf-8"))
    config["enabled"] = True
    output = (args.output or data_directory(project_root) / "output" / f"{args.issue}-caption-test"
              / "image-captions.json").resolve()
    manifest = caption_images(project_root, args.issue, records, issue_data, config_override=config,
                              manifest_path=output, attachments_dir=issue_dir / "attachments",
                              force=args.force)
    accepted = sum(bool(item.get("accepted")) for item in manifest.get("entries", []))
    print(f"Caption trial: {args.issue}")
    print(f"Status: {manifest['status']}")
    print(f"Accepted captions: {accepted}/{len(manifest.get('entries', []))}")
    print(f"Usage: {json.dumps(manifest.get('usage'), sort_keys=True)}")
    print(f"Manifest: {output}")


if __name__ == "__main__":
    main()
