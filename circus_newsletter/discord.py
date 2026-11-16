"""Small, retryable Discord status notifications for archived submissions and issue inputs."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from circus_newsletter.settings import data_directory
from circus_newsletter.core import BLOCKING_FLAGS
from circus_newsletter.settings import load_settings
from circus_newsletter.secret_store import DISCORD_SECRET_NAME, read_secret

STATE_FILE = Path("state/discord-delivered.json")
INVALID_FILE = Path("state/discord-invalid.json")
EDITOR_ALERTS_FILE = Path("state/editor-alerts.json")
DELIVERY_ALERTS_FILE = Path("state/delivery-alerts.json")


class WebhookError(RuntimeError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Discord returned HTTP {status}")


class RateLimited(WebhookError):
    def __init__(self, seconds: float):
        self.seconds = seconds
        super().__init__(429)


def validate_webhook_url(url: str) -> str:
    """Reject non-Discord destinations before a secret or any message is sent."""
    url = url.strip()
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in {"discord.com", "discordapp.com"}
            or parts.port is not None or parts.username or parts.password or parts.query or parts.fragment
            or not re.fullmatch(r"/api/webhooks/[0-9]+/[A-Za-z0-9._-]+", parts.path)):
        raise ValueError("Expected a Discord webhook URL copied from Server Settings → Integrations")
    return url


def post_webhook(url: str, content: str) -> str:
    """Return Discord's saved message ID; no webhook URL enters logs or errors."""
    validate_webhook_url(url)
    payload = json.dumps({"content": content, "allowed_mentions": {"parse": []}},
                         ensure_ascii=False).encode("utf-8")
    request = Request(url + "?wait=true", data=payload,
                      headers={"Content-Type": "application/json", "User-Agent": "CircusNewsletter/1"},
                      method="POST")
    try:
        with urlopen(request, timeout=15) as response:
            message = json.load(response)
    except HTTPError as exc:
        if exc.code == 429:
            try:
                seconds = float(exc.headers.get("Retry-After", "60"))
            except ValueError:
                seconds = 60
            raise RateLimited(seconds) from None
        raise WebhookError(exc.code) from None
    except URLError:
        raise RuntimeError("Discord connection failed") from None
    message_id = str(message.get("id", ""))
    if not message_id.isdigit():
        raise RuntimeError("Discord did not confirm a saved message")
    return message_id


