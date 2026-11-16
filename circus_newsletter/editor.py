"""Turn a frozen issue into a review draft, with a deterministic fallback."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote

from circus_newsletter.settings import data_directory
from circus_newsletter.captioner import caption_images, caption_index
from circus_newsletter.settings import (load_settings, mail_settings, sender_header,
                                        apply_reply_to, prompt_text, codex_executable, codex_environment)
from circus_newsletter.core import BLOCKING_FLAGS, _links, load_config, strip_reply_history
from circus_newsletter.render import issue_label, issue_number, render_html, render_text

IMAGE_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/heic"}
LIMIT_ERROR = re.compile(r"rate.?limit|usage.?limit|quota|too many requests|\b429\b", re.I)


class EditorFailure(RuntimeError):
    def __init__(self, reason: str, *, usage_limit: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.usage_limit = usage_limit


def _cli_errors(json_lines: str) -> str:
    messages = []
    for line in json_lines.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") in {"error", "turn.failed"}:
            detail = event.get("error") or event.get("message") or ""
            messages.append(detail.get("message", "") if isinstance(detail, dict) else str(detail))
    return "\n".join(messages)


def _write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".circus-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(data.encode("utf-8") if isinstance(data, str) else data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json(path: Path, value: dict) -> None:
    _write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def submissions(root: Path, issue: str) -> tuple[list[dict], str]:
    issue_dir = data_directory(root) / "issues" / issue
    final_path = issue_dir / "issue-input-final.md"
    frozen = final_path.read_text(encoding="utf-8")
    records = [json.loads(path.read_text(encoding="utf-8"))
               for path in (issue_dir / "submissions").glob("*.json")]
    included = sorted((r for r in records if not BLOCKING_FLAGS.intersection(r["flags"])),
                      key=lambda r: (r["received_at"], r["submission_id"]))
    frozen_ids = set(re.findall(r"^### Submission ([a-z][a-z0-9_-]*-\d+)$", frozen, re.M))
    actual_ids = {r["submission_id"] for r in included}
    if frozen_ids != actual_ids or len(frozen_ids) != len(included):
        raise ValueError("Frozen bundle and eligible submission records disagree")
    for record in included:
        parsed = (issue_dir / record["parsed_markdown"]).read_text(encoding="utf-8")
        record["body"] = strip_reply_history(parsed.split("\n---\n\n", 1)[1])
        record["submitted_links"], _ = _links(record["body"])
        for attachment in record["attachments"]:
            name = attachment["filename"]
            if Path(name).name != name or not (issue_dir / "attachments" / name).is_file():
                raise ValueError(f"Attachment missing or unsafe for {record['submission_id']}")
    return included, hashlib.sha256(frozen.encode("utf-8")).hexdigest()


def previous_issue_summaries(root: Path, issue: str) -> list[dict[str, str | int]]:
    """Return the four most recent earlier issues sent to every recipient."""
    schedule = json.loads((root / "config/schedule.json").read_text(encoding="utf-8"))
    history = []
    for directory in sorted((data_directory(root) / "issues").glob("????-W??"), reverse=True):
        week = directory.name
        if week >= issue:
            continue
        journal_path = directory / "outbound/issue/delivery.json"
        data_path = directory / "issue-data.json"
        if not journal_path.is_file() or not data_path.is_file():
            continue
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        status = journal.get("status")
        if (journal.get("issue_week") != week or journal.get("kind") != "issue"
                or not isinstance(status, dict) or not status
                or any(value != "sent" for value in status.values())):
            continue
        data = json.loads(data_path.read_text(encoding="utf-8"))
        if data.get("issue_week") != week:
            raise ValueError(f"Published issue data has the wrong week: {week}")
        history.append({"issue_week": week,
                        "issue_number": issue_number(week, schedule),
                        "issue_label": issue_label(week, schedule),
                        "title": data["title"],
                        "summary": data["summary"],
                        "sleepless_take": data.get("sleepless_take", "")})
        if len(history) == 4:
            break
    return list(reversed(history))


def editorial_input(root: Path, issue: str, records: list[dict]) -> str:
    config = json.loads((root / "config/editor.json").read_text(encoding="utf-8"))
    prompt = prompt_text((root / config["prompt"]).read_text(encoding="utf-8"), load_settings(root))
    source = [{"submission_id": r["submission_id"], "member_id": r["member_id"],
               "display_name": r["display_name"], "pronouns": r.get("pronouns", ""),
               "received_at": r["received_at_london"], "subject": r["subject"],
               "submitted_links": r["submitted_links"],
               "attachment_types": [a["content_type"] for a in r["attachments"]],
               "body": r["body"]} for r in records]
    full = (prompt + "\n\n## Frozen editorial material\n\n"
            + json.dumps({"issue_week": issue,
                          "previous_issue_summaries": previous_issue_summaries(root, issue),
                          "submissions": source}, ensure_ascii=False, indent=2)
            + "\n\nUse only the supplied material. Do not call tools or read files.\n")
    if len(full) > config["max_input_characters"]:
        raise EditorFailure("editor input exceeds the configured size limit")
    return full


def codex_draft_with_usage(root: Path, issue: str,
                           records: list[dict]) -> tuple[dict, dict | None]:
    """Run the editor and return its structured draft plus CLI token usage."""
    config = json.loads((root / "config/editor.json").read_text(encoding="utf-8"))
    # On Windows, prefer the executable over shell launchers whose path lookup
    # may resolve through a redirected profile in a restricted environment.
    executable = codex_executable(root, prefer_exe=True)
    if not executable:
        raise EditorFailure("Codex CLI is unavailable")
    prompt = editorial_input(root, issue, records)
    with tempfile.TemporaryDirectory(prefix="circus-editor-") as temporary:
        output = Path(temporary) / "draft.json"
        command = [executable, "exec", "--model", config["model"],
                   "-c", f"model_reasoning_effort={config['reasoning_effort']}",
                   "--sandbox", "read-only", "--ignore-user-config", "--ignore-rules",
                   "--skip-git-repo-check", "--ephemeral", "--json",
                   "--output-schema", str(root / "config/editor-output.schema.json"),
                   "--output-last-message", str(output), "-"]
        try:
            environment = codex_environment(root)
            result = subprocess.run(command, input=prompt, text=True, encoding="utf-8",
                                    capture_output=True, cwd=temporary,
                                    timeout=config["timeout_seconds"], check=False,
                                    env=environment)
        except subprocess.TimeoutExpired:
            raise EditorFailure("Codex editor timed out") from None
        if result.returncode:
            details = _cli_errors(result.stdout) or result.stderr.strip()
            reason = ("Codex usage limit reached"
                      if LIMIT_ERROR.search(result.stderr + "\n" + _cli_errors(result.stdout))
                      else "Codex CLI failed" + (f": {details}" if details else ""))
            raise EditorFailure(reason, usage_limit=reason == "Codex usage limit reached")
        if not output.exists():
            raise EditorFailure("Codex returned no draft")
        try:
            draft = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise EditorFailure("Codex returned invalid JSON") from None
        usage = None
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                usage = event["usage"]
        return draft, usage


def codex_draft(root: Path, issue: str, records: list[dict]) -> dict:
    draft, _ = codex_draft_with_usage(root, issue, records)
    return draft


def validate_draft(draft: dict, issue: str, records: list[dict]) -> None:
    if not isinstance(draft, dict) or draft.get("issue_week") != issue:
        raise EditorFailure("editor draft has the wrong issue week")
    for key in ("title", "summary", "sleepless_take"):
        if not isinstance(draft.get(key), str) or not draft[key].strip():
            raise EditorFailure(f"editor draft has no {key}")
    if draft.get("gallery") != []:
        raise EditorFailure("editor draft attempted to describe images")
    by_id = {r["submission_id"]: r for r in records}
    covered: set[str] = set()
    for section in ("feed", "personal_updates", "worth_your_time"):
        items = draft.get(section)
        if not isinstance(items, list):
            raise EditorFailure(f"editor draft has invalid {section}")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("source_ids"), list) or not item["source_ids"]:
                raise EditorFailure(f"editor draft has unattributed {section} item")
            for source_id in item["source_ids"]:
                if source_id not in by_id:
                    raise EditorFailure("editor draft cites an unknown submission")
                covered.add(source_id)
                if section != "worth_your_time" and item.get("member_id") != by_id[source_id]["member_id"]:
                    raise EditorFailure("editor draft has incorrect attribution")
            if section == "worth_your_time":
                allowed = {url if url.startswith(("http://", "https://")) else "https://" + url
                           for source_id in item["source_ids"] for url in by_id[source_id]["submitted_links"]}
                if item.get("url") not in allowed:
                    raise EditorFailure("editor draft has a link absent from its sources")
            elif not isinstance(item.get("text"), str) or not item["text"].strip():
                raise EditorFailure(f"editor draft has empty {section} text")
    missing = {r["submission_id"] for r in records if r["body"].strip()} - covered
    if missing:
        raise EditorFailure("editor draft omitted one or more text submissions")


def gallery(root: Path, issue: str, records: list[dict],
            captions: dict[tuple[str, str], dict] | None = None
            ) -> tuple[list[dict], list[tuple[Path, str, str]]]:
    items, related = [], []
    captions = captions or {}
    for record in records:
        for number, attachment in enumerate(record["attachments"], 1):
            mime = attachment["content_type"]
            if mime not in IMAGE_MIME:
                continue
            cid = f"{record['submission_id']}-{number}@circus.local"
            name = attachment["filename"]
            caption = captions.get((record["submission_id"], name), {})
            items.append({"image_src": f"cid:{cid}",
                          "alt": caption.get("alt_text", f"Image submitted by {record['display_name']}"),
                          "caption": caption.get("caption", f"Image from submission {record['submission_id']}"),
                          "credit": record["display_name"]})
            related.append((data_directory(root) / "issues" / issue / "attachments" / name, cid, mime))
    return items, related


def other_attachments(root: Path, issue: str, records: list[dict]) -> list[tuple[Path, str]]:
    files = []
    for record in records:
        for attachment in record["attachments"]:
            if attachment["content_type"] not in IMAGE_MIME:
                files.append((data_directory(root) / "issues" / issue / "attachments" / attachment["filename"],
                              attachment["content_type"]))
    return files


def submitted_text(issue: str, records: list[dict], *, settings: dict | None = None) -> str:
    """Collect the eligible submissions' unedited text in one readable file."""
    identity = (settings if settings is not None else load_settings())["newsletter"]
    lines = [f"{identity['short_name']} {issue} - submitted text", "",
             "Text from submissions included in this issue. Images and other files are attached separately.", ""]
    for record in records:
        lines.extend([f"=== {record['display_name']} - {record['submission_id']} ===",
                      f"Subject: {record['subject']}", "",
                      record["body"] or "[No text submitted]", ""])
    return "\n".join(lines).rstrip() + "\n"


