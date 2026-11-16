"""Non-sending installation checks. Online authentication requires explicit opt-in."""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from circus_newsletter.settings import (codex_environment, codex_executable, data_directory,
                                        load_settings, prompt_text)

RUNTIME_FOLDERS = ("issues", "state", "logs", "revisions", "announcements", "output")


def _example_address(address: str) -> bool:
    domain = address.rsplit("@", 1)[-1].lower()
    return any(domain == suffix or domain.endswith("." + suffix)
               for suffix in ("example.com", "example.org", "example.net", "invalid", "test", "localhost"))


@dataclass
class Check:
    name: str
    status: str
    message: str
    action: str = ""


def check_secret_acl(path: Path) -> bool:
    """Machine DPAPI requires that only the setup account and SYSTEM can read it."""
    environment = os.environ.copy()
    environment["CIRCUS_CHECK_SECRET_PATH"] = str(path)
    command = """$ErrorActionPreference = 'Stop'
$allowed = @([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18')
$safe = $true
foreach ($p in @($env:CIRCUS_CHECK_SECRET_PATH, (Split-Path -Parent $env:CIRCUS_CHECK_SECRET_PATH))) {
  foreach ($rule in (Get-Acl -LiteralPath $p).Access) {
    if ($rule.AccessControlType -eq 'Allow') {
      $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
      if ($sid -notin $allowed) { $safe = $false }
    }
  }
}
$safe | ConvertTo-Json -Compress"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            env=environment, capture_output=True, text=True, timeout=15, check=True)
    return json.loads(result.stdout) is True


def _writable(path: Path) -> None:
    """Probe the closest existing folder; never create runtime folders or state."""
    parent = path
    while not parent.exists():
        if parent.parent == parent:
            raise OSError("No existing parent")
        parent = parent.parent
    if not parent.is_dir():
        raise NotADirectoryError()
    with tempfile.TemporaryFile(dir=parent):
        pass


def _dependencies(root: Path) -> None:
    # Read the same constraints pip uses, rather than a second set of versions.
    for filename in ("requirements-imap.txt", "requirements-captioner.txt", "requirements-media-optimizer.txt"):
        for line in (root / filename).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            match = re.fullmatch(r"([A-Za-z0-9_-]+)((?:(?:>=|<)[0-9.]+,?)+)", line)
            if not match:
                raise ValueError("Unsupported dependency constraint")
            installed = tuple(int(part) for part in importlib.metadata.version(match[1]).split("."))
            for operator, version in re.findall(r"(>=|<)([0-9.]+)", match[2]):
                required = tuple(map(int, version.split(".")))
                length = max(len(installed), len(required))
                actual = installed + (0,) * (length - len(installed))
                minimum = required + (0,) * (length - len(required))
                if (operator == ">=" and actual < minimum) or (operator == "<" and actual >= minimum):
                    raise ValueError("Dependency version outside supported range")
    from zoneinfo import ZoneInfo
    ZoneInfo("Europe/London")
    from PIL import Image
    Image.new("RGB", (1, 1)).close()


def inspect(root: Path, *, online: bool = False) -> list[Check]:
    root = Path(root).resolve()
    checks = []

    def run(name, operation, success, action):
        try:
            operation()
        except Exception:
            # Never echo service errors: they may contain account details or secrets.
            checks.append(Check(name, "fail", "Check failed.", action))
            return False
        checks.append(Check(name, "pass", success))
        return True

    checks.append(Check("Python", "pass" if sys.version_info >= (3, 11) else "fail",
                        "Python 3.11 or newer is required.",
                        "Run setup with Python 3.11 or newer." if sys.version_info < (3, 11) else ""))
    windows = sys.platform == "win32"
    checks.append(Check("Operating system", "pass" if windows else "fail",
                        "Windows is the supported platform for secure credentials and automation."))
    run("Dependencies", lambda: _dependencies(root), "Dependencies and timezone data are available.",
        "Run scripts/setup.py, then run this check with .venv-host/Scripts/python.exe.")
    settings = None

    def configuration():
        nonlocal settings
        if not (root / "config/settings.json").is_file():
            raise ValueError("Settings missing")
        settings = load_settings(root)
        for address in (settings["email"]["login_address"], settings["newsletter"]["sender_address"]):
            if _example_address(address):
                raise ValueError("Example account")

    configured = run("Settings", configuration, "Installation settings are valid.",
                     "Run scripts/setup.py --configure or edit config/settings.json; replace example addresses.")

    def roster_schedule():
        from circus_newsletter.core import load_config
        from circus_newsletter.delivery import _settings, recipients
        load_config(root)
        _settings(root)
        if any(_example_address(address) for address in recipients(root).values()):
            raise ValueError("Example recipients")

    run("Roster and schedule", roster_schedule, "Recipient roster, delivery policy, and schedule are valid.",
        "Add real members to config/roster.json and check schedule.json and delivery.json. See docs/setup.md.")

    if configured:
        def folders():
            runtime = data_directory(root)
            for path in (root / "config", runtime, *(runtime / name for name in RUNTIME_FOLDERS)):
                _writable(path)

        run("Folders", folders, "Configuration and runtime folders are writable (temporary probes removed).",
            "Check storage.data_directory and folder permissions. Do not move an existing archive without its journals.")

        def assets():
            for filename in ("editor.json", "captioner.json"):
                config = json.loads((root / "config" / filename).read_text(encoding="utf-8"))
                if filename == "captioner.json" and type(config.get("enabled")) is not bool:
                    raise ValueError("Invalid captioner switch")
                if filename == "captioner.json" and config.get("enabled") is False:
                    continue
                for key in ("model", "reasoning_effort", "prompt"):
                    if not isinstance(config.get(key), str) or not config[key].strip():
                        raise ValueError("Missing model configuration")
                for key in ("timeout_seconds", "max_input_characters"):
                    if type(config.get(key)) is not int or config[key] <= 0:
                        raise ValueError("Invalid model processing limit")
                prompt_text((root / config["prompt"]).read_text(encoding="utf-8"), settings)
                schema = config.get("output_schema", "config/editor-output.schema.json")
                json.loads((root / schema).read_text(encoding="utf-8"))
            from circus_newsletter.render import render_html, render_text
            from circus_newsletter.clock import load_schedule
            # Use fictional bundled content in memory; no issue or outgoing draft is saved.
            issue = json.loads((root / "templates/preview-data.json").read_text(encoding="utf-8"))
            schedule = load_schedule(root)
            issue["issue_week"] = schedule["first_issue_week"]
            render_html(issue, schedule, root / "templates/issue-email.html", preview=True, settings=settings)
            render_text(issue, schedule, settings=settings)

        run("Models, prompts and templates", assets, "Configured prompts, schemas, and template rendering are usable.",
            "Check editor.json, captioner.json, prompt paths, schemas, and templates. No model request was made.")

        def login():
            executable = codex_executable(root)
            if not executable:
                raise FileNotFoundError()
            result = subprocess.run([executable, "login", "status"], env=codex_environment(root),
                                    capture_output=True, text=True, timeout=20, check=False)
            if result.returncode:
                raise RuntimeError("Login unavailable")

        cli_ready = run("Codex login", login, "Codex CLI reports a saved login; no model turn was started.",
                        "Install Codex CLI if missing and run 'codex login' as the account that will run the newsletter. Check codex overrides.")
        password = None

        def credential():
            nonlocal password
            if not windows:
                raise RuntimeError("Windows required")
            from circus_newsletter.secret_store import read_password, SECRET_NAME
            secret = root / "config/secrets" / SECRET_NAME
            if not secret.is_file() or not check_secret_acl(secret):
                raise PermissionError()
            password = read_password(root)
            if not password:
                raise ValueError("Empty credential")

        secret_ready = run("Email credential", credential, "Encrypted email credential is readable with restricted access.",
                           "Run scripts/configure_imap.py under the intended Windows account; it tests and securely stores an app password.")
        webhook = root / "config/secrets/discord-webhook.dpapi"
        if settings["notifications"]["discord_enabled"] and webhook.exists():
            def discord():
                from circus_newsletter.secret_store import read_secret, DISCORD_SECRET_NAME
                from circus_newsletter.discord import validate_webhook_url
                if not check_secret_acl(webhook):
                    raise PermissionError()
                validate_webhook_url(read_secret(root, DISCORD_SECRET_NAME))
            run("Discord configuration", discord, "Webhook is stored securely; no notification was sent.",
                "Check the existing Discord credential and its permissions. Readiness checks never test-post a message.")
        else:
            checks.append(Check("Discord", "skip", "Disabled or no webhook configured; existing optional behaviour is preserved."))

        if online and secret_ready:
            def imap():
                from circus_newsletter.imap import connect
                connection = connect(root, password)
                connection.logout()
            run("IMAP authentication", imap, "IMAP login and read-only mailbox selection succeeded; no messages fetched.",
                "Check the Gmail account, app password, mailbox, and internet connection.")

            def smtp():
                email = settings["email"]
                with smtplib.SMTP_SSL(email["smtp"]["host"], email["smtp"]["port"],
                                      context=ssl.create_default_context(), timeout=30) as connection:
                    connection.login(email["login_address"], password)
            run("SMTP authentication", smtp, "SMTP authentication succeeded; no email was submitted.",
                "Check Gmail SMTP access, the app password, and internet connectivity.")
        else:
            checks.append(Check("Email authentication", "skip", "Not tested (offline mode or email credential unavailable)."))
        if online and cli_ready:
            def quota():
                from circus_newsletter.editor_gate import read_codex_budget
                budget = read_codex_budget(root=root)
                if not budget.allowed:
                    checks.append(Check("AI usage policy", "warn", "Current usage blocks AI editing; the normal retry/fallback policy applies."))
            run("Codex service", quota, "Read-only usage query succeeded; no model turn or credit purchase was made.",
                "Check the saved Codex login, connectivity, and usage/credit policy. No AI generation was attempted.")
    return checks


def report(checks: list[Check], *, online: bool) -> dict:
    return {"mode": "online" if online else "offline", "passed": not any(c.status == "fail" for c in checks),
            "checks": [asdict(check) for check in checks]}
