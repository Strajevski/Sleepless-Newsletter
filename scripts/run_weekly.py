"""Prepare the latest closed Circus issue for review; never send it."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.core import setup_logging
from circus_newsletter.weekly import latest_closed_issue, process_issue


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--issue", help="Use a specific finalized YYYY-Www issue")
    parser.add_argument("--retry", action="store_true", help="Treat this as the 05:00 retry")
    args = parser.parse_args()
    root = args.root.resolve()
    setup_logging(root)
    now = datetime.now(timezone.utc)
    issue = args.issue or latest_closed_issue(root, now)
    if issue is None:
        print("No issue has reached its cutoff yet")
        return 0
    try:
        status = process_issue(root, issue, now, retry=args.retry)
    except Exception:
        logging.exception("Weekly issue preparation failed for %s", issue)
        return 1
    print(f"{issue}: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
