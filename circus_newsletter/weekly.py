"""One weekly editorial attempt, one configured retry, and a reviewable fallback."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from circus_newsletter.settings import data_directory
from circus_newsletter.core import WEEKDAYS, _issue_date, load_config
from circus_newsletter.clock import delivery_settings, event_at
from circus_newsletter.settings import load_settings
from circus_newsletter.discord import dispatch_pending
from circus_newsletter.editor import (EditorFailure, _json, codex_draft, fallback,
                                      issue_from_draft, publish_draft, submissions,
                                      validate_draft)
from circus_newsletter.editor_gate import (FIVE_HOUR_MAX_USED, WEEKLY_MAX_USED,
                                           read_codex_budget)


def retry_time(issue: str, schedule: dict) -> datetime:
    cutoff = event_at(issue, "cutoff", schedule)
    retry_at = event_at(issue, "retry", schedule)
    if retry_at <= cutoff:
        raise ValueError("Editor retry must be after the submission cutoff")
    return retry_at


def latest_closed_issue(root: Path, now: datetime) -> str | None:
    _, schedule = load_config(root)
    zone = ZoneInfo(schedule.get("cutoff_timezone", schedule["timezone"]))
    local = now.astimezone(zone)
    weekday = WEEKDAYS[schedule["cutoff_weekday"]]
    days_back = (local.weekday() - weekday) % 7
    closing = local.date() - timedelta(days=days_back)
    hour, minute = map(int, schedule["cutoff_time"].split(":"))
    close = datetime(closing.year, closing.month, closing.day, hour, minute, tzinfo=zone)
    if local < close:
        closing -= timedelta(days=7)
    first = schedule["first_issue_week"]
    first_date = _issue_date(first, schedule)
    if closing < first_date:
        return None
    iso = (closing - timedelta(days=(weekday - 6) % 7)).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _notify(root: Path) -> None:
    try:
        dispatch_pending(root)
    except Exception:
        logging.exception("Discord dispatch failed; the fallback alert remains in the issue manifest")


def process_issue(root: Path, issue: str, now: datetime, *, retry: bool = False) -> str:
    issue_dir = data_directory(root) / "issues" / issue
    complete_path = issue_dir / "editor-result.json"
    if complete_path.exists():
        _notify(root)
        return "already_complete"
    if not (issue_dir / "issue-input-final.md").exists():
        return "not_finalized"
    records, final_hash = submissions(root, issue)
    status_path = issue_dir / "editor-status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    _, schedule = load_config(root)
    retry_at = retry_time(issue, schedule)
    from circus_newsletter.delivery import due_at
    settings = delivery_settings(root)
    closing = _issue_date(issue, schedule)
    hour, minute = map(int, schedule["cutoff_time"].split(":"))
    cutoff = datetime(closing.year, closing.month, closing.day, hour, minute,
                      tzinfo=ZoneInfo(schedule.get("cutoff_timezone", schedule["timezone"])))
    send_at = due_at(issue, "issue", settings)
    if cutoff >= send_at or retry_at >= send_at:
        raise ValueError("Submission cutoff and editor retry must precede issue delivery")
    if status.get("status") == "deferred" and not retry and now < retry_at:
        return "deferred"
    retry = retry or status.get("status") == "deferred" or now >= retry_at

    def defer_or_fallback(reason: str) -> str:
        if not retry:
            _json(status_path, {"status": "deferred", "reason": reason,
                                "retry_at": retry_at.isoformat(),
                                "at": now.astimezone(timezone.utc).isoformat()})
            logging.warning("Editor deferred for %s until %s: %s", issue, retry_at, reason)
            return "deferred"
        data = fallback(issue, records, reason, settings=load_settings(root))
        publish_draft(root, issue, data, records, "fallback", reason, final_hash)
        _json(status_path, {"status": "complete", "mode": "fallback", "reason": reason})
        _notify(root)
        return "fallback"

    if not records:
        return _fallback_now(root, issue, records, final_hash, status_path,
                             "No eligible submissions were available")
    raw_path = issue_dir / "editor-raw.json"
    if raw_path.exists():
        draft = json.loads(raw_path.read_text(encoding="utf-8"))
    else:
        try:
            budget = read_codex_budget(root=root)
        except Exception as exc:
            logging.error("Codex usage check failed for %s: %s", issue, type(exc).__name__)
            return defer_or_fallback("Codex usage status unavailable")
        if not budget.allowed:
            return defer_or_fallback(
                f"Codex limits: weekly {budget.weekly_used:g}% used (requires <{WEEKLY_MAX_USED}%), "
                f"five-hour {budget.five_hour_used:g}% used (requires <{FIVE_HOUR_MAX_USED}%)")
        try:
            draft = codex_draft(root, issue, records)
        except EditorFailure as exc:
            return defer_or_fallback(exc.reason) if exc.usage_limit else _fallback_now(
                root, issue, records, final_hash, status_path, exc.reason)
        except Exception:
            logging.exception("Unexpected Codex editor failure for %s", issue)
            return _fallback_now(root, issue, records, final_hash, status_path,
                                 "AI editor failed unexpectedly")
    try:
        validate_draft(draft, issue, records)
        _json(raw_path, draft)
        data = issue_from_draft(draft, records)
        publish_draft(root, issue, data, records, "ai", None, final_hash)
    except (EditorFailure, KeyError, TypeError, ValueError) as exc:
        reason = exc.reason if isinstance(exc, EditorFailure) else "Editor draft was invalid"
        return _fallback_now(root, issue, records, final_hash, status_path, reason)
    _json(status_path, {"status": "complete", "mode": "ai"})
    return "ai"


def _fallback_now(root: Path, issue: str, records: list[dict], final_hash: str,
                  status_path: Path, reason: str) -> str:
    publish_draft(root, issue, fallback(issue, records, reason, settings=load_settings(root)), records,
                  "fallback", reason, final_hash)
    _json(status_path, {"status": "complete", "mode": "fallback", "reason": reason})
    _notify(root)
    return "fallback"
