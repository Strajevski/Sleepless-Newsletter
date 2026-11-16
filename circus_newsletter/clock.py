"""Weekly timing from config/schedule.json; no mail or model calls."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

WEEKDAYS = dict(zip(("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                     "Saturday", "Sunday"), range(7)))
EVENTS = ("media", "issue_send", "reminder", "invitation")
TIMING_KEYS = ("catch_up_minutes", "delivery_retry_minutes", "delivery_retry_limit")


def event_at(issue: str, event: str, schedule: dict) -> datetime:
    """Event dates are offsets from this issue's cutoff calendar date."""
    sunday = date.fromisocalendar(int(issue[:4]), int(issue[-2:]), 7)
    closing = sunday + timedelta(days=(WEEKDAYS[schedule["cutoff_weekday"]] - 6) % 7)
    if event == "cutoff":
        day, clock = closing, schedule["cutoff_time"]
        zone = schedule.get("cutoff_timezone", schedule["timezone"])
    elif event == "retry" and schedule.get("version") != 2:
        day = sunday + timedelta(days=(WEEKDAYS[schedule.get("retry_weekday", "Monday")] - 6) % 7)
        clock, zone = schedule.get("retry_time", "05:00"), schedule.get("retry_timezone", "Europe/London")
    else:
        day = closing + timedelta(days=schedule[f"{event}_day_offset"])
        clock, zone = schedule[f"{event}_time"], schedule[f"{event}_timezone"]
    hour, minute = map(int, clock.split(":"))
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(zone))
    # Never silently shift a nonexistent wall-clock time through a DST gap.
    utc = local.astimezone(timezone.utc)
    if utc.astimezone(local.tzinfo).replace(tzinfo=None) != local.replace(tzinfo=None):
        raise ValueError(f"{event} falls in a daylight-saving gap; use UTC or another time")
    return utc


def validate(schedule: dict) -> None:
    if schedule.get("version", 1) not in (1, 2):
        raise ValueError("Unsupported schedule version")
    if schedule.get("version") == 2:
        required = ("cutoff_weekday", "cutoff_time", "cutoff_timezone",
                    "retry_day_offset", "retry_time", "retry_timezone",
                    "first_issue_week", "first_issue_number")
        required += tuple(f"{event}_{suffix}" for event in EVENTS
                          for suffix in ("time", "timezone", "day_offset"))
        for key in required:
            if key not in schedule:
                raise ValueError(f"Missing {key}")
    ZoneInfo(schedule["timezone"])
    for event in ("cutoff", "retry") + EVENTS:
        if event == "retry" and "retry_time" not in schedule:
            continue  # Read-only/local legacy configurations need only a cutoff.
        if event in EVENTS and f"{event}_time" not in schedule:
            continue
        clock = schedule[f"{event}_time"]
        if not isinstance(clock, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
            raise ValueError(f"{event}_time must be HH:MM")
        ZoneInfo(schedule.get(f"{event}_timezone", schedule["timezone"]))
        if event == "cutoff" or (event == "retry" and schedule.get("version") != 2):
            if schedule.get(f"{event}_weekday", "Monday") not in WEEKDAYS:
                raise ValueError(f"{event}_weekday must be an English weekday name")
        elif type(schedule.get(f"{event}_day_offset")) is not int or not -7 <= schedule[f"{event}_day_offset"] <= 6:
            raise ValueError(f"{event}_day_offset must be an integer from -7 to 6")
    if schedule.get("version") != 2:
        return
    for event in EVENTS:
        for suffix in ("time", "timezone", "day_offset"):
            if f"{event}_{suffix}" not in schedule:
                raise ValueError(f"Missing {event}_{suffix}")
    for key, low, high in (("catch_up_minutes", 1, 1440),
                           ("delivery_retry_minutes", 1, 1440),
                           ("delivery_retry_limit", 0, 10),
                           ("intake_interval_minutes", 1, 1440)):
        if type(schedule.get(key)) is not int or not low <= schedule[key] <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}")
    # Check a full year, including both DST transitions, before accepting edits.
    year = int(schedule.get("first_issue_week", "2026-W01")[:4])
    for week in range(1, date(year, 12, 28).isocalendar().week + 1):
        issue = f"{year}-W{week:02d}"
        cutoff, retry, media, send, reminder, invite = (
            event_at(issue, name, schedule)
            for name in ("cutoff", "retry", "media", "issue_send", "reminder", "invitation"))
        previous_sunday = date.fromisocalendar(year, week, 7) - timedelta(days=7)
        iso = previous_sunday.isocalendar()
        previous_issue = f"{iso.year}-W{iso.week:02d}"
        if not cutoff < retry < send:
            raise ValueError("Submission cutoff and editor retry must precede issue delivery")
        if not cutoff <= media <= send:
            raise ValueError("Media preparation must be between cutoff and issue delivery")
        if not event_at(previous_issue, "cutoff", schedule) < reminder < cutoff:
            raise ValueError("Reminder must be inside the submission window")
        if not (event_at(previous_issue, "cutoff", schedule) < invite < reminder
                and event_at(previous_issue, "issue_send", schedule)
                < invite + timedelta(minutes=schedule["catch_up_minutes"])):
            raise ValueError("Invitation window must allow the previous issue to be sent and precede the reminder")


@lru_cache(maxsize=8)
def _validate_text(text: str) -> None:
    validate(json.loads(text))


def load_schedule(root: Path) -> dict:
    text = (root / "config/schedule.json").read_text(encoding="utf-8")
    _validate_text(text)
    return json.loads(text)  # Callers cannot mutate the validation cache.


def delivery_settings(root: Path) -> dict:
    """Keep old private installs readable while schedule.json owns new timings."""
    settings = json.loads((root / "config/delivery.json").read_text(encoding="utf-8"))
    schedule = load_schedule(root)
    if schedule.get("version") == 2:
        for key in ("issue_send_time_utc", "reminder_time_utc") + TIMING_KEYS:
            settings.pop(key, None)
        settings.update(schedule)
    return settings


def intake_due(now: datetime, schedule: dict) -> bool:
    """Cheap minute tick; IMAP runs periodically and at configured stage times."""
    if now.tzinfo is None:
        raise ValueError("Clock time must include a timezone")
    minute = int(now.timestamp()) // 60
    if minute % schedule.get("intake_interval_minutes", 30) == 0:
        return True
    # Include either side of the week/year boundary without assuming weekdays.
    for shift in (-7, 0, 7):
        iso = (now.astimezone(timezone.utc).date() + timedelta(days=shift)).isocalendar()
        issue = f"{iso.year}-W{iso.week:02d}"
        for event in ("cutoff", "retry", "media"):
            if minute == int(event_at(issue, event, schedule).timestamp()) // 60:
                return True
    return False