def fallback(issue: str, records: list[dict], reason: str, *, settings: dict | None = None) -> dict:
    identity = (settings if settings is not None else load_settings())["newsletter"]
    feed = []
    for record in records:
        body = record["body"]
        if not body.strip():
            body = f"Attachment-only submission; see the {identity['headings']['gallery']} or attached files."
        feed.append({"headline": record["subject"] or f"Submission {record['submission_id']}",
                     "text": body, "attribution": record["display_name"],
                     "source_ids": [record["submission_id"]]})
    if not feed:
        feed.append({"headline": "No eligible submissions", "text": "No eligible submissions were received for this issue.",
                     "attribution": identity["name"], "source_ids": []})
    return {"issue_week": issue, "title": "This week's submissions",
            "summary": "The submissions for this week are presented below as received.",
            "feed": feed, "worth_your_time": [], "personal_updates": [],
            "sleepless_take": "", "fallback_reason": reason}


def issue_from_draft(draft: dict, records: list[dict]) -> dict:
    by_id = {r["submission_id"]: r for r in records}
    feed = [{"headline": item["headline"], "text": item["text"],
             "attribution": by_id[item["source_ids"][0]]["display_name"],
             "source_ids": item["source_ids"]} for item in draft["feed"]]
    updates = [{"name": by_id[item["source_ids"][0]]["display_name"],
                "text": item["text"], "source_ids": item["source_ids"]}
               for item in draft["personal_updates"]]
    return {"issue_week": draft["issue_week"], "title": draft["title"],
            "summary": draft["summary"], "feed": feed,
            "worth_your_time": draft["worth_your_time"], "personal_updates": updates,
            "sleepless_take": draft["sleepless_take"]}


