# Install, rehearse, and uninstall on Windows

This is the combined user guide for the portable Windows version. It covers installation, a controlled local rehearsal, optional task activation, and uninstall. Use a local folder on the test computer; the NAS installation is outside this rehearsal. Never use an existing production installation, a network share, production credentials, or the production roster for the rehearsal.

The installation tools have individual tests, but a complete fresh-machine rehearsal is still to be performed. Nothing in this guide uploads or publishes the project. Complete the Git and privacy preparation below before transferring a candidate snapshot.

## 1. Prepare a reproducible candidate in Git

Check the development checkout for uncommitted changes and new files. A clone or archive of its last commit will not include the new setup/doctor/uninstall tools until their files are committed. Confirm which private development branch you are using; its last commit may not include uncommitted work.

Create a private checkpoint branch if you want to separate this work from the existing branch:

```powershell
git switch -c codex/portable-install-rehearsal
git status --short
git diff --check
```

Review and stage the intended changes using the Git UI or explicit file paths. Include the new Python modules, scripts (including task_ownership.ps1), tests, documentation, fictional example settings/roster, updated templates/prompts, configuration defaults, and the intended deletion of the old tracked mail.json. Do not use an indiscriminate `git add .`.

Keep the real settings.json, roster.json, relays.local.json, private/, config/secrets, environments, runtime data, source emails, friends' submissions, attachments, logs, revision backups, generated output, and private reference files out of the commit. The existing ignore rules cover their usual locations, but ignore rules do not remove information that is already tracked. Leave unrelated workflow/design files out unless separately reviewed. Thumbs.db is a Windows cache, not a release file.

Before committing:

```powershell
git diff --cached --name-status
git diff --cached
git check-ignore config/settings.json config/roster.json config/secrets/imap-password.dpapi
```

Confirm that every intended new tool and test appears in the staged list and that no private file has been included. A local checkpoint commit is not publication:

```powershell
git commit -m "Prepare portable installation and uninstall rehearsal"
git rev-parse HEAD
git status --short
```

Record the resulting commit ID, and record the final reviewed commit ID again if sanitisation changes it. Leave unrelated local files uncommitted if appropriate. Do not push this checkpoint to a public remote or attach an archive to a public issue.

### Privacy gate before transferring or publishing

The development source has had a privacy cleanup: operational/archive tools and private notes are kept locally under ignored private/, real relay identity is kept in ignored relays.local.json, and public fixtures/docs/XML use fictional or generic values. A source scan is not a review of the private repository's old history. Review the exact committed candidate and ZIP again before transferring or publishing; a ZIP made before this cleanup still contains the older files. See [the privacy cleanup report](docs/PRIVACY-REVIEW.md).

Review the exact candidate snapshot, sanitise or exclude private/historical material, and commit the reviewed result before exporting it. If publishing a repository with history, review that history too: deleting a file in the latest commit does not remove its older versions. Any exposed real credential must be revoked/rotated, not merely deleted from the tip. A clean snapshot does not make its parent history clean: do not push a private checkpoint branch merely because its exported ZIP is sanitised. Keep private checkpoints separate from a later public release history if necessary.

After that review, create a snapshot of the approved commit from the repository folder:

```powershell
git archive --format=zip --output=../circus-rehearsal.zip HEAD
```

Inspect the ZIP listing before transferring it. git archive exports committed tracked files, including any tracked private files that survived the review. It excludes ignored local settings/data and does not include .git/history. Do not substitute a ZIP of the entire working directory, copy the NAS folder, or copy an existing virtual environment. Use this private reviewed ZIP for the first rehearsal; publishing to GitHub is unnecessary.

## 2. Prepare the test computer

Use Windows with Python 3.11 or newer and Codex CLI installed. PowerShell 7 is recommended; the uninstall tests also cover Windows PowerShell 5.1. Git is optional when using the reviewed ZIP. No shared Python or Codex installation will be removed during uninstall.

Extract the candidate to a new local folder, for example C:/CircusRehearsal. Open PowerShell in that folder. Check the prerequisites:

```powershell
python --version
codex --version
```

Use a dedicated Gmail/Google Workspace test account and only your own address as the rehearsal recipient. Do not copy the existing encrypted credentials: Windows DPAPI storage is tied to the source machine. Do not connect this rehearsal to your friends' roster or real submissions. Keep all rehearsal-generated settings/data local.

