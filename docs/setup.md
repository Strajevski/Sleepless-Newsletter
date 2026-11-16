# Setup and readiness checks

The first supported platform is Windows with Python 3.11 or newer. Run these commands from the installation folder, under the Windows account that will eventually run the newsletter. Codex CLI is a separate prerequisite; the doctor reports if it is missing or not signed in. No command in this guide publishes the project or enables scheduled delivery.

## Guided setup

```powershell
python scripts/setup.py
```

For a new installation, setup asks for the newsletter/editor names, Gmail or Google Workspace address, and data directory, then creates the ignored settings.json from the fictional example. The sender address matches the login address. Other settings, including the existing Discord default, keep their configured values. An empty private roster is created if none exists; populate it using config/roster.example.json as a structural example. Do not leave fictional recipient addresses in the real roster.

Setup creates .venv-host using the Python that launched it, installs all three project requirement files into that environment, and finishes by running offline readiness checks with that environment's interpreter. It does not install dependencies into the global Python environment. Paths with spaces are supported. Existing settings, roster, runtime files, credentials, and a valid local environment are preserved. Old installations without settings.json can migrate their existing mail.json account without switching to the example account.

If no email credential exists, interactive setup offers to run the existing hidden app-password setup. This authenticates to Gmail IMAP and selects the mailbox read-only before storing the app password using Windows DPAPI and restricted permissions. It does not fetch messages or send mail. Existing credentials are not replaced automatically. Passwords are never supplied in command arguments or written into settings.json.

If local checks fail, setup returns a nonzero exit code and reports what needs attention. This is an incomplete setup, not an instruction to delete data. An interrupted or copied environment is not silently removed: inspect or rename .venv-host and rerun setup with a supported Python. A failed dependency installation can usually be retried by rerunning setup. Keep archives and settings in place.

## Explicit configuration options

| Option | Behaviour |
| --- | --- |
| `--root PATH` | Configure that installation folder, not a bare runtime-data folder. |
| `--configure` | Explicitly reopen the identity/account/data prompts for existing settings. Other options, headings, and notification choices are preserved. |
| `--configure-credentials` | Explicitly run hidden app-password configuration, including when a credential already exists. |
| `--non-interactive` | Preserve existing configuration, or create fictional example settings and an empty roster without prompting. It does not enter passwords; edit the configuration and complete authentication separately. |
| `--skip-dependencies` | Skip pip installation. The environment is still created/validated, and the doctor reports missing dependencies. Useful when dependencies have already been installed or internet access is unavailable. |

Stop any existing automation before changing a working installation's identity, account, or data location. Setup does not stop or restart already-registered tasks. Changing storage.data_directory does not migrate data; follow the [directory migration guidance](settings.md#application-and-runtime-directories), including preservation of delivery journals. Normal repeat setup does not change these settings unless --configure is specified.

After initial setup, use the environment's interpreter for commands:

```powershell
.\.venv-host\Scripts\python.exe scripts/configure_imap.py --check
.\.venv-host\Scripts\python.exe scripts/check_smtp.py
```

These two commands verify stored email authentication without sending. To create or replace the credential, run configure_imap.py without --check. If Codex CLI is not signed in, run `codex login` under the intended Windows account. Where codex.executable or codex.home_directory overrides are set, use that executable and login directory when signing in; the doctor honours those same settings.

## Readiness check

```powershell
.\.venv-host\Scripts\python.exe scripts/doctor.py
```

The default is offline. It checks the Python version, Windows support, installed dependency versions and timezone data, settings, roster, schedule, runtime paths and write access, selected prompts/schemas, template rendering, saved Codex CLI login status, and encrypted credential availability/access permissions. Folder checks use temporary files that are immediately removed; missing runtime folders are not created. A fictional preview is rendered only in memory. The selected model's actual availability and output quality are not tested because that would require a model request.

To additionally verify service access:

```powershell
.\.venv-host\Scripts\python.exe scripts/doctor.py --online
```

Online mode authenticates to IMAP and selects the configured mailbox read-only, authenticates to SMTP, and performs the existing read-only Codex usage query. It never fetches inbox messages, submits email, posts Discord messages, starts an AI generation, purchases credits, or edits account settings. Email authentication is skipped if a usable protected credential is unavailable. Blocked ordinary AI usage is reported according to the existing usage/credit policy; quota thresholds can produce a warning because the normal editor retry/fallback behaviour still applies.

Optional Discord configuration is checked locally when enabled and a webhook exists. No webhook is created or tested by posting. A missing optional webhook is reported as skipped, preserving the current behaviour.

```powershell
.\.venv-host\Scripts\python.exe scripts/doctor.py --offline --json
.\.venv-host\Scripts\python.exe scripts/doctor.py --root C:/Newsletter --online
```

Results use pass, fail, warn, and skip statuses with actions for failures. Exit code 0 means the requested checks passed; exit code 1 means attention is needed. Offline success does not establish online authentication. JSON output identifies the mode and omits passwords, webhook URLs, account details, and raw service error text. Keep reports local until the later privacy/publication review is complete.

## Automation and later release work

Setup deliberately has no enable-automation option. Once the installation and recipient list have been reviewed, the [separate task installers](schedule.md#windows-task-upgrade-once) can be run explicitly under the intended Windows account. Passing a doctor check does not enable them or authorise sending.

The fresh-install rehearsal and final publication/security audit are separate next steps. Before sharing a release, review the actual release files and any Git history that would be published for private or sensitive content. No setup or doctor command performs that audit or publishes anything automatically.

## Uninstall

Use `.\scripts\uninstall.ps1` to preview uninstall and add `-Apply` to confirm removal of owned tasks and the local environment. Settings, data, and credentials remain by default. The [uninstall guide](uninstall.md) explains explicit credential deletion, task ownership checks, and local-only path protections. Task installers refuse same-named tasks belonging to a different installation.

For a registration-only rehearsal, both task installers support `-Disabled`. This sets the task-level disabled flag before registration, so triggers cannot start work. Inspect the resulting paths and state as described in [the combined guide](../INSTALL-UNINSTALL.md#5-optional-verify-task-registration-without-running-the-newsletter). Omit this switch only for deliberate live activation.
