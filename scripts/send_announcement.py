"""Send a roster announcement as one private SMTP message."""

from __future__ import annotations

import argparse
import hashlib
import json
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.settings import data_directory
from circus_newsletter.core import load_config
from circus_newsletter.delivery import _lock, _save, recipients
from circus_newsletter.settings import PROJECT, mail_settings, sender_header, apply_reply_to
from circus_newsletter.secret_store import read_password


def content(path: Path, *, root: Path = PROJECT) -> tuple[str, str, str]:
    identity = mail_settings(root)["newsletter"]
    raw = path.read_text(encoding="utf-8")
    headers, separator, body = raw.partition("\n\n")
    if not separator or not body.strip():
        raise ValueError("Announcement draft has no body")
    expected_to = {f"To: {identity['short_name']} roster (13 separate, private emails)",
                   f"To: {identity['short_name']} roster (private SMTP recipients)"}
    lines = headers.splitlines()
    if (len(lines) != 3 or lines[0] not in expected_to
            or lines[1] != f"From: {identity['sender_address']}" or not lines[2].startswith("Subject: ")):
        raise ValueError("Announcement draft headers changed")
    return lines[2][len("Subject: "):], body.rstrip() + "\n", hashlib.sha256(raw.encode()).hexdigest()


def message(subject: str, body: str, campaign: str, date: datetime, *, root: Path = PROJECT) -> bytes:
    settings = mail_settings(root)
    identity = settings["newsletter"]
    mail = EmailMessage(policy=policy.SMTP)
    mail["From"] = sender_header(settings)
    apply_reply_to(mail, settings)
    mail["To"] = identity["sender_address"]
    mail["Subject"] = subject
    mail["Date"] = format_datetime(date)
    mail["Message-ID"] = f"<circus-announcement-{campaign}@{identity['message_id_domain']}>"
    mail.set_content(body)
    return mail.as_bytes(policy=policy.SMTP)


def send(root: Path, draft: Path, campaign: str) -> str:
    settings = mail_settings(root)
    email = settings["email"]
    account = settings["newsletter"]["sender_address"]
    subject, body, draft_hash = content(draft, root=root)
    load_config(root)
    targets = recipients(root)
    directory = data_directory(root) / "announcements" / campaign
    journal_path = directory / "delivery.json"
    with _lock(root) as acquired:
        if not acquired:
            return "busy"
        if journal_path.exists():
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            if journal["draft_sha256"] != draft_hash or journal["recipients"] != targets:
                raise ValueError("Draft or roster changed after announcement started")
        else:
            journal = {"campaign": campaign, "draft_sha256": draft_hash,
                       "message_date": datetime.now(timezone.utc).isoformat(),
                       "recipients": targets,
                       "status": {member_id: "pending" for member_id in targets},
                       "accepted_at": {}}
            _save(journal_path, journal)
        if all(status == "sent" for status in journal["status"].values()):
            return "already_sent"
        if any(status not in {"pending", "sent"} for status in journal["status"].values()):
            return "needs_review"
        date = datetime.fromisoformat(journal["message_date"])
        password = read_password(root)
        with smtplib.SMTP_SSL(email["smtp"]["host"], email["smtp"]["port"], context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(email["login_address"], password)
            pending = {member_id: address for member_id, address in targets.items()
                       if journal["status"][member_id] == "pending"}
            if not pending:
                return "already_sent"
            if (not (directory / "message.eml").exists()
                    and any(value == "sent" for value in journal["status"].values())):
                return "needs_review"
            raw = message(subject, body, campaign, date, root=root)
            eml = directory / "message.eml"
            if eml.exists():
                if eml.read_bytes() != raw:
                    raise ValueError("Archived announcement changed")
            else:
                eml.write_bytes(raw)
            for member_id in pending:
                journal["status"][member_id] = "sending"
            _save(journal_path, journal)
            try:
                refused = smtp.sendmail(account, list(pending.values()), raw)
            except smtplib.SMTPRecipientsRefused as exc:
                refused = exc.recipients
            except Exception:
                for member_id in pending:
                    journal["status"][member_id] = "uncertain"
                _save(journal_path, journal)
                return "needs_review"
            for member_id, address in pending.items():
                if address in refused:
                    journal["status"][member_id] = "pending"
                else:
                    journal["status"][member_id] = "sent"
                    journal["accepted_at"][member_id] = datetime.now(timezone.utc).isoformat()
            _save(journal_path, journal)
            if refused:
                return "partial_refusal"
    return "sent"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--campaign", default="2026-10-06")
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()
    root, draft = args.root.resolve(), args.draft.resolve()
    subject, _body, digest = content(draft)
    targets = recipients(root)
    if not args.send:
        print(f"DRY RUN: {subject}; one message, {len(targets)} private envelope recipients; draft SHA-256 {digest}")
        return 0
    outcome = send(root, draft, args.campaign)
    print(f"Announcement {args.campaign}: {outcome}")
    return 0 if outcome in {"sent", "already_sent"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
