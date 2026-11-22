# Installation settings

Edit `config/settings.json` for newsletter identity, headings, mailbox, Codex CLI overrides and optional notifications. Guided setup creates this private file; use `python scripts/setup.py --configure` to reopen the basic prompts, or edit the JSON directly for other options.

The real file is ignored by Git. The tracked `config/settings.example.json` contains fictional addresses and names. For a new installation, copy the example to `config/settings.json`, edit the identity and account, then run the existing credential configuration and checks. Creating or editing this file does not register scheduled tasks or send any messages.

## Newsletter identity

| Field within `newsletter` | Used for |
| --- | --- |
| `name` | Newsletter title, HTML/text masthead, email subjects, and AI prompt identity. |
| `short_name` | Greetings and shorter operational wording, such as “Hello {name of group}”. |
| `editor_name` | Editorial persona supplied to prompts. It does not select an AI model. |
| `sender_name` | Inbox display name. An empty string preserves the existing bare-address From header. |
| `sender_address` | From address and SMTP envelope sender. Currently must match `email.login_address`; sending aliases are not supported. |
| `reply_to_address` | Optional explicit Reply-To header and submission address in reminders. `null` leaves the header absent, so replies use the sender address as before. |
| `signature` | Reminder and invitation sign-off. `{editor_name}` substitutes the configured editor name. A literal string provides an independent signature. |
| `message_id_domain` | Domain used in outgoing message identifiers. Preserve it in an established installation. It is independent of the SMTP server hostname. |

`newsletter.headings` contains display labels for `feed`, `personal_updates`, `worth_your_time`, `long`, `short`, `gallery`, `editorial` and `ledger`. The default feed label is **The Lead**. The editorial heading defaults to `{editor_name}’s Take`; a literal heading such as `A Final Thought` overrides it. Display headings are uppercased in the email design. Long and Short have their own column headings; `long_and_short` remains available as a collective prompt label but is not displayed as a combined heading. See [Long and Short](long-and-short.md) for entry structure and attribution.

Internal JSON/schema keys remain unchanged, including `feed` and `sleepless_take`. Changing a heading changes its label, not its meaning, structure, or validation. Bundled editor/caption prompts use explicit tokens such as `{{newsletter_name}}`, `{{editor_name}}`, and `{{heading_feed}}`. The runner substitutes settings before calling Codex; source submissions are never processed as prompt templates. The default substitutions reproduce the previous prompt text exactly. Uppercase and straight-apostrophe variants preserve the existing prompt typography; a legacy feed token preserves “The Feed” in older prompt versions and notifications.

## Email

`email.login_address` replaces the former account constant. `email.imap` contains the host, port, mailbox, and security setting; `email.smtp` contains host, port, and security. The supported provider remains `gmail`, using implicit TLS on IMAP 993 and SMTP 465. These fields now have one home, but other providers and STARTTLS have not been added: intake still requires Gmail's unique-message-ID extension. Unsupported combinations are rejected before connecting.

Passwords stay in the existing Windows DPAPI credential store under `config/secrets/`; do not put them in settings.json. Changing an account does not change its stored credential: rerun `scripts/configure_imap.py` and verify it with `scripts/check_smtp.py` before enabling automation.

settings.json replaces mail.json in this checkout. Older installations that do not yet have settings.json can still use their own existing mail.json during migration. If settings.json exists, it is authoritative: invalid or missing account values do not fall back to the legacy account or another checkout.

## Private relay configuration

The tracked config/relays.json is empty. If config/relays.local.json exists, it is used instead and is ignored by Git. This keeps the current installation's real forwarding arrangement local while fresh releases have no configured external relay. Use config/relays.example.json as a fictional structural example. An invalid local override fails rather than silently disabling it. The roster still governs attribution for an explicitly configured relay.

## Application and runtime directories

`storage.data_directory` defines the base folder for private runtime files. Its default value is `"."`, meaning the installation folder containing `config/settings.json`, regardless of the terminal's current folder. Each installation can use its own application folder with the same setting.

```json
"storage": {
  "data_directory": "."
}
```

A relative value such as `"private-data"` resolves inside the installation folder. An absolute value such as `"D:/Newsletter Data"` selects a separate location. JSON backslashes must be doubled; forward slashes in Windows paths avoid that escaping. An omitted storage setting also defaults to `"."` for compatibility.

| Location | Contents |
| --- | --- |
| Application folder | Python code, scripts, templates, prompts, environments, and `config/` settings. |
| Configured data folder | `issues/` (including originals, attachments, drafts and delivery journals), `state/`, `logs/`, `revisions/`, `announcements/`, and default trial `output/`. |
| Application `config/secrets/` | Existing machine-bound credentials. These stay in place and are not made portable by changing the data folder. |

Commands using `--root` still take the **application/installation folder**, where the configuration lives; they resolve its data folder from settings. Manual recovery, delivery, notification state, captioning, image optimisation, archive compaction, and trial/promotion commands use that same distinction. Explicit trial output paths remain overrides. Staged trials reset their copied data setting to `"."` so they cannot accidentally operate on the live external archive. The sample-template preview command continues to write its demonstration files beside the application's templates.

To inspect the resolved directory without creating files or contacting any services:

```powershell
python scripts/data_directory.py
python scripts/data_directory.py --root C:/Newsletter
```

Changing this setting does **not** migrate files. Before changing it on an established installation, stop automation and move or copy the complete runtime folders to the new location, including delivery journals and notification state. Verify the resolved path and archive before restarting; pointing at an empty folder loses visibility of previous sends and can permit duplicate campaigns. Existing scheduled tasks still launch from the application folder.

## Codex and notifications

`codex.executable` and `codex.home_directory` are `null` by default. This preserves the existing executable discovery, environment, and saved CLI login handling. Set an executable path or Codex login/home directory explicitly when needed. Relative paths resolve from the project folder containing `config/settings.json`, not the shell's working directory. The text editor, captioner, and quota check all respect these overrides. Model selection, prompts, reasoning effort, and processing timeouts remain in editor.json and captioner.json. API-key environment variables are still removed from child processes.

`notifications.discord_enabled` remains **true**, preserving the existing behaviour: a configured webhook can send notifications; a missing webhook does nothing. Setting it to false explicitly stops dispatch without deleting the webhook or notification state. Turning it back on may send pending notifications. The webhook itself remains in the existing secure store.

## Scope and safe edits

Settings are read again when operations run; restart an already-running operation to ensure it uses edited values. Keep `version` at `1`. Unknown keys, malformed addresses, control characters in identity fields, unsupported email connections, and invalid option types are rejected. Settings should be ordinary JSON with no comments or trailing commas.

Editing settings does not move runtime data or credentials, register tasks, or regenerate existing drafts. Identity changes affect newly generated artifacts. Changing an identity after a campaign has begun can trigger archived-message integrity checks; make those changes between cycles with automation stopped.

The roster lives in `config/roster.json`, timing in `config/schedule.json`, and delivery policy/holds in `config/delivery.json`. Passwords and webhook URLs belong in the protected credential store, not these JSON files.
