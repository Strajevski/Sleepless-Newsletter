from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.core import build_issue, setup_logging

parser = argparse.ArgumentParser(description="Build one editor-ready issue bundle")
parser.add_argument("issue", help="YYYY-Www")
parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
args = parser.parse_args()
setup_logging(args.root)
print(build_issue(args.root.resolve(), args.issue))
