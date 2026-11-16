"""Inspect the schedule, or run the scheduled intake tick (explicit opt-in)."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.clock import event_at, intake_due, load_schedule


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--issue", help="Show UTC event times for YYYY-Www; never sends mail")
    parser.add_argument("--run-intake", action="store_true", help="Run intake when the configuration says it is due")
    args = parser.parse_args()
    schedule = load_schedule(args.root)
    now = datetime.now(timezone.utc)
    if args.run_intake:
        if args.issue:
            parser.error("--issue cannot be combined with --run-intake")
        if intake_due(now, schedule):
            return subprocess.run([sys.executable, str(Path(__file__).with_name("intake.py")),
                                   "--root", str(args.root), "--imap"], check=False).returncode
        return 0
    issue = args.issue or schedule["first_issue_week"]
    for name in ("invitation", "reminder", "cutoff", "media", "retry", "issue_send"):
        print(f"{name:12} {event_at(issue, name, schedule).isoformat()}")
    print(f"Intake interval: {schedule['intake_interval_minutes']} minutes")
    print(f"Automatic delivery window: {schedule['catch_up_minutes']} minutes")
    print(f"Safe delivery retries: {schedule['delivery_retry_limit']}, "
          f"{schedule['delivery_retry_minutes']} minutes apart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