If Windows blocks a reviewed PowerShell script, stop and inspect the execution-policy/download-block message. Do not lower a machine-wide policy just to continue. Use a shell/session permitted by your normal Windows policy after reviewing the script; the tests do not require changing a machine-wide policy.

## 3. Install and configure

```powershell
python scripts/setup.py
```

For a new installation, answer the newsletter/editor identity prompts, enter the test account, and keep the data directory as `.` for the first rehearsal. This means the local application folder, not the terminal's current directory. Setup creates .venv-host, installs the project's dependencies into that environment, creates private settings and an empty roster, and offers hidden Gmail app-password setup. Existing settings and archives are preserved on repeat runs. Discord retains its configured enabled flag, but without a webhook it sends nothing; no webhook setup is needed for this rehearsal.

If you choose email credential setup, enter a Google app password at the hidden local prompt. Never put it into settings.json, a command argument, a report, or this guide. The setup step checks IMAP read-only before storing the password securely. It sends no email.

An initial nonzero result is expected while the roster, CLI login, or credentials are incomplete. Follow the doctor's Next actions rather than deleting settings or archives. Fill config/roster.json using config/roster.example.json as a structural guide, with exactly one real rehearsal member: yourself. Replace the entire placeholder below, including angle brackets:

```json
{
  "members": {
    "self": {
      "name": "Rehearsal",
      "emails": ["<your own test email address>"],
      "pronouns": ""
    }
  }
}
```

Sign into Codex under the Windows account that will run the rehearsal:

```powershell
codex login
```

If needed, configure the test app password separately and inspect the resolved data directory:

```powershell
.\.venv-host\Scripts\python.exe scripts/configure_imap.py
.\.venv-host\Scripts\python.exe scripts/data_directory.py
.\.venv-host\Scripts\python.exe scripts/schedule.py --issue 2026-W47
```

The resolved directory must be your local rehearsal folder, never an existing production location or network share. The schedule command is a non-sending preview; use the relevant issue ID if rehearsing later. Models/prompts/headings live in their documented configuration files. Do not change schedules in production while following this guide.

## 4. Check the installation without enabling automation

```powershell
.\.venv-host\Scripts\python.exe scripts/doctor.py
.\.venv-host\Scripts\python.exe scripts/doctor.py --online
```

The first command runs local checks. The second additionally authenticates to IMAP/SMTP and queries Codex usage read-only. Neither fetches inbox contents, generates AI text, submits email, posts a Discord message, or enables tasks. Follow failures before proceeding. Offline success does not verify service authentication; a usage warning may be consistent with the normal retry/fallback policy. Model availability and output quality still require a later functional newsletter test.

Optional checks, performed manually only after confirming the test account:

```powershell
.\.venv-host\Scripts\python.exe scripts/render_preview.py
.\.venv-host\Scripts\python.exe scripts/send_smtp_self_test.py
```

The first writes the bundled fictional sample under templates/. The second really sends one [TEST] message to the configured sender account only, never the roster. Skip it if you want a completely non-sending first rehearsal. Confirm the test message arrives if you run it.

Rerun setup to verify repeat installation preserves your settings, roster, and existing files:

```powershell
python scripts/setup.py --non-interactive
```

Do not copy real issues just to exercise archive preservation. For the first environment-only uninstall check, a harmless text marker in the rehearsal's issues folder is sufficient; the individual tests already cover delivery journals and duplicate-send protection with synthetic data.

## 5. Optional: verify task registration without running the newsletter

Use the current installer scripts with -Disabled to register the real tasks without enabling any triggers. This can be tested with a dummy email account, an empty roster, and no Codex login. It verifies Windows task registration, paths, account, and trigger definitions—not inbox handling or unattended execution. Older extracted copies may not have -Disabled; update both task installers and uninstall.ps1 from the reviewed development source before using this option. If -Disabled is unrecognised, stop rather than retrying without it.

```powershell
.\scripts\install_final_build_task.ps1 -Disabled
.\scripts\install_delivery_task.ps1 -Disabled
```

The installers prompt locally for the intended Windows account password, not a Windows Hello PIN. Do not put it into a script or settings. Task names remain shared on one Windows host; ownership guards refuse to overwrite another installation's tasks. Run this on the test computer, not on a production host.

