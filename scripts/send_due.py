"""Run the due issue or reminder delivery; SMTP sends only inside the UTC window."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.core import setup_logging
from circus_newsletter.delivery import send_campaign, send_due


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--issue", help="Explicit YYYY-Www issue for a manual send")
    parser.add_argument("--kind", choices=("issue", "reminder", "invitation"), help="Campaign for a manual send")
    parser.add_argument("--approve-fallback", action="store_true", help="Send a reviewed fallback issue")
    parser.add_argument("--send-late", action="store_true", help="Manually send after the automatic window")
    parser.add_argument("--approve-safe-retry", action="store_true",
                        help="Manually retry after the safe automatic retry limit; uncertain sends remain blocked")
    args = parser.parse_args()
    if bool(args.issue) != bool(args.kind):
        parser.error("--issue and --kind must be supplied together")
    if (args.approve_fallback or args.send_late or args.approve_safe_retry) and not args.issue:
        parser.error("manual approvals require --issue and --kind")
    if args.approve_fallback and args.kind != "issue":
        parser.error("--approve-fallback applies only to an issue")
    root = args.root.resolve()
    setup_logging(root)
    try:
        if args.issue:
            outcomes = [(args.issue, args.kind, send_campaign(
                root, args.issue, args.kind, datetime.now(timezone.utc),
                approve_fallback=args.approve_fallback, allow_late=args.send_late,
                approve_safe_retry=args.approve_safe_retry))]
        else:
            outcomes = send_due(root, datetime.now(timezone.utc))
    except Exception:
        logging.exception("Delivery check failed")
        return 1
    for issue, kind, status in outcomes:
        print(f"{issue} {kind}: {status}")
    return 1 if any(status in {"connection_failed", "needs_review", "missed_window",
                               "held_message_too_large", "retry_exhausted", "partial_refusal"}
                    for _, _, status in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
