# Manual Circus controls

Double-click one of the numbered `.cmd` files on the installation host. The window prompts for an issue week or candidate where needed and stays open so you can read the result. Run them as the Windows account that owns the scheduled tasks, IMAP credential, and Codex CLI login. All five actions use the installation containing these scripts and its configured data directory.

1. **Status** reads the frozen input, draft, hold, delivery journal, candidates, retry count, archived message, and scheduled-task state.
2. **Retry AI** places an issue delivery hold, then runs the configured editor against its frozen input. Every returned JSON candidate is saved under `revisions/`, including one that fails coverage validation. It never replaces the live preview.
3. **Promote candidate** validates a saved JSON, holds delivery, backs up the existing editorial artifacts under `revisions/`, and uses the normal renderer to replace the live preview and MIME draft. It refuses to run once an issue delivery journal exists. Inspect the new preview before releasing the hold.
4. **Hold or release** changes only this issue's delivery hold. Release checks the frozen-input and MIME-draft hashes and the 20 MiB size policy. It does not send an email; it allows the existing schedule to do so.
5. **Scan inbox** reads Gmail INBOX without changing messages, archives unseen mail, and rebuilds affected live issue inputs. It does not freeze an issue, invoke the editor, post Discord notifications, contact SMTP, or send mail.

For a future bot, call `scripts/manual_ops.py` directly with `--issue`, `--candidate`, `--action`, `--reason`, and `--yes` as appropriate. Do not pass untrusted text through a shell. The tool serializes its own operations, and pauses and restores the three scheduled Circus tasks during live mutations. It preserves tasks that were already disabled. If it reports that task state could not be restored, inspect Task Scheduler before relying on automation.

Examples from the installation folder:

```powershell
.\.venv-host\Scripts\python.exe scripts/manual_ops.py status --issue 2026-W42
.\.venv-host\Scripts\python.exe scripts/manual_ops.py retry --issue 2026-W42
.\.venv-host\Scripts\python.exe scripts/manual_ops.py promote --issue 2026-W42 --candidate '2026-W42-ai-retry-YYYYMMDDTHHMMSSZ.json'
.\.venv-host\Scripts\python.exe scripts/manual_ops.py delivery --issue 2026-W42 --action hold --reason 'Review in progress'
.\.venv-host\Scripts\python.exe scripts/manual_ops.py scan --yes
```

Do not release a hold if you still need to review the issue. Automatic delivery may become due immediately after release. An existing issue delivery journal requires manual reconciliation; these controls will not replace that issue's draft or edit its journal.

The delivery journal distinguishes a clear pre-send connection failure from an uncertain SMTP result. Automatic retries occur 20 minutes apart, at most three times, within the two-hour window. For an uncertain result, inspect the journal and Gmail Sent Mail before considering another send. `send_due.py --send-late` bypasses the time window, but does not clear an uncertain journal. After the automatic retry limit, `send_due.py --issue WEEK --kind KIND --approve-safe-retry` permits a reviewed manual attempt if the pending recipients are known not to have been sent; add `--send-late` if the window has closed. Neither flag bypasses an uncertain journal. Do not delete or edit the journal to force another send.
