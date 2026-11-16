"""Send one private message per campaign, with an on-disk delivery journal."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import smtplib
import ssl
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, parseaddr
from pathlib import Path

from circus_newsletter.settings import data_directory
from circus_newsletter.core import _issue_date, load_config
from circus_newsletter.clock import delivery_settings, event_at
from circus_newsletter.render import issue_label
from circus_newsletter.secret_store import read_password
from circus_newsletter.settings import mail_settings, sender_header, apply_reply_to

KINDS = {"issue", "reminder", "invitation"}
MAX_ISSUE_MESSAGE_BYTES = 20 * 1024 * 1024


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".delivery-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _lock(root: Path):
    directory = data_directory(root) / "state"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "outbound.lock").open("a+b") as handle:
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


def recipients(root: Path) -> dict[str, str]:
    roster, _ = load_config(root)
    addresses = {}
    used = set()
    for member_id, member in roster["members"].items():
        accepted = member["emails"]
        if not accepted:
            raise ValueError(f"No delivery address for {member_id}")
        address = member.get("delivery_email", accepted[0]).strip()
        if (address.casefold() not in {item.strip().casefold() for item in accepted}
                or parseaddr(address) != ("", address)
                or not re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", address)):
            raise ValueError(f"Invalid delivery address for {member_id}")
        if address.casefold() in used:
            raise ValueError(f"Duplicate delivery address for {member_id}")
        addresses[member_id] = address
        used.add(address.casefold())
    if not addresses:
        raise ValueError("No roster members to deliver to")
    return addresses


def _settings(root: Path) -> dict:
    settings = delivery_settings(root)
    if (not isinstance(settings.get("catch_up_minutes"), int)
            or not 0 < settings["catch_up_minutes"] <= 1440
            or not isinstance(settings.get("auto_send_fallback"), bool)):
        raise ValueError("Invalid delivery configuration")
    for name in (() if settings.get("version") == 2 else ("issue_send_time_utc", "reminder_time_utc")):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", settings.get(name, "")):
            raise ValueError(f"Invalid {name}")
    holds = settings.get("issue_holds", {})
    if (not isinstance(holds, dict) or any(
            not re.fullmatch(r"\d{4}-W\d{2}", issue) or not isinstance(reason, str)
            for issue, reason in holds.items())):
        raise ValueError("Invalid issue_holds")
    if (type(settings.get("delivery_retry_limit", 3)) is not int
            or not 0 <= settings.get("delivery_retry_limit", 3) <= 10
            or type(settings.get("delivery_retry_minutes", 20)) is not int
            or not 1 <= settings.get("delivery_retry_minutes", 20) <= 1440):
        raise ValueError("Invalid delivery retry configuration")
    return settings


def due_at(issue: str, kind: str, settings: dict) -> datetime:
    if kind not in KINDS:
        raise ValueError("Unknown delivery kind")
    if settings.get("version") == 2:
        return event_at(issue, "issue_send" if kind == "issue" else kind, settings)
    closing = date.fromisocalendar(int(issue[:4]), int(issue[-2:]), 7)
    day = closing + {"issue": timedelta(days=1),
                     "reminder": -timedelta(days=1),
                     "invitation": -timedelta(days=6)}[kind]
    clock = settings["reminder_time_utc" if kind == "reminder" else "issue_send_time_utc"]
    hour, minute = map(int, clock.split(":"))
    return datetime.combine(day, time(hour, minute), timezone.utc)


def _message(root: Path, issue: str, kind: str, member_id: str, address: str,
             message_date: datetime, draft: bytes | None, roster: dict) -> bytes:
    settings = mail_settings(root)
    identity = settings["newsletter"]
    account = identity["sender_address"]
    from_header = sender_header(settings)
    if kind == "issue":
        mail = BytesParser(policy=policy.SMTP).parsebytes(draft)
        if parseaddr(str(mail["From"])) != (identity["sender_name"], account) or mail["To"] or mail["Cc"] or mail["Bcc"]:
            raise ValueError("Issue draft has unexpected sender or recipients")
    else:
        _, schedule = load_config(root)
        closing = _issue_date(issue, schedule)
        cutoff_zone = schedule.get("cutoff_timezone", schedule["timezone"])
        cutoff_clock = schedule["cutoff_time"]
        if cutoff_zone == "UTC":
            reminder_cutoff = f"{closing:%A %d %B %Y} at {cutoff_clock} GMT (UTC)"
            invitation_cutoff = f"{closing:%A, %d %B %Y} at {cutoff_clock} UTC"
        else:
            from zoneinfo import ZoneInfo
            hour, minute = map(int, cutoff_clock.split(":"))
            cutoff = datetime(closing.year, closing.month, closing.day, hour, minute,
                              tzinfo=ZoneInfo(cutoff_zone))
            reminder_cutoff = cutoff.strftime("%A %d %B %Y at %H:%M %Z")
            invitation_cutoff = cutoff.strftime("%A, %d %B %Y at %H:%M %Z")
        label = issue_label(issue, schedule)
        mail = EmailMessage(policy=policy.SMTP)
        mail["From"] = from_header
        apply_reply_to(mail, settings)
        if kind == "reminder":
            mail["Subject"] = f"{identity['name']} {label}: submission reminder"
            mail.set_content(
                f"Hello {identity['short_name']},\n\n"
                f"Submissions for {identity['name']} {label} close on "
                f"{reminder_cutoff}.\n\n"
                f"Send news, personal updates, links, or photos to {identity['reply_to_address'] or account}. "
                "If you have already submitted, you can still send more before the deadline.\n\n"
                f"{identity['signature']}\n")
        else:
            settings = _settings(root)
            send_time = due_at(issue, "issue", settings)
            mail["Subject"] = f"{identity['name']} {label}: submissions are open"
            mail.set_content(
                f"Hello {identity['short_name']},\n\n"
                f"Submissions are open for {identity['name']} {label}.\n\n"
                "Reply with whatever you've been noticing in your field or corner of the world. "
                "You can also send personal updates and any photos or files you'd like included. "
                "It can be as long or short as you like.\n\n"
                "Send as many replies as you like this week; they all count toward your one submission\n\n"
                f"The submission window closes {invitation_cutoff}. Issue {label} will follow "
                f"{send_time:%A at %H:%M} UTC.\n\n"
                f"{identity['signature']}\n")
    mail["To"] = account
    mail["Date"] = format_datetime(message_date)
    mail["Message-ID"] = f"<circus-{kind}-{issue}@{identity['message_id_domain']}>"
    return mail.as_bytes(policy=policy.SMTP)


def _alert(root: Path, key: str, message: str) -> None:
    try:
        from circus_newsletter.discord import queue_delivery_alert, dispatch_pending
        queue_delivery_alert(root, key, message)
        dispatch_pending(root)
    except Exception:
        logging.exception("Could not dispatch delivery alert; inspect the outbound journal")


def send_campaign(root: Path, issue: str, kind: str, now: datetime, *,
                  approve_fallback: bool = False, allow_late: bool = False,
                  approve_safe_retry: bool = False,
                  smtp_factory=smtplib.SMTP_SSL) -> str:
    """Send inside the UTC window; an uncertain SMTP result requires manual review."""
    if now.tzinfo is None:
        raise ValueError("Delivery time must include a timezone")
    settings = _settings(root)
    installation = mail_settings(root)
    email = installation["email"]
    account = installation["newsletter"]["sender_address"]
    brand = installation["newsletter"]["short_name"]
    due = due_at(issue, kind, settings)
    now = now.astimezone(timezone.utc)
    if now < due:
        return "not_due"
    if kind == "issue" and issue in settings.get("issue_holds", {}):
        logging.info("Issue %s held from delivery: %s", issue, settings["issue_holds"][issue])
        return "held_for_review"
    if kind == "invitation":
        closing = date.fromisocalendar(int(issue[:4]), int(issue[-2:]), 7)
        previous = (closing - timedelta(days=7)).isocalendar()
        previous_issue = f"{previous.year}-W{previous.week:02d}"
        previous_journal = data_directory(root) / "issues" / previous_issue / "outbound/issue/delivery.json"
        if not previous_journal.exists():
            return "not_ready"
        previous_status = json.loads(previous_journal.read_text(encoding="utf-8")).get("status", {})
        if not previous_status or any(value != "sent" for value in previous_status.values()):
            return "not_ready"
    if not allow_late and now >= due + timedelta(minutes=settings["catch_up_minutes"]):
        journal_path = data_directory(root) / "issues" / issue / "outbound" / kind / "delivery.json"
        if journal_path.exists():
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            if journal.get("status") and all(value == "sent" for value in journal["status"].values()):
                return "already_sent"
        _alert(root, f"{kind}-late:{issue}",
               f"{brand} {issue} {kind} missed its automatic sending window; review before manual sending.")
        return "missed_window"

    issue_dir = data_directory(root) / "issues" / issue
    draft = None
    if kind == "issue":
        result_path = issue_dir / "editor-result.json"
        if not result_path.exists():
            return "not_ready"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result["issue_week"] != issue or not result.get("source_ids"):
            _alert(root, f"issue-empty:{issue}", f"{brand} {issue} has no eligible submissions; issue held.")
            return "held_empty"
        if result["mode"] == "fallback" and not (approve_fallback or settings["auto_send_fallback"]):
            return "held_fallback"
        if result["mode"] not in {"ai", "fallback"}:
            raise ValueError("Unknown editor result mode")
        draft = (issue_dir / "issue-draft.eml").read_bytes()
        if hashlib.sha256(draft).hexdigest() != result["draft_sha256"]:
            raise ValueError("Issue draft changed after editorial completion")
        if len(draft) > MAX_ISSUE_MESSAGE_BYTES:
            try:
                from circus_newsletter.media_delivery import optimized_draft, prepare_issue_media
                if prepare_issue_media(root, issue) == "ready":
                    draft = optimized_draft(root, issue, draft) or draft
            except Exception:
                logging.exception("Image optimization failed for %s", issue)
        if len(draft) > MAX_ISSUE_MESSAGE_BYTES:
            _alert(root, f"issue-size:{issue}",
                   f"{brand} {issue} issue email is too large for automatic delivery "
                   f"({len(draft):,} bytes). Review the preserved attachments before sending.")
            return "held_message_too_large"
        frozen = (issue_dir / "issue-input-final.md").read_bytes()
        if hashlib.sha256(frozen).hexdigest() != result["final_input_sha256"]:
            raise ValueError("Final editor input changed after drafting")

    with _lock(root) as acquired:
        if not acquired:
            return "busy"
        roster, _ = load_config(root)
        targets = recipients(root)
        directory = issue_dir / "outbound" / kind
        journal_path = directory / "delivery.json"
        draft_hash = hashlib.sha256(draft).hexdigest() if draft is not None else None
        if journal_path.exists():
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            if (journal.get("draft_sha256") != draft_hash or journal.get("recipients") != targets
                    or journal.get("due_at") != due.isoformat()):
                raise ValueError("Draft, schedule, or recipient list changed during delivery")
        else:
            journal = {"issue_week": issue, "kind": kind, "due_at": due.isoformat(),
                       "message_date": now.isoformat(), "draft_sha256": draft_hash,
                       "recipients": targets,
                       "status": {member_id: "pending" for member_id in targets},
                       "accepted_at": {}, "retry_count": 0}
            _save(journal_path, journal)
        message_date = datetime.fromisoformat(journal["message_date"])
        if all(value == "sent" for value in journal["status"].values()):
            return "already_sent"
        if any(value not in {"pending", "sent"} for value in journal["status"].values()):
            _alert(root, f"{kind}-uncertain:{issue}",
                   f"{brand} {issue} {kind} delivery may be partial. Inspect outbound/delivery.json before retrying.")
            return "needs_review"

        retry_count = journal.get("retry_count", 0)
        if retry_count > settings.get("delivery_retry_limit", 3) and not approve_safe_retry:
            _alert(root, f"{kind}-retry-exhausted:{issue}",
                   f"{brand} {issue} {kind} exhausted safe delivery retries; review the journal.")
            return "retry_exhausted"
        last_failure = journal.get("last_connection_failure_at")
        if last_failure and not allow_late:
            next_retry = datetime.fromisoformat(last_failure) + timedelta(
                minutes=settings.get("delivery_retry_minutes", 20))
            if now < next_retry:
                return "retry_wait"
        if (not (directory / "message.eml").exists()
                and any(value == "sent" for value in journal["status"].values())):
            return "needs_review"

        try:
            password = read_password(root)
            smtp = smtp_factory(email["smtp"]["host"], email["smtp"]["port"], context=ssl.create_default_context(), timeout=30)
            smtp.login(email["login_address"], password)
        except Exception as exc:
            if "smtp" in locals():
                try:
                    smtp.quit()
                except Exception:
                    pass
            logging.error("SMTP connection or login failed for %s %s: %s", issue, kind, type(exc).__name__)
            journal["retry_count"] = retry_count + 1
            journal["last_connection_failure_at"] = now.isoformat()
            _save(journal_path, journal)
            _alert(root, f"{kind}-connection:{issue}",
                   f"{brand} {issue} {kind} could not connect or authenticate for sending. No new recipient was attempted.")
            return "connection_failed"
        try:
            pending = {member_id: address for member_id, address in targets.items()
                       if journal["status"][member_id] == "pending"}
            raw = _message(root, issue, kind, "", "", message_date, draft, roster)
            message_path = directory / "message.eml"
            if message_path.exists():
                if message_path.read_bytes() != raw:
                    raise ValueError("Archived outgoing message changed")
            else:
                with message_path.open("xb") as output:
                    output.write(raw)
            for member_id in pending:
                journal["status"][member_id] = "sending"
            _save(journal_path, journal)
            try:
                refused = smtp.sendmail(account, list(pending.values()), raw)
            except smtplib.SMTPRecipientsRefused as exc:
                refused = exc.recipients
            except Exception as exc:
                for member_id in pending:
                    journal["status"][member_id] = "uncertain"
                journal["uncertain_at"] = datetime.now(timezone.utc).isoformat()
                _save(journal_path, journal)
                logging.error("SMTP delivery uncertain for %s %s: %s",
                              issue, kind, type(exc).__name__)
                _alert(root, f"{kind}-uncertain:{issue}",
                       f"{brand} {issue} {kind} delivery may be partial. Inspect outbound/delivery.json before retrying.")
                return "needs_review"
            refused_addresses = {address.casefold() for address in refused}
            if refused_addresses - {address.casefold() for address in pending.values()}:
                raise ValueError("SMTP reported an unknown refused recipient")
            for member_id, address in pending.items():
                if address.casefold() in refused_addresses:
                    journal["status"][member_id] = "pending"
                else:
                    journal["status"][member_id] = "sent"
                    journal["accepted_at"][member_id] = datetime.now(timezone.utc).isoformat()
            if refused_addresses:
                journal["retry_count"] = retry_count + 1
                journal["last_connection_failure_at"] = now.isoformat()
            _save(journal_path, journal)
            if refused_addresses:
                _alert(root, f"{kind}-recipient-refusal:{issue}",
                       f"{brand} {issue} {kind} was refused for some recipients; safe retry is scheduled.")
                return "partial_refusal"
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
        from circus_newsletter.discord import dispatch_pending
        try:
            dispatch_pending(root)
        except Exception:
            logging.exception("Delivery succeeded but Discord confirmation is pending")
        logging.info("Sent Circus %s %s to %d roster members", issue, kind, len(targets))
        return "sent"


def send_due(root: Path, now: datetime, *,
             smtp_factory=smtplib.SMTP_SSL) -> list[tuple[str, str, str]]:
    """Called by the minute clock and by intake as a catch-up safety net."""
    _, schedule = load_config(root)
    if now.tzinfo is None:
        raise ValueError("Delivery time must include a timezone")
    settings = _settings(root)
    outcomes = []
    utc = now.astimezone(timezone.utc)
    # Every event has its own date/window, including invitations on a later day.
    # Nearby issue IDs cover shifted weekdays and ISO year boundaries.
    candidates = set()
    for shift in (-14, -7, 0, 7):
        iso = (utc.date() + timedelta(days=shift)).isocalendar()
        candidates.add(f"{iso.year}-W{iso.week:02d}")
    for kind in ("reminder", "issue", "invitation"):
        for issue in sorted(candidates):
            due = due_at(issue, kind, settings)
            # Keep same-day missed-window reporting, plus windows across midnight.
            if utc < due:
                continue
            if not (due.date() == utc.date() or
                    due <= utc < due + timedelta(minutes=settings["catch_up_minutes"])):
                continue
            # Do not deliver a campaign from before the configured first issue.
            if issue < schedule.get("first_issue_week", "0000-W00"):
                continue
            status = send_campaign(root, issue, kind, now, smtp_factory=smtp_factory)
            if kind != "invitation" or status != "not_ready":
                outcomes.append((issue, kind, status))
    return outcomes
