from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from circus_newsletter.settings import data_directory

WEEKDAYS = {name: number for number, name in enumerate(
    ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"))}
URL_RE = re.compile(r"(?i)(?:https?://|www\.)[^\s<>\"']*")
SUPPORTED_MIME = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/heic",
    "video/mp4", "video/quicktime", "video/webm", "video/mpeg", "video/x-msvideo",
    "application/pdf", "text/plain", "text/markdown",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
BLOCKING_FLAGS = {"unknown_sender", "empty_submission", "duplicate_message_id",
                  "test_submission", "after_finalization", "unresolved_relay_identity",
                  "administrative_email"}
REPLY_ON = re.compile(
    r"(?im)^[ \t]*>?[ \t]*On[ \t]+[^\n]{0,500}(?:@|<[^>]+>)[^\n]{0,200}[ \t]+wrote:[ \t]*$"
)
ORIGINAL_MESSAGE = re.compile(r"(?im)^[ \t]*-{5,}[ \t]*Original Message[ \t]*-{5,}[ \t]*$")
CID_PLACEHOLDER = re.compile(r"(?im)^[ \t]*(?:\[cid:[^\]]+\]|<cid:[^>]+>)[ \t]*$")


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.parts.append(f" {href} ")


def load_config(root: Path) -> tuple[dict, dict]:
    roster = json.loads((root / "config/roster.json").read_text(encoding="utf-8"))
    from circus_newsletter.clock import load_schedule
    schedule = load_schedule(root)
    ZoneInfo(schedule["timezone"])
    ZoneInfo(schedule.get("cutoff_timezone", schedule["timezone"]))
    if schedule["cutoff_weekday"] not in WEEKDAYS:
        raise ValueError("cutoff_weekday must be an English weekday name")
    hour, minute = map(int, schedule["cutoff_time"].split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError("cutoff_time must be HH:MM")
    if "first_issue_week" in schedule:
        _issue_date(schedule["first_issue_week"], schedule)
    seen: set[str] = set()
    for member_id, member in roster["members"].items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", member_id):
            raise ValueError(f"unsafe member ID: {member_id}")
        pronouns = member.get("pronouns", "")
        if not isinstance(pronouns, str) or len(pronouns) > 60 or "\n" in pronouns:
            raise ValueError(f"invalid pronouns for {member_id}")
        for address in member["emails"]:
            key = address.strip().casefold()
            if key in seen:
                raise ValueError(f"sender address in roster more than once: {address}")
            seen.add(key)
    return roster, schedule


def load_relays(root: Path) -> dict:
    local = root / "config/relays.local.json"
    path = local if local.exists() else root / "config/relays.json"
    if not path.exists():
        return {}
    configured = json.loads(path.read_text(encoding="utf-8"))["relays"]
    relays = {}
    for address, relay in configured.items():
        key = address.strip().casefold()
        if not key or key in relays or not isinstance(relay.get("name"), str):
            raise ValueError(f"Invalid relay configuration for {address}")
        relays[key] = relay["name"]
    return relays


def relay_claim_from_subject(subject: str) -> str:
    """Accept the tagged form and Instinct's observed exact-name subject form."""
    match = re.match(r"^(?:\[TEST\]\s*)?\[Circus forward:\s*([^\]]+)\]", subject, re.I)
    if match:
        return match.group(1).strip()
    match = re.fullmatch(r"(?:\[TEST\]\s*)?Circus submission\s*[-:]\s*(.+?)\s*", subject, re.I)
    return match.group(1).strip() if match else ""


def issue_for(received_at: datetime, schedule: dict) -> tuple[str, datetime]:
    local = received_at.astimezone(ZoneInfo(schedule.get("cutoff_timezone", schedule["timezone"])))
    weekday = WEEKDAYS[schedule["cutoff_weekday"]]
    hour, minute = map(int, schedule["cutoff_time"].split(":"))
    close_date = local.date() + timedelta(days=(weekday - local.weekday()) % 7)
    close = datetime(close_date.year, close_date.month, close_date.day,
                     hour, minute, tzinfo=local.tzinfo)
    if local >= close:
        close_date += timedelta(days=7)
        close = datetime(close_date.year, close_date.month, close_date.day,
                         hour, minute, tzinfo=local.tzinfo)
    iso = (close.date() - timedelta(days=(weekday - 6) % 7)).isocalendar()
    return f"{iso.year}-W{iso.week:02d}", close


def _issue_date(issue: str, schedule: dict) -> date:
    match = re.fullmatch(r"(\d{4})-W(\d{2})", issue)
    if not match:
        raise ValueError("Issue must look like YYYY-Www")
    # Issue IDs name the Sunday of the submission week, even when closing is Monday.
    sunday = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 7)
    return sunday + timedelta(days=(WEEKDAYS[schedule["cutoff_weekday"]] - 6) % 7)


def _body(message: EmailMessage) -> str:
    if message.is_multipart():
        plain: list[str] = []
        html: list[str] = []
        for part in message.walk():
            if part.is_multipart() or _is_attachment(part):
                continue
            if part.get_content_type() == "text/plain":
                plain.append(_text(part))
            elif part.get_content_type() == "text/html":
                html.append(_text(part))
        return "\n\n".join(plain or html)
    return _text(message)


def strip_reply_history(body: str) -> str:
    """Remove recognised reply history while leaving forwards and ordinary prose alone."""
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    cutoffs = [match.start() for pattern in (REPLY_ON, ORIGINAL_MESSAGE)
               for match in pattern.finditer(text)]
    lines = text.splitlines(keepends=True)
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    for index, line in enumerate(lines):
        if not re.fullmatch(r"[ \t]*(?:_{10,})?[ \t]*(?:\n)?", line):
            continue
        start = index + 1 if "_" in line else index
        block = "".join(lines[start:start + 10])
        headers = {match.group(1).casefold() for match in re.finditer(
            r"(?im)^[ \t]*(From|Sent|To|Subject):", block)}
        if {"from", "sent", "to", "subject"}.issubset(headers):
            cutoffs.append(offsets[index])
    for index, line in enumerate(lines):
        if not re.match(r"(?i)^[ \t]*From:", line):
            continue
        block = "".join(lines[index:index + 10])
        headers = {match.group(1).casefold() for match in re.finditer(
            r"(?im)^[ \t]*(From|Sent|To|Subject):", block)}
        if {"from", "sent", "to", "subject"}.issubset(headers):
            cutoffs.append(offsets[index - 1] if index and re.fullmatch(
                r"[ \t]*_{10,}[ \t]*(?:\n)?", lines[index - 1]) else offsets[index])
    if cutoffs:
        text = text[:min(cutoffs)]
    text = CID_PLACEHOLDER.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _text(part: EmailMessage) -> str:
    try:
        value = part.get_content()
    except (LookupError, UnicodeError):
        value = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        parser = _HTMLText()
        parser.feed(str(value))
        return "".join(parser.parts)
    return str(value)


def _is_attachment(part: EmailMessage) -> bool:
    if part.is_multipart():
        return False
    return (part.get_content_disposition() == "attachment" or
            bool(part.get_filename()) or
            (part.get_content_disposition() == "inline" and
             part.get_content_maintype() != "text"))


def _links(body: str) -> tuple[list[str], list[str]]:
    good, bad = [], []
    for match in URL_RE.finditer(body):
        raw = match.group().rstrip(".,;:!?)]}")
        candidate = raw if raw.lower().startswith(("http://", "https://")) else "https://" + raw
        try:
            parsed = urlsplit(candidate)
            valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and "." in parsed.hostname
        except ValueError:
            valid = False
        if valid:
            if raw not in good:
                good.append(raw)
        else:
            bad.append(raw)
    return good, bad


def _existing(root: Path) -> list[dict]:
    records = []
    for path in (data_directory(root) / "issues").glob("*/submissions/*.json"):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            logging.error("Cannot read metadata %s: %s", path, exc)
            raise
    return records


def existing_source_ids(root: Path) -> set[str]:
    """IDs already archived, including records created before the IMAP migration."""
    return {record["source_id"] for record in _existing(root)}


def issues_needing_bundle(root: Path) -> set[str]:
    """Find issues with metadata newer than their editor input, including interrupted runs."""
    pending = set()
    for metadata in (data_directory(root) / "issues").glob("*/submissions/*.json"):
        issue_dir = metadata.parent.parent
        bundle = issue_dir / "issue-input.md"
        if not bundle.exists() or metadata.stat().st_mtime_ns > bundle.stat().st_mtime_ns:
            pending.add(issue_dir.name)
    return pending


def _write_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(content)
        try:
            os.rename(temporary, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise RuntimeError(f"Existing file differs; refusing to overwrite: {path}")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_replace(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ingest(root: Path, raw: bytes, source_id: str, received_at: datetime,
           *, expected_issue: str | None = None) -> dict:
    """Store a single source email. Repeated source IDs are safe to replay."""
    roster, schedule = load_config(root)
    records = _existing(root)
    for record in records:
        if record["source_id"] == source_id:
            logging.info("Already ingested source %s as %s", source_id, record["submission_id"])
            return record

    message = BytesParser(policy=policy.default).parsebytes(raw)
    sender = getaddresses([str(message.get("From", ""))])
    sender_email = (sender[0][1] if sender else "").strip().casefold()
    subject = str(message.get("Subject", ""))
    relay_name = load_relays(root).get(sender_email, "")
    claimed_name = ""
    source_kind = "relay" if relay_name else "direct"
    member_id, display_name, pronouns = "unknown", sender_email or "Unknown sender", ""
    if relay_name:
        claimed_name = relay_claim_from_subject(subject)
        if not claimed_name:
            bare_name = re.sub(r"^\[TEST\]\s*", "", subject.strip(), flags=re.I).strip()
            if any(member["name"].casefold() == bare_name.casefold()
                   for member in roster["members"].values()):
                claimed_name = bare_name
        matches = [(candidate_id, member) for candidate_id, member in roster["members"].items()
                   if member["name"].casefold() == claimed_name.casefold()]
        if len(matches) == 1:
            candidate_id, member = matches[0]
            member_id, display_name, pronouns = candidate_id, member["name"], member.get("pronouns", "")
        else:
            display_name = claimed_name or "Unknown relay contributor"
    else:
        for candidate_id, member in roster["members"].items():
            if sender_email in {value.strip().casefold() for value in member["emails"]}:
                member_id, display_name, pronouns = candidate_id, member["name"], member.get("pronouns", "")
                break

    issue, close = issue_for(received_at, schedule)
    existing_ids = [r["submission_id"] for r in records if r["issue_week"] == issue]
    sequence = 1 + max((int(match.group(1)) for value in existing_ids
                        if (match := re.fullmatch(re.escape(member_id) + r"-(\d+)", value))), default=0)
    submission_id = f"{member_id}-{sequence:03d}"
    body = strip_reply_history(_body(message))
    links, bad_links = _links(body)
    message_id = str(message.get("Message-ID", "")).strip()
    flags: list[str] = []
    if subject.lstrip().upper().startswith("[TEST]"):
        flags.append("test_submission")
    if relay_name:
        flags.append("relayed_submission")
        if member_id == "unknown":
            flags.append("unresolved_relay_identity")
    elif member_id == "unknown":
        flags.append("unknown_sender")
    if not body and not any(_is_attachment(part) for part in message.walk()):
        flags.append("empty_submission")
    if message_id and any(r.get("message_id", "").casefold() == message_id.casefold() for r in records):
        flags.append("duplicate_message_id")
    if bad_links:
        flags.append("malformed_url")
    if expected_issue and issue != expected_issue:
        flags.append("outside_current_issue_window")

    issue_dir = data_directory(root) / "issues" / issue
    if (issue_dir / "issue-input-final.md").exists():
        flags.append("after_finalization")
    attachments = []
    for index, part in enumerate(message.walk(), start=1):
        if not _is_attachment(part):
            continue
        original_name = part.get_filename() or f"attachment-{index}"
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(original_name).name).strip(".") or f"attachment-{index}"
        stored_name = f"{submission_id}-{index:02d}-{safe_name}"
        mime = part.get_content_type()
        if mime not in SUPPORTED_MIME and "unsupported_attachment_type" not in flags:
            flags.append("unsupported_attachment_type")
        attachments.append({"original_filename": original_name, "filename": stored_name,
                            "content_type": mime, "size": len(part.get_payload(decode=True) or b""),
                            "part": part})

    record = {
        "submission_id": submission_id, "member_id": member_id,
        "display_name": display_name, "sender_email": sender_email, "pronouns": pronouns,
        "source_kind": source_kind, "relayed_by": relay_name,
        "claimed_contributor": claimed_name,
        "received_at": received_at.astimezone(timezone.utc).isoformat(),
        "received_at_london": received_at.astimezone(ZoneInfo(schedule["timezone"])).isoformat(),
        "message_id": message_id, "subject": subject,
        "issue_week": issue, "issue_cutoff": close.isoformat(),
        "source_id": source_id, "source_sha256": hashlib.sha256(raw).hexdigest(),
        "original_eml": f"originals/{submission_id}.eml",
        "parsed_markdown": f"submissions/{submission_id}.md",
        "attachments": [{key: item[key] for key in ("original_filename", "filename", "content_type", "size")}
                        for item in attachments],
        "submitted_links": links, "malformed_urls": bad_links, "flags": flags,
    }
    metadata_path = issue_dir / "submissions" / f"{submission_id}.json"
    original_path = issue_dir / record["original_eml"]
    _write_new(original_path, raw)
    for item in attachments:
        _write_new(issue_dir / "attachments" / item["filename"], item["part"].get_payload(decode=True) or b"")
    front = {key: record[key] for key in ("submission_id", "member_id", "display_name", "sender_email", "pronouns",
             "source_kind", "relayed_by", "claimed_contributor",
             "received_at", "message_id", "subject", "issue_week", "attachments", "submitted_links", "flags")}
    markdown = "---\n" + "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in front.items()) + "\n---\n\n" + body + "\n"
    _write_new(issue_dir / record["parsed_markdown"], markdown.encode("utf-8"))
    _write_new(metadata_path, (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    logging.info("Ingested %s into %s; flags=%s", submission_id, issue, ",".join(flags) or "none")
    return record


def build_issue(root: Path, issue: str) -> Path:
    roster, schedule = load_config(root)
    _issue_date(issue, schedule)
    issue_dir = data_directory(root) / "issues" / issue
    records = [json.loads(path.read_text(encoding="utf-8"))
               for path in (issue_dir / "submissions").glob("*.json")]
    included = sorted((r for r in records if not BLOCKING_FLAGS.intersection(r["flags"])),
                      key=lambda r: (r["display_name"].casefold(), r["received_at"], r["submission_id"]))
    held = sorted((r for r in records if BLOCKING_FLAGS.intersection(r["flags"])),
                  key=lambda r: r["submission_id"])
    lines = [f"# Circus {issue} — Editor Input", "", "## Issue metadata", "",
             f"- Issue: {issue}", f"- Display timezone: {schedule['timezone']}",
             f"- Weekly cutoff: {schedule['cutoff_weekday']} {schedule['cutoff_time']} "
             f"{schedule.get('cutoff_timezone', schedule['timezone'])}",
             f"- Included submissions: {len(included)}", f"- Held for review: {len(held)}", "",
             "## Previous issue summaries", "", "_To be added when the editorial stage is built._", ""]
    current_member = None
    for record in included:
        if record["member_id"] != current_member:
            current_member = record["member_id"]
            member = roster["members"].get(current_member, {})
            pronouns = member.get("pronouns", record.get("pronouns", ""))
            editorial_pronouns = pronouns or "Unspecified; infer from name if needed"
            lines.extend([f"## {record['display_name']} ({current_member})", "",
                          f"- Editorial pronouns: {editorial_pronouns}", ""])
        parsed = (issue_dir / record["parsed_markdown"]).read_text(encoding="utf-8")
        body = strip_reply_history(parsed.split("\n---\n\n", 1)[1])
        links, _ = _links(body)
        lines.extend([f"### Submission {record['submission_id']}", "",
                      f"- Received: {record['received_at_london']}",
                      f"- Subject: {record['subject']}",
                      f"- Source: {record['original_eml']}",
                      f"- Submitted links: {', '.join(links) or 'None'}",
                      f"- Attachments: {', '.join(a['filename'] for a in record['attachments']) or 'None'}",
                      f"- Flags: {', '.join(record['flags']) or 'None'}", ""])
        if record.get("source_kind") == "relay":
            lines.extend([f"- Relay: {record['relayed_by']} ({record['sender_email']}); "
                          "contributor name claimed in subject", ""])
        lines.extend([body, ""])
    if held:
        lines.extend(["## Held for review", ""])
        for record in held:
            lines.append(f"- {record['submission_id']}: {', '.join(record['flags'])} ({record['sender_email']})")
        lines.append("")
    path = issue_dir / "issue-input.md"
    _atomic_replace(path, "\n".join(lines))
    logging.info("Built %s: %d included, %d held", issue, len(included), len(held))
    return path


def finalize_due_issues(root: Path, now: datetime) -> list[Path]:
    """Freeze each due issue once; later mail stays archived but cannot alter its final input."""
    if now.tzinfo is None:
        raise ValueError("Finalization time must include a timezone")
    _, schedule = load_config(root)
    if "first_issue_week" not in schedule:
        raise ValueError("first_issue_week is required for automatic finalization")
    closing_date = _issue_date(schedule["first_issue_week"], schedule)
    zone = ZoneInfo(schedule.get("cutoff_timezone", schedule["timezone"]))
    hour, minute = map(int, schedule["cutoff_time"].split(":"))
    finalized = []
    while True:
        close = datetime(closing_date.year, closing_date.month, closing_date.day,
                         hour, minute, tzinfo=zone)
        if now < close:
            break
        iso = (closing_date - timedelta(days=(WEEKDAYS[schedule["cutoff_weekday"]] - 6) % 7)).isocalendar()
        issue = f"{iso.year}-W{iso.week:02d}"
        final_path = data_directory(root) / "issues" / issue / "issue-input-final.md"
        if not final_path.exists():
            draft = build_issue(root, issue).read_text(encoding="utf-8")
            _, remainder = draft.split("\n", 1)
            final = (f"# Circus {issue} — Final Editor Input\n\n"
                     f"- Finalized at (UTC): {now.astimezone(timezone.utc).isoformat()}\n"
                     f"- Submission cutoff: {close.isoformat()}\n" + remainder)
            _write_new(final_path, final.encode("utf-8"))
            logging.info("Finalized issue %s at cutoff %s", issue, close.isoformat())
            finalized.append(final_path)
        closing_date += timedelta(days=7)
    return finalized


def received_from_eml(raw: bytes) -> datetime:
    message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    try:
        value = parsedate_to_datetime(str(message["Date"]))
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc)
    except (TypeError, ValueError, KeyError):
        pass
    raise ValueError("Local .eml must have a valid Date header with timezone; use --received-at")


def setup_logging(root: Path) -> None:
    log_dir = data_directory(root) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(log_dir / "intake.log", encoding="utf-8"),
                                  logging.StreamHandler()])
