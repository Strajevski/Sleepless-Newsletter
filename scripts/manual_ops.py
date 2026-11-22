"""Review-safe, on-demand Circus operations. No command in this file sends email."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.settings import data_directory

from circus_newsletter.core import (build_issue, existing_source_ids, ingest,
                                    issue_for, issues_needing_bundle, load_config,
                                    setup_logging)
from circus_newsletter.delivery import MAX_ISSUE_MESSAGE_BYTES, _settings, due_at
from circus_newsletter.editor import (_json, codex_draft, issue_from_draft,
                                      publish_draft, submissions, validate_draft)
from circus_newsletter.editor_gate import read_codex_budget
from circus_newsletter.weekly import latest_closed_issue


ROOT = Path(__file__).resolve().parents[1]
TASKS = ("Circus Newsletter Intake", "Circus Newsletter Final Build",
         "Circus Newsletter Delivery")
ARTIFACTS = ("editor-result.json", "editor-status.json", "editor-raw.json",
             "issue-data.json", "issue-email.html", "issue-preview.html",
             "issue.txt", "issue-draft.eml")
ISSUE_RE = re.compile(r"\d{4}-W\d{2}\Z")


def _issue(value: str | None) -> str:
    default = latest_closed_issue(ROOT, datetime.now(timezone.utc))
    issue = value or input(f"Issue week [{default or 'YYYY-Www'}]: ").strip() or default
    if not issue or not ISSUE_RE.fullmatch(issue):
        raise ValueError("Issue must look like 2026-W42")
    _, schedule = load_config(ROOT)
    from circus_newsletter.core import _issue_date
    _issue_date(issue, schedule)
    return issue


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".manual-", dir=path.parent)
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
def _manual_lock():
    """Serialize manual invocations; scheduled tasks are paused separately for mutations."""
    path = data_directory(ROOT) / "state" / "manual-ops.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
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
        except OSError as exc:
            raise RuntimeError("Another manual Circus operation is running") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _powershell(script: str) -> str:
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                             "-Command", script], capture_output=True, text=True,
                            encoding="utf-8", timeout=30, check=False)
    if result.returncode:
        raise RuntimeError("Task Scheduler access failed: " + (result.stderr.strip() or result.stdout.strip()))
    return result.stdout.strip()


def _task_states() -> list[dict]:
    names = ",".join("'" + name.replace("'", "''") + "'" for name in TASKS)
    script = ("$ErrorActionPreference='Stop'; $names=@(" + names + "); "
              "$rows=@(foreach($name in $names){$t=Get-ScheduledTask -TaskName $name; "
              "[pscustomobject]@{name=$name;enabled=[bool]$t.Settings.Enabled;state=[string]$t.State}}); "
              "ConvertTo-Json -InputObject $rows -Compress")
    rows = json.loads(_powershell(script))
    if not isinstance(rows, list) or len(rows) != len(TASKS):
        raise RuntimeError("Could not establish the state of every Circus task")
    return rows


def _change_task(name: str, enabled: bool) -> None:
    if name not in TASKS:
        raise ValueError("Unknown scheduled task")
    verb = "Enable" if enabled else "Disable"
    _powershell(f"$ErrorActionPreference='Stop'; {verb}-ScheduledTask -TaskName '{name}' | Out-Null")


@contextmanager
def _pause_tasks():
    """Disable future triggers, wait for active runs, restore original enabled states."""
    before = _task_states()
    changed: list[str] = []
    try:
        for task in before:
            if task["enabled"]:
                _change_task(task["name"], False)
                changed.append(task["name"])
        deadline = time.monotonic() + 330
        while any(task["state"].casefold() == "running" for task in _task_states()):
            if time.monotonic() >= deadline:
                raise RuntimeError("A Circus task remained active; no manual change was made")
            time.sleep(5)
        yield
    finally:
        failures = []
        for name in reversed(changed):
            try:
                _change_task(name, True)
            except Exception as exc:
                failures.append(f"{name}: {exc}")
        if failures:
            raise RuntimeError("Could not restore scheduled tasks: " + "; ".join(failures))


def _journal(issue: str) -> Path:
    return data_directory(ROOT) / "issues" / issue / "outbound" / "issue" / "delivery.json"


def _require_undelivered(issue: str) -> None:
    if _journal(issue).exists():
        raise RuntimeError("Issue delivery journal exists; inspect it before any editorial change")


def _hold(issue: str, reason: str) -> bool:
    if not reason.strip():
        raise ValueError("A delivery hold needs a reason")
    path = ROOT / "config" / "delivery.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    holds = settings.setdefault("issue_holds", {})
    if issue in holds:
        print(f"Delivery hold already present: {holds[issue]}")
        return False
    backup = data_directory(ROOT) / "revisions" / f"delivery-before-hold-{issue}-{_stamp()}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    holds[issue] = reason.strip()
    _atomic_json(path, settings)
    print(f"Delivery hold set for {issue}; previous configuration: {backup}")
    return True


def _status(issue: str) -> None:
    directory = data_directory(ROOT) / "issues" / issue
    settings = _settings(ROOT)
    records = sorted((directory / "submissions").glob("*.json"))
    print(f"\nCircus {issue}")
    print(f"Submissions: {len(records)}")
    print("Frozen input:", "yes" if (directory / "issue-input-final.md").exists() else "no")
    manifest = directory / "editor-result.json"
    if manifest.exists():
        result = json.loads(manifest.read_text(encoding="utf-8"))
        print("Editor:", result.get("mode"), "-", result.get("reason") or "complete")
        draft = directory / "issue-draft.eml"
        if draft.exists():
            print(f"MIME draft: {draft.stat().st_size:,} / {MAX_ISSUE_MESSAGE_BYTES:,} bytes")
            print("Draft hash valid:", _sha(draft) == result.get("draft_sha256"))
    else:
        print("Editor: not complete")
    print("Delivery hold:", settings.get("issue_holds", {}).get(issue, "none"))
    journal = _journal(issue)
    if journal.exists():
        data = json.loads(journal.read_text(encoding="utf-8"))
        statuses = data.get("status", {})
        counts = {value: list(statuses.values()).count(value) for value in sorted(set(statuses.values()))}
        print("Issue delivery journal:", counts)
        print("SMTP retries:", data.get("retry_count", 0))
        if data.get("last_connection_failure_at"):
            print("Last safe failure (UTC):", data["last_connection_failure_at"])
        if data.get("uncertain_at"):
            print("Uncertain result (UTC):", data["uncertain_at"])
        print("Archived message:", "yes" if (journal.parent / "message.eml").exists() else "legacy or absent")
    else:
        print("Issue delivery journal: absent")
    print("Issue due (UTC):", due_at(issue, "issue", settings).isoformat())
    print("Preview:", directory / "issue-preview.html")
    candidates = sorted((data_directory(ROOT) / "revisions").glob(f"{issue}-ai-retry-*.json"),
                        key=lambda path: path.stat().st_mtime, reverse=True)
    if candidates:
        print("Latest candidate:", candidates[0])
    try:
        for task in _task_states():
            print(f"Task {task['name']}: {task['state']}, enabled={task['enabled']}")
    except Exception as exc:
        print("Task state unavailable:", exc)


def _retry(issue: str) -> None:
    _require_undelivered(issue)
    if not (data_directory(ROOT) / "issues" / issue / "issue-input-final.md").exists():
        raise RuntimeError("Issue input is not frozen")
    with _pause_tasks():
        _require_undelivered(issue)
        _hold(issue, "Manual AI retry pending review")
    budget = read_codex_budget(root=ROOT)
    if not budget.allowed:
        raise RuntimeError(f"Codex usage gate blocked retry: weekly {budget.weekly_used:g}%, "
                           f"five-hour {budget.five_hour_used:g}% used")
    records, final_hash = submissions(ROOT, issue)
    print(f"Running AI editor for {len(records)} frozen submissions. No live draft will change.")
    draft = codex_draft(ROOT, issue, records)
    output = data_directory(ROOT) / "revisions" / f"{issue}-ai-retry-{_stamp()}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(draft, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    covered = {source_id for section in ("feed", "personal_updates", "worth_your_time")
               for item in draft.get(section, []) for source_id in item.get("source_ids", [])}
    positions = draft.get("long_and_short", {})
    if isinstance(positions, dict):
        covered.update(source_id for side in ("long", "short")
                       for item in positions.get(side, []) if isinstance(item, dict)
                       for source_id in item.get("source_ids", []))
    missing = sorted(record["submission_id"] for record in records
                     if record["body"].strip() and record["submission_id"] not in covered)
    print("AI candidate saved:", output)
    print("Frozen input SHA-256:", final_hash)
    print("Missing text source IDs:", ", ".join(missing) or "none")
    try:
        validate_draft(draft, issue, records)
    except Exception as exc:
        print("Candidate needs review:", exc)
        return
    print("Candidate passed project validation. Use Promote Candidate after review.")


def _choose_candidate(issue: str, provided: str | None) -> Path:
    revisions = (data_directory(ROOT) / "revisions").resolve()
    if provided:
        candidate = Path(provided)
        if not candidate.is_absolute():
            candidate = revisions / candidate
    else:
        matches = sorted(revisions.glob(f"{issue}-ai-retry-*.json"),
                         key=lambda path: path.stat().st_mtime, reverse=True)
        if matches:
            print("Saved candidates:")
            for number, path in enumerate(matches, 1):
                print(f"  {number}. {path.name}")
        choice = input("Candidate number or full path: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(matches):
            candidate = matches[int(choice) - 1]
        else:
            candidate = Path(choice)
            if not candidate.is_absolute():
                candidate = revisions / candidate
    candidate = candidate.resolve(strict=True)
    if not candidate.is_relative_to(revisions) or not candidate.is_file():
        raise ValueError(f"Candidate must be a JSON file under {revisions}")
    return candidate


def _promote(issue: str, candidate: Path, yes: bool) -> None:
    _require_undelivered(issue)
    records, final_hash = submissions(ROOT, issue)
    draft = json.loads(candidate.read_text(encoding="utf-8"))
    validate_draft(draft, issue, records)
    data = issue_from_draft(draft, records)
    print("Candidate:", candidate)
    print("Title:", draft["title"])
    print("Submissions covered:", len(records))
    if not yes and input(f"Type PROMOTE {issue} to replace the working preview: ").strip() != f"PROMOTE {issue}":
        raise RuntimeError("Promotion cancelled")
    directory = data_directory(ROOT) / "issues" / issue
    with _pause_tasks():
        _require_undelivered(issue)
        _hold(issue, "Manual AI draft pending review")
        backup = data_directory(ROOT) / "revisions" / f"{issue}-before-promotion-{_stamp()}"
        backup.mkdir(parents=True)
        existed = {name for name in ARTIFACTS if (directory / name).exists()}
        for name in existed:
            shutil.copy2(directory / name, backup / name)
        try:
            _json(directory / "editor-raw.json", draft)
            publish_draft(ROOT, issue, data, records, "ai", None, final_hash)
            _json(directory / "editor-status.json", {"status": "complete", "mode": "ai"})
            result = json.loads((directory / "editor-result.json").read_text(encoding="utf-8"))
            draft_path = directory / "issue-draft.eml"
            if (_sha(draft_path) != result["draft_sha256"]
                    or _sha(directory / "issue-input-final.md") != result["final_input_sha256"]
                    or draft_path.stat().st_size > MAX_ISSUE_MESSAGE_BYTES):
                raise RuntimeError("Generated draft failed hash or size verification")
            _require_undelivered(issue)
        except Exception:
            for name in ARTIFACTS:
                target = directory / name
                if name in existed:
                    shutil.copy2(backup / name, target)
                elif target.exists():
                    target.unlink()
            raise
        _atomic_json(backup / "promotion.json", {
            "issue_week": issue, "candidate": str(candidate),
            "candidate_sha256": _sha(candidate),
            "promoted_at": datetime.now(timezone.utc).isoformat(),
            "backup": str(backup), "draft_sha256": result["draft_sha256"],
        })
    print("Working preview:", directory / "issue-preview.html")
    print("MIME draft bytes:", draft_path.stat().st_size)
    print("Previous artifacts:", backup)
    print("Delivery remains on hold until you review and release it.")


def _delivery_action(issue: str, action: str | None, reason: str | None, yes: bool) -> None:
    action = action or input("Hold or release delivery? [hold/release]: ").strip().casefold()
    if action not in {"hold", "release"}:
        raise ValueError("Choose hold or release")
    if action == "hold":
        _require_undelivered(issue)
        reason = reason or input("Reason for hold: ").strip()
        with _pause_tasks():
            _require_undelivered(issue)
            _hold(issue, reason)
        return
    if not yes and input(f"Type RELEASE {issue} to allow scheduled delivery: ").strip() != f"RELEASE {issue}":
        raise RuntimeError("Release cancelled")
    with _pause_tasks():
        _require_undelivered(issue)
        path = ROOT / "config" / "delivery.json"
        _settings(ROOT)  # Validate before releasing a hold.
        settings = json.loads(path.read_text(encoding="utf-8"))
        holds = settings.setdefault("issue_holds", {})
        if issue not in holds:
            raise RuntimeError("No delivery hold exists for this issue")
        result_path = data_directory(ROOT) / "issues" / issue / "editor-result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        draft_path = data_directory(ROOT) / "issues" / issue / "issue-draft.eml"
        if (_sha(draft_path) != result["draft_sha256"]
                or _sha(data_directory(ROOT) / "issues" / issue / "issue-input-final.md") != result["final_input_sha256"]
                or draft_path.stat().st_size > MAX_ISSUE_MESSAGE_BYTES):
            raise RuntimeError("Draft integrity or size check failed; hold remains")
        backup = data_directory(ROOT) / "revisions" / f"delivery-before-release-{issue}-{_stamp()}.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        del holds[issue]
        _atomic_json(path, settings)
    print(f"Delivery hold released for {issue}. No email was sent by this command.")
    print("Previous configuration:", backup)


def _scan(yes: bool) -> None:
    if not yes and input("Scan Gmail INBOX and archive new mail now? [y/N]: ").strip().casefold() != "y":
        raise RuntimeError("Scan cancelled")
    from circus_newsletter.imap import connect, messages

    setup_logging(ROOT)
    _, schedule = load_config(ROOT)
    current_issue, _ = issue_for(datetime.now(timezone.utc), schedule)
    count, failures = 0, 0
    changed = set()
    with _pause_tasks():
        known = existing_source_ids(ROOT)
        connection = None
        fetch_error = None
        try:
            connection = connect(ROOT)
            for source_id, raw, received in messages(connection, known):
                try:
                    record = ingest(ROOT, raw, source_id, received,
                                    expected_issue=current_issue)
                    known.add(source_id)
                    changed.add(record["issue_week"])
                    count += 1
                    print(f"Archived {record['submission_id']} in {record['issue_week']}")
                except Exception as exc:
                    failures += 1
                    print(f"Could not archive {source_id}: {exc}")
        except Exception as exc:
            fetch_error = exc
        finally:
            if connection is not None:
                try:
                    connection.logout()
                except Exception:
                    pass
        for issue in sorted(changed | issues_needing_bundle(ROOT)):
            build_issue(ROOT, issue)
        if fetch_error is not None:
            raise RuntimeError(f"IMAP scan stopped after partial intake: {fetch_error}") from fetch_error
    print(f"Archived {count} new message(s); {failures} failure(s).")
    print("This command did not finalize, edit, notify Discord, contact SMTP, or send mail.")
    if failures:
        raise RuntimeError("Some messages remain unprocessed; rerun the scan after review")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "retry", "promote", "delivery"):
        command = sub.add_parser(name)
        command.add_argument("--issue")
        if name == "promote":
            command.add_argument("--candidate")
            command.add_argument("--yes", action="store_true")
        if name == "delivery":
            command.add_argument("--action", choices=("hold", "release"))
            command.add_argument("--reason")
            command.add_argument("--yes", action="store_true")
    scan = sub.add_parser("scan")
    scan.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    try:
        with _manual_lock():
            if args.command == "scan":
                _scan(args.yes)
            else:
                issue = _issue(args.issue)
                if args.command == "status":
                    _status(issue)
                elif args.command == "retry":
                    _retry(issue)
                elif args.command == "promote":
                    _promote(issue, _choose_candidate(issue, args.candidate), args.yes)
                else:
                    _delivery_action(issue, args.action, args.reason, args.yes)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
