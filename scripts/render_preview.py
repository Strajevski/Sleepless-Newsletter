"""Render a local HTML/text template preview without sending email."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.render import render_html, render_text


parser = argparse.ArgumentParser(description="Render a local Circus issue preview")
parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument("--data", type=Path, help="Reviewed issue JSON; defaults to template sample data")
args = parser.parse_args()
root = args.root.resolve()
data_path = args.data or root / "templates/preview-data.json"
issue = json.loads(data_path.read_text(encoding="utf-8"))
schedule = json.loads((root / "config/schedule.json").read_text(encoding="utf-8"))
from circus_newsletter.settings import load_settings
settings = load_settings(root)
html = render_html(issue, schedule, root / "templates/issue-email.html", preview=True, settings=settings)
plain = render_text(issue, schedule, settings=settings)
directory = root / "templates"
(directory / "sample-issue.html").write_text(html, encoding="utf-8")
(directory / "sample-issue.txt").write_text(plain, encoding="utf-8")
print(directory / "sample-issue.html")
print(directory / "sample-issue.txt")