def publish_draft(root: Path, issue: str, data: dict, records: list[dict],
                  mode: str, reason: str | None, final_hash: str, *,
                  caption_manifest_override: dict | None = None) -> Path:
    _, schedule = load_config(root)
    issue_dir = data_directory(root) / "issues" / issue
    if caption_manifest_override is not None:
        caption_manifest = caption_manifest_override
    else:
        try:
            caption_manifest = caption_images(root, issue, records, data)
        except Exception:
            logging.exception("Image captioning failed without affecting draft generation for %s", issue)
            caption_manifest = None
    images, related = gallery(root, issue, records, caption_index(caption_manifest))
    data["gallery"] = images
    data["ledger"] = {"contributors": list(dict.fromkeys(r["display_name"] for r in records))
                       or ["None"]}
    blank_take = mode == "fallback"
    settings = mail_settings(root)
    rendered = render_html(data, schedule, root / "templates/issue-email.html",
                           allow_empty_take=blank_take, settings=settings)
    plain = render_text(data, schedule, allow_empty_take=blank_take, settings=settings)
    preview = rendered
    for path, cid, _mime in related:
        preview = preview.replace(f'src="cid:{cid}"',
                                  f'src="attachments/{quote(path.name)}"')
    mail = EmailMessage()
    mail["Subject"] = f"{settings['newsletter']['name']} {issue_label(issue, schedule)}: {data['title']}"
    mail["From"] = sender_header(settings)
    apply_reply_to(mail, settings)
    mail.set_content(plain)
    mail.add_alternative(rendered, subtype="html")
    html_part = mail.get_payload()[-1]
    for path, cid, mime in related:
        html_part.add_related(path.read_bytes(), maintype="image", subtype=mime.split("/", 1)[1],
                              cid=f"<{cid}>", filename=path.name)
    for path, mime in other_attachments(root, issue, records):
        maintype, _, subtype = mime.partition("/")
        if not maintype or not subtype:
            maintype, subtype = "application", "octet-stream"
        mail.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype,
                            filename=path.name)
    source_text = submitted_text(issue, records, settings=settings)
    mail.add_attachment(source_text, subtype="plain", filename=f"circus-{issue}-submissions.txt")
    draft_bytes = mail.as_bytes()
    manifest = {"issue_week": issue, "mode": mode, "reason": reason,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_ids": [r["submission_id"] for r in records],
                "final_input_sha256": final_hash,
                "draft_sha256": hashlib.sha256(draft_bytes).hexdigest(),
                "caption_status": caption_manifest.get("status") if caption_manifest else "disabled",
                "caption_manifest_sha256": (
                    hashlib.sha256((issue_dir / "image-captions.json").read_bytes()).hexdigest()
                    if caption_manifest and (issue_dir / "image-captions.json").exists() else None),
                "model": json.loads((root / "config/editor.json").read_text(encoding="utf-8"))["model"] if mode == "ai" else None}
    _json(issue_dir / "issue-data.json", data)
    _write(issue_dir / "issue-email.html", rendered)
    _write(issue_dir / "issue-preview.html", preview)
    _write(issue_dir / "issue.txt", plain)
    _write(issue_dir / "submitted-text.txt", source_text)
    _write(issue_dir / "issue-draft.eml", draft_bytes)
    _json(issue_dir / "editor-result.json", manifest)  # Completion marker goes last.
    logging.info("Prepared %s issue draft for %s", mode, issue)
    return issue_dir / "issue-preview.html"
