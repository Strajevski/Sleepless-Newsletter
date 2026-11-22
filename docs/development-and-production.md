Development and production are checkouts of the same clean Git history. GitHub (when configured) and the local bare remote store that history; a portable release is an archive of a selected commit, not an independently maintained source tree.

Make and test changes in the development checkout, commit them, and push them to the canonical remote. Production should be updated only to a tested commit or release tag. Publishing development commits does not automatically deploy them.

Before updating production, inspect `git status`, back up the affected files and private configuration, and pause the existing newsletter tasks while no task or manual operation is running. Fetch the intended release, inspect its changes, update the checkout, run the automated tests and offline doctor, then restore the previous task enabled states. Do not run setup or task installers as part of an ordinary source update unless that release explicitly requires an installation migration.

Keep mailbox identity, credentials, roster, relay mapping and runtime data private. `config/settings.json`, legacy `config/mail.json`, `config/roster.json`, `config/relays.local.json`, `config/secrets/`, issues, submissions, journals, logs and local environments are ignored. Inspect staged files before every public commit. Schedule and model configuration files are tracked; review local configuration changes before updating or committing them.

The initial consolidation retains production's three existing task registrations and their timings. The portable installers' two-task layout is available for new installations, but replacing an existing task layout is a separate operational change. Do not register duplicate task layouts.

The retired repositories and migration backups contain private history and configuration. Keep them private and never add them as public remotes or merge their history into the clean repository. They are recovery copies, not active development branches.