```powershell
$taskNames = 'Circus Newsletter Final Build', 'Circus Newsletter Delivery'
$tasks = Get-ScheduledTask -TaskName $taskNames -TaskPath '\' -ErrorAction Stop
$tasks | Select-Object TaskName,State
$tasks | ForEach-Object {
    [pscustomobject]@{
        Task = $_.TaskName
        Enabled = $_.Settings.Enabled
        Program = $_.Actions.Execute
        Arguments = $_.Actions.Arguments
        WorkingDirectory = $_.Actions.WorkingDirectory
        RepeatEvery = $_.Triggers.Repetition.Interval
        Account = $_.Principal.UserId
    }
} | Format-List
```

Both states must be Disabled and Enabled must be False. Each program must point at this local installation's .venv-host/Scripts/python.exe. The working directory must be this installation folder. Final Build runs scripts/schedule.py --run-intake; Delivery runs scripts/send_due.py. Both repetition intervals must be PT1M (one minute). The configured account must be the intended local Windows account. Do not use Run or Enable in Task Scheduler for this registration-only test. A never-run result is expected, not an execution failure.

If registration fails or inspection is denied, retain the error for diagnosis; do not assume success or loosen system permissions. The check needs the account's normal rights to register and inspect its tasks.

Then continue to uninstall below to verify removal of the disabled tasks. Actual service authentication and unattended operation remain separate acceptance tests. For a future real activation, review account, roster, data and service checks first, then deliberately run the installers without -Disabled; by default they enable tasks immediately. No activation is needed for this rehearsal.

## 6. Preview uninstall, then apply

Close any manually launched newsletter/Python processes. Run uninstall using PowerShell, not the environment that is about to be removed:

```powershell
.\scripts\uninstall.ps1
```

Review the plan. It must identify the local rehearsal installation, show only its recognised owned tasks (if any), and target only that folder's .venv-host. Other installations' tasks are left alone. Network installation roots, links/junctions, unrecognised environment users, and private runtime data inside the environment cause refusal rather than broader deletion.

```powershell
.\scripts\uninstall.ps1 -Apply
```

Type `UNINSTALL` to confirm. The command removes the selected installation's owned tasks and local environment. Application files, settings, roster, credentials, data, journals, logs, state, and external storage are retained. Shared Python, Codex CLI, and its login remain installed. No NAS location is deleted. If task handling fails, the environment and credentials remain for inspection; previously handled tasks may already be disabled/removed.

Check the local environment is gone and private files remain:

```powershell
Test-Path .venv-host
Test-Path config/settings.json
Test-Path config/roster.json
```

Expect False, True, True respectively after successful removal. Check any harmless archive marker and stored credential still exist. If task activation was included, verify those rehearsal tasks are absent from Task Scheduler while unrelated tasks remain. Run uninstall preview again to verify repeat removal needs no further action.

## 7. Optional credential cleanup or reinstall

To explicitly remove the two local stored credentials as well:

```powershell
.\scripts\uninstall.ps1 -RemoveCredentials
.\scripts\uninstall.ps1 -Apply -RemoveCredentials
```

Preview first. These commands never remove settings or archive data and do not revoke the credentials at Google/Discord. Revoke a dedicated test app password separately when retiring it. Avoid credential deletion if you intend to test reinstall with retained configuration.

```powershell
python scripts/setup.py
```

Reinstall should recreate the environment and preserve the existing settings/roster/archive. Retained credentials remain usable on the same machine/account; deleted ones must be set up again. Application source files remain deliberately available for reinstall. After backing up anything needed, remove the local rehearsal folder manually only when you are certain it contains no retained data you want.

## Test record and rehearsal outcome

The full regression suite ran 149 tests with one junction-fixture skip under restricted permissions. The complete 15-test uninstall/installer suite was subsequently rerun with the required permissions under both PowerShell 7 and Windows PowerShell 5.1: all passed, including real junction protection. Installer tests also exercise XML generation, local action paths, temporary-file cleanup, and collision refusal using mocked Task Scheduler registration. These checks do not register, stop, or remove real scheduled tasks.

Record the reviewed candidate commit ID, Windows/Python/PowerShell versions, setup and doctor results, whether the optional test email/task activation was included, and uninstall/reinstall results. Do not include passwords, webhook URLs, actual submissions, or private roster details in a report intended for sharing. Real dependency downloads, new-machine credentials, provider access, and unattended task behaviour remain the purpose of the controlled rehearsal, not evidence already established by the unit tests.

After a successful rehearsal, review the exact final release snapshot and any history to be published again. Only then decide whether and how to publish it. This guide is not confirmation that the existing repository is already safe to make public.
