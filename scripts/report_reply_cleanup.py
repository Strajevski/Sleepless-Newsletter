import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.settings import data_directory

from circus_newsletter.core import BLOCKING_FLAGS, strip_reply_history


def main() -> None:
    parser = argparse.ArgumentParser(description="Report deterministic reply-history cleanup without changing files")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--issue", action="append", help="Issue week to inspect; may be repeated")
    args = parser.parse_args()

    root = args.root.resolve()
    selected = set(args.issue or [])
    issues = []
    for issue_dir in sorted((data_directory(root) / "issues").glob("*-W*")):
        if selected and issue_dir.name not in selected:
            continue
        rows = []
        for metadata in sorted((issue_dir / "submissions").glob("*.json")):
            record = json.loads(metadata.read_text(encoding="utf-8"))
            if BLOCKING_FLAGS.intersection(record.get("flags", [])):
                continue
            parsed = (issue_dir / record["parsed_markdown"]).read_text(encoding="utf-8")
            original = parsed.split("\n---\n\n", 1)[1].rstrip("\n")
            cleaned = strip_reply_history(original)
            removed = len(original) - len(cleaned)
            rows.append({"submission_id": record["submission_id"],
                         "original_characters": len(original),
                         "cleaned_characters": len(cleaned),
                         "removed_characters": removed})
        removed = sum(row["removed_characters"] for row in rows)
        issues.append({"issue_week": issue_dir.name, "eligible_submissions": len(rows),
                       "original_characters": sum(row["original_characters"] for row in rows),
                       "cleaned_characters": sum(row["cleaned_characters"] for row in rows),
                       "removed_characters": removed,
                       "rough_tokens_removed_at_four_characters_each": math.ceil(removed / 4),
                       "submissions": rows})
    print(json.dumps({"issues": issues}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
