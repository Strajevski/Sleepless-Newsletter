# Circus Newsletter

A Windows newsletter pipeline for collecting email submissions, preparing AI-edited or deterministic fallback drafts, rendering HTML/plain text, and delivering scheduled issues. Private settings, credentials, roster, submissions, delivery journals, and local operational tools are excluded from the release source.

## Install, check, and uninstall

See [INSTALL-UNINSTALL.md](INSTALL-UNINSTALL.md) for the complete controlled-rehearsal workflow, including Git preparation and privacy checks. Use a local, user-writable folder such as a NewsletterTest folder under your Windows Local AppData directory. The current portable layout writes configuration and its Python environment beside the code, so Program Files is unsuitable without changing the application layout; do not elevate setup to work around that.

```powershell
python scripts/setup.py
.\.venv-host\Scripts\python.exe scripts/doctor.py
.\.venv-host\Scripts\python.exe scripts/doctor.py --online
.\scripts\uninstall.ps1
```

Setup creates or reuses the local .venv-host environment and preserves existing settings and data. Doctor runs local checks by default; --online adds read-only authentication and usage checks without sending messages or generating AI content. Uninstall previews by default; add -Apply for confirmed removal of owned tasks and the environment, preserving private data and credentials. Automation requires separate, deliberate task installation. Details: [setup](docs/setup.md), [uninstall](docs/uninstall.md).

## Configuration

| File | Responsibility |
| --- | --- |
| config/settings.json | Private newsletter/editor identity, headings, email connections, Codex overrides, notification switch, and data directory. Start from settings.example.json. |
| config/roster.json | Private members, accepted sender addresses, optional pronouns, and delivery addresses. Start from roster.example.json; never use fictional recipients for live delivery. |
| config/schedule.json | Cutoff, image preparation, editor retry, reminder, invitation, delivery windows/retries, and inbox frequency. |
| config/editor.json and config/captioner.json | Models, reasoning effort, prompt selection, processing limits, and captioning enablement. |
| config/delivery.json | Sending policy and issue holds. |
| config/relays.json | Empty public relay default. |
| config/relays.local.json | Optional private relay configuration, used instead of the public default when present. Start from relays.example.json and keep real identities private. |
| config/secrets/ | Machine-bound Windows DPAPI credentials with restricted permissions. Never copy these as portable credentials. |

See [settings](docs/settings.md) and [schedule](docs/schedule.md) for fields and examples. storage.data_directory defaults to the installation folder, independent of the terminal's working directory. Runtime folders (issues, state, logs, revisions, announcements, output) can move to a separately configured data folder; code, configuration, templates, prompts, and credentials remain installation-relative. Changing this setting does not move existing data: stop automation and preserve all journals during migration.

Gmail/Google Workspace remains the supported provider, using IMAP over implicit TLS on 993 and SMTP over implicit TLS on 465. Intake requires Gmail's unique-message-ID extension. Enter app passwords only through the hidden local setup prompt. Codex uses its saved CLI login; API-key environment variables are removed from child processes. Optional Discord dispatch requires both its configured switch and a stored webhook; missing webhooks do nothing.

## Default weekly cycle

These are the supplied defaults, all UTC year-round. Edit schedule.json to change them; they are not embedded in the task triggers.

| Event | Default |
| --- | --- |
| Submission reminder | Saturday 17:00 |
| Submission cutoff | Monday 03:00 |
| Image-size preparation | Monday 04:00 or later, once a draft exists |
| One deferred editor retry | Monday 07:00 |
| Issue delivery | Monday 08:00 |
| Next issue invitation | After complete delivery, within its configured invitation window |

Normal intake checks occur every 30 minutes, plus configured stage checks. The clock tasks check every minute. A missed stage catches up on the next successful intake; automatic sending has a two-hour window by default and does not send unexpectedly late. Safe SMTP failures can retry up to three times, at least 20 minutes apart. An uncertain SMTP outcome stops automatic retries for manual reconciliation, protecting against duplicates. Review holds, empty issues, oversized messages, and incomplete previous delivery can block outgoing campaigns.

Before editing, weekly usage must be below 90% and five-hour usage below 80%; the read-only query must establish that paid credits are unavailable. Usage-related failures defer to the configured retry, then fall back if still blocked. Other editing failures fall back immediately. Completed drafts are not automatically regenerated. The fallback uses the normal template with eligible source text and an empty editorial section; sending policy controls whether it may be delivered automatically.

Captioning runs after text editing and before Gallery rendering. It inspects temporary small copies of eligible still images; failures use neutral captions. Outgoing image optimisation happens afterward, preserving archived originals and the original draft. The complete outbound message must fit 20 MiB; optimisation aims for 18.5 MiB. Non-image attachments are not compressed. If the message remains oversized, delivery holds it.

## Runtime records and privacy

Each unique source message is archived separately. Raw emails, parsed submissions, attachment bytes, rolling input, frozen input, draft artifacts, caption manifests, optimisation manifests, and outbound journals live under issues/YYYY-Www in the configured data directory. Unknown, duplicate, empty, test, administrative, and late-after-finalisation records are held where appropriate rather than silently discarded. Intake reads IMAP without changing mailbox state. Conservative reply-history cleanup keeps archived originals intact.

Outbound journals record acceptance per recipient. Recipients are supplied privately through the SMTP envelope, not exposed as a roster in message headers. Preserve these journals and state during updates, backups, moves, and reinstalls. Do not delete journals to force another send. The editor can use the four most recent fully delivered earlier issues as context; held or incomplete deliveries are excluded.

Back up private configuration, runtime archives/state, and any private references separately from release source. Encrypted credentials should be recreated after replacing a host rather than copied as portable credentials. Before publishing, review the actual release snapshot, images/artifacts, and any history you intend to share; ignore rules alone do not clean tracked content or old commits.

## Manual commands and trials

Issue commands take --root as the installation folder containing config, then resolve its data directory. See [manual controls](scripts/manual/README.md) for status, retry, promotion, review holds, and controlled inbox scans.

```powershell
.\.venv-host\Scripts\python.exe scripts/schedule.py --issue 2026-W47
.\.venv-host\Scripts\python.exe scripts/manual_ops.py status --issue 2026-W47
.\scripts\run_trial.ps1
```

The trial launcher uses this installation only by default. To read another installation, explicitly pass -DataRoot or set CIRCUS_DATA_ROOT to that installation folder; there is no automatic external/NAS fallback. CIRCUS_PYTHON can explicitly select an interpreter. Trial output is isolated from the source archive, and copied settings reset runtime storage to the trial folder. Trials may call the configured AI services, but do not send mail.

For offline image-size work, the reusable media_optimizer module accepts explicit source/output paths. Historical one-off production archive/repair scripts are private local material and are not part of the release.

Task XML files are installer templates with placeholder account/path values. Use install_final_build_task.ps1 and install_delivery_task.ps1 rather than directly importing those templates. Installers set the selected local account, interpreter, scripts, and working directory, and refuse same-named tasks belonging elsewhere. By default they enable tasks immediately; use -Disabled for registration-only testing, and review the account and roster before real activation.

## Tests

```powershell
python -m unittest discover -s tests -q
```

Fixtures use fictional people and reserved example email domains. Network/service interactions and scheduled-task changes are mocked where appropriate. Windows junction tests can require additional permissions; a skipped fixture is not equivalent to a passed integration check. Real authentication and unattended task execution must still be rehearsed on the intended host.