def _state(root: Path) -> dict:
    path = data_directory(root) / STATE_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save(root: Path, path: Path, value: dict) -> None:
    target = data_directory(root) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".discord-", dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _delivery_lock(root: Path):
    """Prevent the 30-minute and cutoff tasks posting the same event concurrently."""
    directory = data_directory(root) / "state"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "discord.lock").open("a+b") as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logging.info("Discord delivery already running; the next intake will retry")
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _events(root: Path) -> list[tuple[str, str]]:
    identity = load_settings(root)["newsletter"]
    brand = identity["short_name"]
    events = []
    for path in sorted((data_directory(root) / "issues").glob("*/submissions/*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["member_id"] == "unknown" and record.get("source_kind") != "relay":
            continue
        bundle = path.parent.parent / "issue-input.md"
        if not bundle.exists() or bundle.stat().st_mtime_ns < path.stat().st_mtime_ns:
            continue
        held = bool(BLOCKING_FLAGS.intersection(record["flags"]))
        status = "Archived and held for review" if held else "Archived and added to the editor input"
        flags = ", ".join(record["flags"]) or "none"
        display_name = record["display_name"]
        if record.get("source_kind") == "relay":
            display_name += f" (via {record['relayed_by']})"
        content = (f"{brand} submission {record['submission_id']} — {display_name}\n"
                   f"Issue: {record['issue_week']} | Status: {status}\n"
                   f"Attachments: {len(record['attachments'])} | Flags: {flags}")
        events.append((f"submission:{record['source_id']}", content))
    for path in sorted((data_directory(root) / "issues").glob("*/issue-input-final.md")):
        final = path.read_text(encoding="utf-8")
        included = re.search(r"^- Included submissions: (\d+)$", final, re.M)
        held = re.search(r"^- Held for review: (\d+)$", final, re.M)
        if not (included and held):
            logging.error("Cannot summarize final bundle %s", path)
            continue
        issue = path.parent.name
        events.append((f"final-input:{issue}",
                       f"{brand} {issue} editor input finalized. "
                       f"Included: {included.group(1)} | Held for review: {held.group(1)}. "
                       "Ready for the editor."))
    for issue, reason in sorted(_state_for(root, EDITOR_ALERTS_FILE).items()):
        events.append((f"editor-unavailable:{issue}", f"{brand} {issue}: {reason}"))
    for key, message in sorted(_state_for(root, DELIVERY_ALERTS_FILE).items()):
        events.append((f"delivery-alert:{key}", message))
    for path in sorted((data_directory(root) / "issues").glob("*/editor-result.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        if result.get("mode") == "fallback":
            settings = json.loads((root / "config/delivery.json").read_text(encoding="utf-8"))
            issue = path.parent.name
            if issue in settings.get("issue_holds", {}):
                delivery_note = "Issue is held for review."
            elif settings.get("auto_send_fallback"):
                delivery_note = "Scheduled for automatic delivery if eligible."
            else:
                delivery_note = "Review the issue draft before manual sending."
            events.append((f"editor-fallback:{path.parent.name}",
                           f"{brand} {path.parent.name}: deterministic fallback draft prepared. "
                           f"Reason: {result.get('reason') or 'unspecified editor error'}. "
                           f"All eligible submissions are in {('The Feed' if identity['headings']['feed'] == 'The Lead' else identity['headings']['feed'])}; images are in the {identity['headings']['gallery']}. "
                           f"{identity['headings']['editorial'].replace(chr(8217), chr(39))} is blank. {delivery_note}"))
    for path in sorted((data_directory(root) / "issues").glob("*/outbound/*/delivery.json")):
        journal = json.loads(path.read_text(encoding="utf-8"))
        if journal.get("status") and all(value == "sent" for value in journal["status"].values()):
            issue, kind = journal["issue_week"], journal["kind"]
            events.append((f"delivery-complete:{kind}:{issue}",
                           f"{brand} {issue} {kind} accepted by Gmail SMTP for "
                           f"{len(journal['status'])} roster members."))
    return events


def _state_for(root: Path, path: Path) -> dict:
    target = data_directory(root) / path
    return json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}


def queue_editor_alert(root: Path, issue: str, reason: str) -> None:
    """Persist one editor alert per issue so intake can retry Discord delivery."""
    if not re.fullmatch(r"\d{4}-W\d{2}", issue):
        raise ValueError("Invalid issue week")
    with _delivery_lock(root) as acquired:
        if not acquired:
            raise RuntimeError("Discord delivery lock is busy; retry editor alert")
        alerts = _state_for(root, EDITOR_ALERTS_FILE)
        if issue not in alerts:
            alerts[issue] = reason
            _save(root, EDITOR_ALERTS_FILE, alerts)


def queue_delivery_alert(root: Path, key: str, message: str) -> None:
    """Persist a short operational alert for retry on the next intake run."""
    if not re.fullmatch(r"[a-z-]+:\d{4}-W\d{2}", key):
        raise ValueError("Invalid delivery alert key")
    with _delivery_lock(root) as acquired:
        if not acquired:
            raise RuntimeError("Discord delivery lock is busy; retry delivery alert")
        alerts = _state_for(root, DELIVERY_ALERTS_FILE)
        if key not in alerts:
            alerts[key] = message
            _save(root, DELIVERY_ALERTS_FILE, alerts)


def baseline_existing(root: Path) -> int:
    """On first connection, leave old archived records out of the notification stream."""
    with _delivery_lock(root) as acquired:
        if not acquired:
            raise RuntimeError("Another Discord delivery is running")
        if (data_directory(root) / STATE_FILE).exists():
            return 0
        events = _events(root)
        _save(root, STATE_FILE, {key: {"status": "before-connection"} for key, _ in events})
        return len(events)


def dispatch_pending(root: Path) -> int:
    if not load_settings(root)["notifications"]["discord_enabled"]:
        return 0
    secret_path = root / "config/secrets" / DISCORD_SECRET_NAME
    if not secret_path.exists():
        return 0
    with _delivery_lock(root) as acquired:
        if not acquired:
            return 0
        url = read_secret(root, DISCORD_SECRET_NAME)
        invalid_path = data_directory(root) / INVALID_FILE
        if invalid_path.exists():
            logging.error("Discord webhook disabled after an authentication or missing-webhook error; rerun configure_discord.py")
            return 0
        delivered = _state(root)
        sent = 0
        for key, content in _events(root):
            if key in delivered:
                continue
            try:
                message_id = post_webhook(url, content)
            except RateLimited as exc:
                logging.warning("Discord rate limited; retry after %.1f seconds on next intake", exc.seconds)
                break
            except WebhookError as exc:
                logging.error("Discord notification failed: HTTP %s", exc.status)
                if exc.status in {401, 403, 404}:
                    _save(root, INVALID_FILE, {"status": exc.status})
                break
            except Exception as exc:
                logging.error("Discord notification failed: %s", type(exc).__name__)
                break
            delivered[key] = {"message_id": message_id,
                              "at": datetime.now(timezone.utc).isoformat()}
            _save(root, STATE_FILE, delivered)
            sent += 1
            logging.info("Discord confirmed notification %s as %s", key, message_id)
        return sent
