"""Gate a future editor command on the five-hour and weekly Codex usage windows.

Run once after the final issue bundle exists, then once at the configured retry time
with --retry if the first run was deferred. A future editor must exit 75 when
Codex explicitly reports a usage limit. No paid-credit operation is called.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.settings import data_directory
from circus_newsletter.core import load_config, setup_logging
from circus_newsletter.editor_gate import (FIVE_HOUR_MAX_USED, LIMIT_EXIT_CODE,
                                           WEEKLY_MAX_USED, next_monday_five,
                                           read_codex_budget, valid_issue)
from circus_newsletter.weekly import retry_time

DEFER_EXIT_CODE = 10
STOP_EXIT_CODE = 11


def _retry_at(root: Path, issue: str) -> datetime:
    if (root / "config/schedule.json").exists():
        _, schedule = load_config(root)
        return retry_time(issue, schedule)
    return next_monday_five(datetime.now(timezone.utc))


def alert(root: Path, issue: str, reason: str) -> None:
    from circus_newsletter.discord import dispatch_pending, queue_editor_alert

    queue_editor_alert(root, issue, reason)
    dispatch_pending(root)  # The 30-minute intake task retries if Discord is down.


def run(root: Path, issue: str, retry: bool, command: list[str]) -> int:
    if not valid_issue(issue):
        raise ValueError("Issue must look like YYYY-Www")
    if not (data_directory(root) / "issues" / issue / "issue-input-final.md").exists():
        raise FileNotFoundError(f"Final bundle is missing for {issue}")
    try:
        budget = read_codex_budget(root=root)
    except Exception as exc:
        logging.error("Codex usage check failed: %s", type(exc).__name__)
        budget = None  # An unavailable or malformed query never permits an editor run.
    if budget is None or not budget.allowed:
        if budget:
            reason = (f"weekly {budget.weekly_used:g}% used (must be <{WEEKLY_MAX_USED}%), "
                      f"five-hour {budget.five_hour_used:g}% used (must be <{FIVE_HOUR_MAX_USED}%)")
        else:
            reason = "Codex usage status unavailable"
        if retry:
            alert(root, issue, f"Editor unavailable after the configured retry: {reason}.")
            print(json.dumps({"issue": issue, "decision": "stop", "reason": reason}))
            return STOP_EXIT_CODE
        when = _retry_at(root, issue)
        print(json.dumps({"issue": issue, "decision": "defer", "reason": reason,
                          "retry_at": when.isoformat()}))
        return DEFER_EXIT_CODE
    print(json.dumps({"issue": issue, "decision": "allow",
                      "weekly_used_percent": budget.weekly_used,
                      "five_hour_used_percent": budget.five_hour_used}))
    if not command:
        return 0
    completed = subprocess.run(command, cwd=root, check=False)
    if completed.returncode == LIMIT_EXIT_CODE:
        alert(root, issue, "Codex reported a usage limit while editing. No paid credits were requested.")
        logging.error("Editor hit a Codex usage limit for %s", issue)
    return completed.returncode


def main() -> int:
    raw = sys.argv[1:]
    divider = raw.index("--") if "--" in raw else len(raw)
    options, command = raw[:divider], raw[divider + 1:] if divider < len(raw) else []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issue", help="Frozen issue week, such as 2026-W40")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--retry", action="store_true", help="This is the single configured retry")
    parser.add_argument("--wait", action="store_true",
                        help="On deferral, remain running until the configured retry and recheck once")
    args = parser.parse_args(options)
    root = args.root.resolve()
    setup_logging(root)
    try:
        result = run(root, args.issue, args.retry, command)
        if result == DEFER_EXIT_CODE and args.wait:
            when = _retry_at(root, args.issue)
            seconds = max(0, (when.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds())
            logging.info("Editor deferred until %s; one usage recheck remains", when.isoformat())
            time.sleep(seconds)
            return run(root, args.issue, True, command)
        return result
    except Exception:
        logging.exception("Editor preflight failed")
        return STOP_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
