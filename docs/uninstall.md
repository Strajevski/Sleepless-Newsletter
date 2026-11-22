# Uninstalling a local installation

Uninstall uses PowerShell, so it can remove the local Python environment without depending on that environment's interpreter. The default is a preview. Run it on the machine that owns the installation, as the Windows account authorised to manage its scheduled tasks.

```powershell
.\scripts\uninstall.ps1
```

The preview shows the installation folder, owned tasks, local environment, any explicitly selected credentials, and tasks it will leave alone. It makes no changes. Use `-Root 'C:/path/to/installation'` to select another local installation explicitly. The default root is the folder containing the script's parent scripts directory, not the terminal's working folder.

## Apply the plan

```powershell
.\scripts\uninstall.ps1 -Apply
```

The command asks you to type `UNINSTALL` before making changes. For a previously reviewed noninteractive run, `-Apply -Yes` skips that prompt. `-Yes` alone is rejected.

Uninstall disables, stops, and removes recognised scheduled tasks belonging to this exact installation, then removes its .venv-host environment. It rechecks task ownership during removal and refuses to remove the environment while any scheduled task still references it. Query errors, changed ownership, stop failures, incomplete environments, and links/junctions cause a stop for inspection rather than broader deletion. Close manually launched newsletter/Python processes first; a locked environment may need manual attention if removal fails.

Settings, the roster, archives, attachments, delivery journals, logs, state, revision backups, trial output, and application source files are preserved. Credentials are preserved by default. Shared Python, Codex CLI, and Codex login information are not removed. No application directory, data directory or external data is deleted; uninstall reads only the local path setting to protect data accidentally placed inside the environment; it never inspects or deletes the configured external data folder. After backing up private files, you can remove the retained application folder separately if appropriate.

## Optional credential removal

```powershell
.\scripts\uninstall.ps1 -RemoveCredentials
.\scripts\uninstall.ps1 -Apply -RemoveCredentials
```

The first command previews credential deletion. The second includes the installation's imap-password.dpapi and discord-webhook.dpapi files in the confirmed removal plan. No other secrets or files in config/secrets are deleted. This removes local stored credentials; it does not revoke the Google app password or Discord webhook at their providers. Revoke those separately if retiring them. Reinstalling with retained configuration and data can use the setup process again; missing credentials must be configured anew before operation.

## Ownership and local-path protection

Task names alone are insufficient. Ownership requires a recognised root-folder task name, exactly one Python action, the selected installation as its working directory, the expected script inside its scripts folder, and the expected arguments. The recognised names are Circus Newsletter Intake, Circus Newsletter Final Build, and Circus Newsletter Delivery. Both current stage checks and recognised legacy intake actions are supported. Older/custom tasks missing these details require manual inspection. Task actions must use the same installation path; legacy mapped-path aliases may need manual reconciliation before a local-path uninstall.

Tasks belonging to another installation or another Task Scheduler folder are preserved. An unrecognised task that uses this installation's environment blocks removal until reviewed. The task installers now use the same ownership checks to refuse replacing another installation's same-named task. Task names remain shared, so a second installation on one Windows host cannot silently take over the first installation's task names; task namespacing would be a separate enhancement.

UNC paths and mapped network drives are refused, and existing path ancestors and environment contents are checked for links/junctions before recursive deletion. If the installation is hosted on another computer, run uninstall on that computer using its actual local disk path. Check the preview carefully before applying it to an established installation.

Uninstall can be rerun after successful removal: absent owned tasks, environments, and credentials need no further action. A failure may leave tasks disabled or partially removed; private data is retained, and the command stops before deleting the environment/credentials when task handling fails. Inspect any reported failure before relying on automation or restarting setup.

## Data protection and repeat removal

Private runtime data configured or detected inside the environment blocks its removal. Invalid local path metadata must be corrected before uninstall.

After successful removal, `Test-Path .venv-host` should return `False`; retained configuration and archive files should still exist. Preview uninstall again if you want to confirm there is nothing further to remove. For complete removal, back up what you need and manually delete the remaining application folder. Check `storage.data_directory` first: external runtime data is deliberately preserved and must be handled separately. Do not delete shared Python, Codex or its login as part of removing this application.
