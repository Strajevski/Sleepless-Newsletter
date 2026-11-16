"""Repeatable Windows setup; never registers tasks or sends newsletter messages."""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

from circus_newsletter.settings import DEFAULTS, PROJECT, data_directory, load_settings
from circus_newsletter.readiness import RUNTIME_FOLDERS

REQUIREMENTS = ("requirements-imap.txt", "requirements-captioner.txt", "requirements-media-optimizer.txt")


class SetupError(RuntimeError):
    """A safe, actionable local setup error with no service response or secret text."""


def _write_settings(path: Path, settings: dict, *, replace: bool = False) -> None:
    # Validate the candidate without exposing a partially configured installation.
    with tempfile.TemporaryDirectory() as temporary:
        candidate = Path(temporary) / "config"
        candidate.mkdir()
        (candidate / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        load_settings(candidate.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not replace:
        with path.open("x", encoding="utf-8") as output:
            output.write(json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configure(root: Path, *, interactive: bool, reconfigure: bool = False) -> None:
    path = root / "config/settings.json"
    if path.exists() and not reconfigure:
        print("Existing settings preserved.")
        load_settings(root)
        return
    legacy = root / "config/mail.json"
    if not path.exists() and legacy.exists():
        old = load_settings(root)
        settings = copy.deepcopy(DEFAULTS)
        settings["email"] = old["email"]
        settings["newsletter"]["sender_address"] = old["newsletter"]["sender_address"]
        print("Migrating the existing mail configuration without changing the account.")
    else:
        source = path if path.exists() else root / "config/settings.example.json"
        settings = json.loads(source.read_text(encoding="utf-8"))
    if interactive:
        print("Configure this installation. Passwords are entered separately and never saved in settings.json.")

        def ask(label, default):
            return input(f"{label} [{default}]: ").strip() or default

        identity = settings["newsletter"]
        identity["name"] = ask("Newsletter name", identity["name"])
        identity["short_name"] = ask("Short name used in greetings", identity["short_name"])
        identity["editor_name"] = ask("Editor name", identity["editor_name"])
        address = ask("Gmail / Google Workspace login address", settings["email"]["login_address"])
        settings["email"]["login_address"] = address
        identity["sender_address"] = address
        if not path.exists():
            identity["message_id_domain"] = address.rsplit("@", 1)[-1]
        storage = settings.setdefault("storage", {"data_directory": "."})
        storage["data_directory"] = ask("Data directory (. keeps the current layout)", storage["data_directory"])
    _write_settings(path, settings, replace=path.exists())
    print("Settings saved." if interactive or legacy.exists() else "Example settings created; replace the fictional identity and email before use.")


def prepare_environment(root: Path, *, install: bool = True) -> Path:
    directory = root / ".venv-host"
    python = directory / "Scripts/python.exe"
    if directory.exists():
        if not python.is_file() or not (directory / "pyvenv.cfg").is_file():
            raise SetupError("Existing .venv-host is incomplete. Inspect or rename it before rerunning setup; it was not deleted.")
        print("Existing Python environment preserved.")
    else:
        print("Creating the local Python environment...")
        venv.EnvBuilder(with_pip=True).create(directory)
        if not python.is_file():
            raise SetupError("Environment creation did not produce its interpreter. Inspect .venv-host and the Python installation.")
    # A copied venv is not a portable interpreter. Verify its own prefix before pip.
    result = subprocess.run([str(python), "-c", "import sys; from pathlib import Path; print(Path(sys.prefix).resolve()); print(sys.version_info >= (3, 11))"],
                            capture_output=True, text=True, timeout=20, check=True)
    lines = result.stdout.strip().splitlines()
    if len(lines) != 2 or Path(lines[0]) != directory.resolve() or lines[1] != "True":
        raise SetupError("The local environment belongs to another installation or uses unsupported Python. Recreate it locally; do not copy virtual environments.")
    if install:
        print("Installing the project's dependencies into the local environment...")
        command = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
        for name in REQUIREMENTS:
            command.extend(["-r", str(root / name)])
        subprocess.run(command, cwd=root, check=True)
    return python


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT, help="Installation folder (not the private data directory)")
    parser.add_argument("--non-interactive", action="store_true", help="Preserve settings or copy the example; never prompt for credentials")
    parser.add_argument("--configure", action="store_true", help="Explicitly edit existing installation identity/settings")
    parser.add_argument("--configure-credentials", action="store_true", help="Explicitly launch hidden app-password setup and IMAP verification")
    parser.add_argument("--skip-dependencies", action="store_true", help="Skip pip installation; readiness will report missing packages")
    args = parser.parse_args(argv)
    if args.non_interactive and (args.configure or args.configure_credentials):
        parser.error("Interactive configuration cannot be combined with --non-interactive")
    if sys.version_info < (3, 11) or sys.platform != "win32":
        print("Setup requires Windows and Python 3.11 or newer.")
        return 1
    root = args.root.resolve()
    try:
        # Verify source completeness before creating an environment or settings.
        for name in (*REQUIREMENTS, "scripts/doctor.py", "scripts/configure_imap.py", "config/settings.example.json"):
            if not (root / name).is_file():
                raise FileNotFoundError("Installation source is incomplete")
        configure(root, interactive=not args.non_interactive, reconfigure=args.configure)
        roster = root / "config/roster.json"
        if not roster.exists():
            with roster.open("x", encoding="utf-8") as output:
                output.write('{"members": {}}\n')
            print("Empty private roster created. Add members using config/roster.example.json as a guide.")
        runtime = data_directory(root)
        for name in RUNTIME_FOLDERS:
            (runtime / name).mkdir(parents=True, exist_ok=True)
        python = prepare_environment(root, install=not args.skip_dependencies)
        credential = root / "config/secrets/imap-password.dpapi"
        configure_password = args.configure_credentials
        if not args.non_interactive and not credential.exists() and not configure_password:
            configure_password = input("Configure the Gmail app password now? [y/N]: ").strip().lower() in {"y", "yes"}
        if configure_password:
            subprocess.run([str(python), str(root / "scripts/configure_imap.py"), "--root", str(root)], check=True)
        print("\nRun 'codex login' under the intended Windows account if it is not already signed in.")
        print("Add/check roster members and review settings, AI configuration, and schedule before enabling automation.")
        print("Running local readiness checks; service authentication needs a separate --online check.")
        result = subprocess.run([str(python), str(root / "scripts/doctor.py"), "--root", str(root), "--offline"], check=False)
        print("No scheduled tasks were registered/enabled, newsletter messages sent, or files published.")
        return result.returncode
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Setup stopped ({type(exc).__name__}). Existing settings and archives were not reset.")
        if isinstance(exc, SetupError):
            print(str(exc))
        print("Check the installation files, folder permissions, and Python environment; see docs/setup.md.")
        return 1
