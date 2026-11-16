from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.core import (build_issue, existing_source_ids, finalize_due_issues,
                                    ingest, issue_for, issues_needing_bundle, load_config,
                                    received_from_eml, setup_logging)

parser = argparse.ArgumentParser(description="Archive and parse local or Gmail IMAP submissions")
parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
source = parser.add_mutually_exclusive_group(required=True)
source.add_argument("--eml", type=Path, nargs="+", help="Local test email files")
source.add_argument("--imap", action="store_true", help="Read Gmail through IMAP")
parser.add_argument("--received-at", help="Override local email Date in ISO 8601 format")
args = parser.parse_args()
root = args.root.resolve()
setup_logging(root)
_, schedule = load_config(root)
current_issue, _ = issue_for(datetime.now(timezone.utc), schedule)

if args.eml:
    for path in args.eml:
        raw = path.read_bytes()
        received = datetime.fromisoformat(args.received_at) if args.received_at else received_from_eml(raw)
        if received.tzinfo is None:
            parser.error("--received-at must include a timezone offset")
        source_id = "local:" + hashlib.sha256(raw).hexdigest()
        record = ingest(root, raw, source_id, received, expected_issue=current_issue)
        print(f"{path}: {record['submission_id']} -> {record['issue_week']} ({', '.join(record['flags']) or 'ok'})")
elif args.imap:
    from circus_newsletter.imap import connect, messages

    count = 0
    failures = 0
    changed_issues = set()
    connection = None
    fetch_error = None
    known = existing_source_ids(root)
    try:
        connection = connect(root)
        for source_id, raw, received in messages(connection, known):
            count += 1
            try:
                record = ingest(root, raw, source_id, received, expected_issue=current_issue)
                known.add(source_id)
                changed_issues.add(record["issue_week"])
                print(f"{source_id}: {record['submission_id']} -> {record['issue_week']} ({', '.join(record['flags']) or 'ok'})")
            except Exception:
                failures += 1
                logging.exception("Failed to ingest %s; email remains in Gmail", source_id)
    except Exception as exc:
        logging.exception("IMAP intake failed; unprocessed email remains in Gmail")
        fetch_error = exc
    finally:
        if connection is not None:
            try:
                connection.logout()
            except Exception:
                logging.warning("IMAP logout failed", exc_info=True)
    print(f"Fetched {count} new IMAP messages; {failures} failed")
    for issue in sorted(changed_issues | issues_needing_bundle(root)):
        try:
            print(f"Built {build_issue(root, issue)}")
        except Exception:
            failures += 1
            logging.exception("Failed to build issue %s", issue)
    logging.info("IMAP intake complete: %d new messages, %d failures", count, failures)
    if fetch_error:
        raise fetch_error
    if failures:
        sys.exit(1)
    for path in finalize_due_issues(root, datetime.now(timezone.utc)):
        print(f"Finalized {path}")
    try:
        from circus_newsletter.discord import dispatch_pending
        sent = dispatch_pending(root)
        if sent:
            print(f"Discord confirmed {sent} notification(s)")
    except Exception:
        logging.exception("Discord notification dispatch failed; archived email and bundles are intact")
    closed_issue = None
    try:
        from circus_newsletter.weekly import latest_closed_issue, process_issue

        now = datetime.now(timezone.utc)
        closed_issue = latest_closed_issue(root, now)
        if closed_issue:
            print(f"Weekly draft {closed_issue}: {process_issue(root, closed_issue, now)}")
            from circus_newsletter.clock import event_at
            media_at = (event_at(closed_issue, "media", schedule)
                        if schedule.get("version") == 2 else now.replace(hour=4, minute=0, second=0, microsecond=0))
            if now >= media_at:
                from circus_newsletter.media_delivery import prepare_issue_media
                try:
                    print(f"Issue media {closed_issue}: {prepare_issue_media(root, closed_issue)}")
                except Exception:
                    logging.exception("Issue media preparation failed for %s", closed_issue)
    except Exception:
        logging.exception("Weekly draft preparation failed; archived email and bundles are intact")
        if closed_issue:
            try:
                from circus_newsletter.discord import queue_editor_alert, dispatch_pending

                queue_editor_alert(root, closed_issue,
                                   "Weekly draft preparation failed unexpectedly. Inspect logs/intake.log; no issue was sent.")
                dispatch_pending(root)
            except Exception:
                logging.exception("Could not queue the weekly draft failure alert")
        sys.exit(1)
    try:
        from circus_newsletter.delivery import send_due

        for issue, kind, status in send_due(root, datetime.now(timezone.utc)):
            print(f"Delivery {issue} {kind}: {status}")
    except Exception:
        logging.exception("Scheduled delivery check failed; inspect outbound journals before manual retry")
        sys.exit(1)
