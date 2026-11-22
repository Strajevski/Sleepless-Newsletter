# Installing and uninstalling on Windows

This guide takes you from a downloaded copy to an automatically delivered weekly newsletter. Run commands in PowerShell from the installation folder, using the Windows account that will run the newsletter. Setup does not enable automation; you do that explicitly after configuring and checking the installation.

## 1. Requirements

- A Windows computer that can stay awake and connected to the internet during scheduled operations. Task Scheduler runs the newsletter; the terminal does not need to stay open.
- [Python](https://www.python.org/downloads/) 3.11 or later, available through the `python` command.
- [OpenAI Codex CLI](https://developers.openai.com/codex/cli), signed in with a ChatGPT account that has Codex allowance and access to the configured editing and image-captioning models. This release uses subscription usage checks; API-key-only billing and other model providers are not supported. Follow the official CLI installation instructions, including prerequisites for your chosen installation method.
- A dedicated Gmail or Google Workspace mailbox with 2-Step Verification and app-password access. Workspace administrators must permit IMAP, SMTP and app passwords. Other email providers and Google OAuth sign-in are not supported by this release.
- A writable local installation folder and permission to register scheduled tasks. Registration asks for the Windows account password, not a Windows Hello PIN.

Git is optional when installing from a ZIP. Setup installs the required Python dependencies into a local environment. Discord notifications are optional.

Check Python and Codex in a new PowerShell window after installing them:

```powershell
python --version
codex --version
```

## 2. Download and choose a folder

Download and extract the chosen release ZIP, or clone the repository. Use a local folder you can write to, for example `C:\Users\YourName\AppData\Local\SleeplessNewsletter`. Avoid `Program Files`, network shares and mapped network drives for a new installation. You should not need administrator rights simply to write configuration or runtime files.

Open PowerShell in the extracted folder. It should contain `scripts`, `config`, `templates` and this guide. For example:

```powershell
cd "$env:LOCALAPPDATA\SleeplessNewsletter"
```

Use your actual extraction folder if its name differs. Do not copy an existing `.venv-host` environment or encrypted credentials to another computer; create them on the destination computer instead.

## 3. Prepare Gmail and Codex

Enable Google 2-Step Verification and create an app password using [Google's instructions](https://support.google.com/accounts/answer/185833). Some managed or protected accounts cannot create app passwords; confirm availability before proceeding. No Google Cloud project or Gmail API key is needed.

The mailbox receives submissions and sends issues, reminders and invitations. Enter its app password at the hidden local prompt during setup. Do not put your normal Google password or the app password in JSON, a command line or a GitHub file.

Sign in to Codex under the Windows account that will run the scheduled tasks:

```powershell
codex login
```

Choose ChatGPT sign-in. If you later use a different Windows account for automation, configure that account's login and credential access too.

## 4. Run setup

```powershell
python scripts/setup.py
```

Setup asks for the newsletter name, short name used in greetings, editor name, mailbox address and data directory. The editor name is an editorial persona, not a model selection. Keep the data directory as `.` for a straightforward installation: private runtime folders stay inside the installation folder, regardless of the terminal's working directory.

Setup creates private `config/settings.json`, an empty `config/roster.json`, and the `.venv-host` Python environment with dependencies installed. When offered Gmail app-password configuration, answer `y` if you have prepared it. The helper authenticates to IMAP, selects the mailbox read-only, and stores the password locally using Windows DPAPI and restricted permissions. It does not fetch submissions or send email.

If you skip credential setup, complete it later:

```powershell
.\.venv-host\Scripts\python.exe scripts/configure_imap.py
```

Setup finishes with readiness checks. Failures for an empty roster, missing credential or missing Codex login are expected until those steps are complete. Follow the reported next actions; do not delete settings or data to clear a failure. Repeating setup preserves existing settings, roster, credentials and archives.

Use `.\.venv-host\Scripts\python.exe` for subsequent Python commands so they use the installed dependencies. The leading `.\` means “from this folder”; environment activation is unnecessary. To reopen the existing identity/account/data prompts, run `python scripts/setup.py --configure`. See [setup options and troubleshooting](docs/setup.md) for more detail.

## 5. Add contributors

Edit `config/roster.json`, using `config/roster.example.json` as a structural example. Each member needs a unique lowercase ID beginning with a letter; letters, digits, underscores and hyphens are allowed. List the email addresses from which that person will submit. The first address is also their newsletter delivery address.

```json
{
  "members": {
    "alex": {
      "name": "Alex",
      "emails": ["alex@example.com"],
      "pronouns": ""
    },
    "bob": {
      "name": "Bob",
      "emails": ["bob@example.com"],
      "pronouns": "he/him"
    }
  }
}
```

Replace these fictional names and addresses before proceeding. Add yourself if you want to receive the newsletter. Do not assign one email address to multiple members. For the first functional cycle, a roster containing only your own real recipient address keeps delivery under your control. The `pronouns` field is optional. If left blank, the editor may infer pronouns from the contributor’s name when needed, which can lead to mistakes. Populate it to avoid ambiguity.

## 6. Review timing, models and identity

| File | What to configure |
| --- | --- |
| `config/settings.json` | Newsletter/editor names, headings, mailbox, data directory, Codex overrides and optional Discord switch. |
| `config/schedule.json` | Deadlines, reminders, invitations, delivery times, retries, timezones and issue numbering. |
| `config/editor.json` | Editing model, reasoning effort, prompt and timeout. The selected prompt is `prompts/edited-v2.0.md`, including optional Long and Short entries. |
| `config/captioner.json` | Image-captioning model, prompt and limits. Set `enabled` to `false` for neutral image attribution instead of AI captions. |
| `config/delivery.json` | Automatic fallback-delivery policy and issue holds. A hold prevents that issue being sent until released. |

The bundled schedule uses UTC for operational events. Defaults are: Monday 03:00 submission cutoff, 04:00 image preparation, 07:00 deferred editor retry and 08:00 issue delivery. Reminders are Saturday 17:00. Invitations for the next issue follow the previous issue's successful delivery. Inbox scans normally occur every 30 minutes, with extra checks at configured stage times.

Choose `first_issue_week` and `first_issue_number` for your newsletter. The bundled baseline is an example, not an instruction to start at issue 2. An issue ID names the ISO week ending on the Sunday before the default Monday cutoff: `2026-W47` closes on Monday 23 November 2026 for example (the 47th week of 2026). Set your intended first issue as the baseline and use `1` to start numbering at 1. Keep the baseline stable after sending issues.

Preview a cycle without contacting services, replacing the example issue ID with yours:

```powershell
.\.venv-host\Scripts\python.exe scripts/schedule.py --issue 2026-W47
```

See the [schedule guide](docs/schedule.md) for timezones, daylight saving and delivery windows, and the [settings reference](docs/settings.md) for other configuration. Missing a sending window does not cause arbitrarily late automatic delivery; review the state before attempting recovery.

Model access varies by account. The bundled model IDs do not guarantee your account can use them. Readiness checks validate configuration without making a model request; actual access and editorial quality need a functional trial.

The editor's usage gate requires Codex to report that paid credits are unavailable. Accounts reporting an available paid-credit balance are blocked by this policy rather than allowed to spend it. Review any usage-check failure before relying on automated editing.

By default, `auto_send_fallback` is `true`: if AI editing remains unavailable after the normal retry/fallback process, the collected original submissions can be delivered using the fallback layout. Set this to `false` if fallback issues should require manual review instead of automatic delivery. Text and, when captioning is enabled, submitted images are processed by OpenAI; self-hosting the scripts does not make model processing local.

### (Optional) Discord Webhook setup
No webhook means no posts; set `notifications.discord_enabled` to `false` in settings to disable dispatch explicitly. If you configure a webhook with `scripts/configure_discord.py`, that helper sends a real setup notification before storing it. Keep the webhook private.

1. 1. [Create a webhook](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks) for the desired Discord channel.
2. Run:

   ```powershell
   .\.venv-host\Scripts\python.exe scripts/configure_discord.py
   ```
3. Paste the webhook URL at the hidden prompt. This sends a real setup notification before storing it securely.
4. Ensure `notifications.discord_enabled` is `true` in `config/settings.json`.
5. To send another test notification:

   ```powershell
   .\.venv-host\Scripts\python.exe scripts/configure_discord.py --check
   ```


## 7. Check before enabling automation

```powershell
.\.venv-host\Scripts\python.exe scripts/doctor.py --offline
.\.venv-host\Scripts\python.exe scripts/doctor.py --online
```

Offline mode checks local configuration, dependencies, writable paths, prompts/templates, saved Codex login and protected credentials. Online mode additionally authenticates to IMAP/SMTP and queries Codex usage read-only. Neither fetches inbox contents, sends email or Discord notifications, runs a model, or registers tasks. Resolve failures before continuing. A usage warning can mean the normal retry/fallback policy will apply.

Optionally send one real transport test to the newsletter mailbox itself, never to the roster:

```powershell
.\.venv-host\Scripts\python.exe scripts/send_smtp_self_test.py
```

Confirm it arrives. This checks sending, not the whole unattended workflow. Use a self-only roster and a harmless submission for your first functional cycle before inviting the full group.

For that first submission, send an ordinary email from an address listed in your roster to the newsletter mailbox before the configured cutoff. Include a short update and optionally an image. To exercise the new columns, add a distinct paragraph such as `Long on: walking; classical music` and `Short on: delayed trains`. The editor should only use explicitly intended positions in these columns. After activation, check intake before the deadline and then the generated issue and delivery at the scheduled times; do not manually start sending merely because a task is shown as `Ready`.

## 8. Register and enable automation

Setup does not register tasks. For a new installation, register the two supplied tasks initially disabled:

```powershell
.\scripts\install_final_build_task.ps1 -Disabled
.\scripts\install_delivery_task.ps1 -Disabled
```

Enter the intended Windows account password when prompted. Both installers default to the current account and this installation folder. Task names are shared on a Windows computer; the installers refuse to overwrite another installation's tasks. If Windows policy blocks a downloaded script or registration is denied, inspect the error and use approved permissions rather than disabling machine-wide protections.

Inspect the tasks:

```powershell
$taskNames = 'Circus Newsletter Final Build', 'Circus Newsletter Delivery'
Get-ScheduledTask -TaskName $taskNames -TaskPath '\' |
    Select-Object TaskName,State
Get-ScheduledTask -TaskName $taskNames -TaskPath '\' | ForEach-Object {
    [pscustomobject]@{
        Task = $_.TaskName
        Program = $_.Actions.Execute
        Arguments = $_.Actions.Arguments
        WorkingDirectory = $_.Actions.WorkingDirectory
        Account = $_.Principal.UserId
        RepeatEvery = $_.Triggers.Repetition.Interval
    }
} | Format-List
```

Both should be `Disabled`. The program must be this installation's `.venv-host\Scripts\python.exe`, the working directory must be this installation folder, and the account must be the intended Windows account. Final Build calls `scripts\schedule.py --run-intake`; Delivery calls `scripts\send_due.py`. Both repeat every minute (`PT1M`). These lightweight checks decide when work is due; they do not fetch email or call a model every minute.

Once configuration, checks and recipients are ready, deliberately enable automation:

```powershell
Enable-ScheduledTask -TaskName 'Circus Newsletter Final Build' -TaskPath '\'
Enable-ScheduledTask -TaskName 'Circus Newsletter Delivery' -TaskPath '\'
```

Scheduled work can now fetch submissions, run models, post configured Discord notifications and send messages to the roster when due. Check the first scheduled cycle's logs, generated issue and delivery status. `Ready` means enabled and waiting; it does not establish a successful run.

Existing installations with a separate Intake task should follow [the task migration notes](docs/schedule.md#windows-task-upgrade-once). Do not add a duplicate schedule or replace an existing layout blindly.

## 9. Maintain or move an installation

Back up private settings, roster and runtime folders, including delivery journals and notification state. These records prevent repeated delivery. Git ignores their standard locations; it is not a backup of those files.

Pause automation before changing a working installation's mailbox, data location or source release. Do not move an installation while tasks still point to its old path. Recreate `.venv-host` at the destination and register tasks for the new location. Another machine needs newly configured credentials and its own Codex login. Changing `storage.data_directory` alone does not move existing data. See [directory migration](docs/settings.md#application-and-runtime-directories) and [development/deployment](docs/development-and-production.md).

## 10. Uninstall

Close manually launched newsletter processes, then preview uninstall from the installation folder:

```powershell
.\scripts\uninstall.ps1
```

Check that the plan identifies the intended installation and only its owned tasks and local environment. Then apply:

```powershell
.\scripts\uninstall.ps1 -Apply
```

Type `UNINSTALL` to confirm. The script removes recognised tasks owned by this installation and `.venv-host`. It preserves application files, settings, roster, credentials, submissions, issues, attachments, delivery journals, logs, state and external runtime storage. Shared Python, Codex CLI and Codex login are not removed.

To remove the locally stored email/Discord credentials as well, preview first and then apply:

```powershell
.\scripts\uninstall.ps1 -RemoveCredentials
.\scripts\uninstall.ps1 -Apply -RemoveCredentials
```

Deleting stored credentials does not revoke them at Google or Discord; revoke them separately if no longer needed. After backing up anything you want to retain and checking for external storage, you can manually delete the remaining application folder. External data remains until deliberately removed separately.

To reinstall with retained configuration and data, run `python scripts/setup.py`, repeat checks, and register tasks when ready. If uninstall reports a failure, inspect it before continuing: tasks may already have been disabled or removed, while data is retained. See [uninstall details](docs/uninstall.md) for ownership rules and local-path protections.
