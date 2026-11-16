"""Read Codex quota without starting a model turn, then gate one editor attempt."""

from __future__ import annotations

import json
import math
import queue
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from circus_newsletter.settings import PROJECT, codex_executable, codex_environment

WEEKLY_MAX_USED = 90
FIVE_HOUR_MAX_USED = 80
LIMIT_EXIT_CODE = 75  # Contract for the future editor: Codex reported usage exhausted.
UK = ZoneInfo("Europe/London")


@dataclass(frozen=True)
class Budget:
    weekly_used: float
    five_hour_used: float
    weekly_reset: datetime
    five_hour_reset: datetime

    @property
    def allowed(self) -> bool:
        return self.weekly_used < WEEKLY_MAX_USED and self.five_hour_used < FIVE_HOUR_MAX_USED


def _window(bucket: dict, minutes: int) -> tuple[float, datetime]:
    candidates = [bucket.get("primary"), bucket.get("secondary")]
    match = next((item for item in candidates
                  if isinstance(item, dict) and item.get("windowDurationMins") == minutes), None)
    if match is None:
        raise ValueError(f"Codex did not report a {minutes}-minute usage window")
    percent = match.get("usedPercent")
    reset = match.get("resetsAt")
    if (isinstance(percent, bool) or not isinstance(percent, (int, float))
            or not math.isfinite(percent) or not 0 <= percent <= 100
            or isinstance(reset, bool) or not isinstance(reset, (int, float))
            or not math.isfinite(reset)):
        raise ValueError("Codex usage window has missing or invalid values")
    return float(percent), datetime.fromtimestamp(reset, timezone.utc)


def parse_budget(result: dict) -> Budget:
    if result.get("ordinaryUsageAllowed") is False:
        raise ValueError("Codex reports ordinary usage is unavailable")
    bucket = result.get("rateLimitsByLimitId", {}).get("codex") or result.get("rateLimits")
    if not isinstance(bucket, dict) or bucket.get("rateLimitReachedType"):
        raise ValueError("Codex usage limits are unavailable or reached")
    credits = bucket.get("credits")
    if not isinstance(credits, dict) or credits.get("hasCredits") is not False:
        raise ValueError("Cannot establish that paid credits are unavailable")
    five_hour, five_hour_reset = _window(bucket, 300)
    weekly, weekly_reset = _window(bucket, 10080)
    return Budget(weekly, five_hour, weekly_reset, five_hour_reset)


def next_monday_five(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("Current time must have a timezone")
    local = now.astimezone(UK)
    days = (0 - local.weekday()) % 7
    candidate_date = local.date() + timedelta(days=days)
    candidate = datetime(candidate_date.year, candidate_date.month, candidate_date.day,
                         5, 0, tzinfo=UK)
    return candidate


def _read_reply(stream, output: queue.Queue, stop: threading.Event) -> None:
    try:
        for line in stream:
            if stop.is_set():
                return
            try:
                output.put(json.loads(line))
            except json.JSONDecodeError:
                continue
    finally:
        output.put(None)


def _rpc_reply(output: queue.Queue, request_id: int, timeout: float) -> dict:
    from time import monotonic

    deadline = monotonic() + timeout
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex usage query timed out")
        try:
            message = output.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError("Codex usage query timed out") from None
        if message is None:
            raise RuntimeError("Codex app-server exited before answering the usage query")
        if message.get("id") == request_id:
            if "error" in message:
                raise RuntimeError("Codex rejected the usage query")
            return message.get("result", {})


def read_codex_budget(timeout: float = 20, *, root: Path = PROJECT) -> Budget:
    executable = codex_executable(root)
    if not executable:
        raise RuntimeError("Codex CLI is not installed or not on PATH")
    environment = codex_environment(root, profile_defaults=False)
    process = subprocess.Popen([executable, "app-server"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding="utf-8", bufsize=1, env=environment)
    output: queue.Queue = queue.Queue()
    stop = threading.Event()
    reader = threading.Thread(target=_read_reply, args=(process.stdout, output, stop), daemon=True)
    reader.start()

    def send(message: dict) -> None:
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    try:
        send({"method": "initialize", "id": 1, "params": {
            "clientInfo": {"name": "circus_newsletter", "title": "Circus Newsletter",
                           "version": "0.2.0"}}})
        _rpc_reply(output, 1, timeout)
        send({"method": "initialized", "params": {}})
        send({"method": "account/rateLimits/read", "id": 2})
        return parse_budget(_rpc_reply(output, 2, timeout))
    finally:
        stop.set()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()


def valid_issue(issue: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-W\d{2}", issue))
