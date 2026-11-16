"""Back up and compact completed legacy outbound campaigns; dry-run by default."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.settings import data_directory


def compact(root: Path, backup: Path, *, apply: bool = False) -> list[dict]:
    root = root.resolve(strict=True)
    archive_root = data_directory(root)
    backup = backup.resolve()
    if backup.is_relative_to(root) or backup.is_relative_to(archive_root):
        raise ValueError("Backup directory must be outside the project archive")
    report = []
    for journal_path in sorted((archive_root / "issues").glob("*/outbound/*/delivery.json")):
        directory = journal_path.parent
        files = sorted(directory.glob("*.eml"))
        if len(files) < 2:
            continue
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        status = journal.get("status", {})
        if (not status or any(value != "sent" for value in status.values())
                or set(path.stem for path in files) != set(status)):
            report.append({"campaign": str(directory.relative_to(archive_root)),
                           "action": "skipped: incomplete or unexpected archive"})
            continue
        digests = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        relative = directory.relative_to(archive_root)
        archive = backup / relative.parent / f"{relative.name}-legacy-eml.zip"
        report.append({"campaign": str(relative), "messages": len(files),
                       "backup": str(archive), "action": "compact" if apply else "would compact"})
        if not apply:
            continue
        if archive.exists():
            raise FileExistsError(archive)
        archive.parent.mkdir(parents=True, exist_ok=True)
        temporary = archive.with_suffix(".zip.partial")
        try:
            with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as output:
                output.write(journal_path, "delivery.json")
                for path in files:
                    output.write(path, path.name)
            with zipfile.ZipFile(temporary) as source:
                for name, digest in digests.items():
                    if hashlib.sha256(source.read(name)).hexdigest() != digest:
                        raise ValueError(f"Backup verification failed: {name}")
            os.replace(temporary, archive)
            representative = directory / "message.eml"
            with representative.open("xb") as target:
                target.write(files[0].read_bytes())
            for path in files:
                path.unlink()
        finally:
            if temporary.exists():
                temporary.unlink()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(compact(args.root, args.backup, apply=args.apply), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
