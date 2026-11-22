"""Installation settings shared by mail, rendering, prompts, and Codex runners."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
from email.utils import formataddr, parseaddr
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    "version": 1,
    "newsletter": {
        "name": "Circus Newsletter", "short_name": "Circus", "editor_name": "Sleepless",
        "sender_name": "", "sender_address": "", "reply_to_address": None,
        "signature": "{editor_name}", "message_id_domain": "sleepless.circus.news",
        "headings": {"feed": "The Lead", "personal_updates": "Personal Updates",
                     "long_and_short": "Long and Short", "long": "Long", "short": "Short",
                     "worth_your_time": "Worth Your Time", "gallery": "Gallery",
                     "editorial": "{editor_name}’s Take", "ledger": "The Ledger"},
    },
    "email": {
        "provider": "gmail", "login_address": "",
        "imap": {"host": "imap.gmail.com", "port": 993, "mailbox": "INBOX", "security": "tls"},
        "smtp": {"host": "smtp.gmail.com", "port": 465, "security": "tls"},
    },
    "codex": {"executable": None, "home_directory": None},
    "storage": {"data_directory": "."},
    "notifications": {"discord_enabled": True},
}


def _merge(defaults: dict, supplied: dict, prefix: str = "settings") -> dict:
    if not isinstance(supplied, dict):
        raise ValueError(f"{prefix} must be an object")
    result = copy.deepcopy(defaults)
    for key, value in supplied.items():
        if key not in defaults:
            raise ValueError(f"Unknown setting: {prefix}.{key}")
        result[key] = (_merge(defaults[key], value, f"{prefix}.{key}")
                       if isinstance(defaults[key], dict) else value)
    return result


def _text(value, key: str, *, empty: bool = False) -> None:
    if (not isinstance(value, str) or (not empty and not value.strip())
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(f"{key} must be {'an optional' if empty else 'a nonempty'} single-line string")


def _address(value: str, key: str, *, empty: bool = False) -> None:
    _text(value, key, empty=empty)
    if value or not empty:
        if parseaddr(value) != ("", value) or not re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", value):
            raise ValueError(f"{key} must be an email address without a display name")


def load_settings(root: Path = PROJECT) -> dict:
    root = Path(root)
    path = root / "config/settings.json"
    if path.exists():
        settings = _merge(DEFAULTS, json.loads(path.read_text(encoding="utf-8")))
        # A real settings file must identify its own account. Never borrow it from
        # another checkout or silently fall back to the former mail.json account.
        _address(settings["email"]["login_address"], "email.login_address")
        _address(settings["newsletter"]["sender_address"], "newsletter.sender_address")
    else:
        settings = copy.deepcopy(DEFAULTS)
        legacy = root / "config/mail.json"
        if legacy.exists():
            mail = json.loads(legacy.read_text(encoding="utf-8"))
            settings["email"]["login_address"] = mail["account"]
            settings["newsletter"]["sender_address"] = mail["account"]
            settings["email"]["imap"].update({key: mail[key] for key in ("host", "port", "mailbox")})
    if type(settings["version"]) is not int or settings["version"] != 1:
        raise ValueError("Unsupported settings version")
    newsletter = settings["newsletter"]
    for key in ("name", "short_name", "editor_name", "signature"):
        _text(newsletter[key], f"newsletter.{key}")
    _text(newsletter["sender_name"], "newsletter.sender_name", empty=True)
    _address(newsletter["sender_address"], "newsletter.sender_address", empty=not path.exists())
    if newsletter["reply_to_address"] is not None:
        _address(newsletter["reply_to_address"], "newsletter.reply_to_address")
    if not isinstance(newsletter["message_id_domain"], str) or not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", newsletter["message_id_domain"]):
        raise ValueError("newsletter.message_id_domain must be a domain name")
    for key, value in newsletter["headings"].items():
        _text(value, f"newsletter.headings.{key}")
    email = settings["email"]
    _address(email["login_address"], "email.login_address", empty=not path.exists())
    if email["provider"] != "gmail":
        raise ValueError("Only the Gmail provider is currently supported")
    for protocol, host, port in (("imap", "imap.gmail.com", 993), ("smtp", "smtp.gmail.com", 465)):
        connection = email[protocol]
        if (connection["host"] != host or type(connection["port"]) is not int
                or connection["port"] != port or connection["security"] != "tls"):
            raise ValueError(f"Gmail {protocol.upper()} requires {host}:{port} with implicit TLS")
    _text(email["imap"]["mailbox"], "email.imap.mailbox")
    if newsletter["sender_address"].casefold() != email["login_address"].casefold():
        raise ValueError("Sender address must match the login address; sending aliases are not yet supported")
    for key, value in settings["codex"].items():
        if value is not None:
            _text(value, f"codex.{key}")
    if type(settings["notifications"]["discord_enabled"]) is not bool:
        raise ValueError("notifications.discord_enabled must be true or false")
    _text(settings["storage"]["data_directory"], "storage.data_directory")
    for key in ("signature",):
        newsletter[key] = newsletter[key].replace("{editor_name}", newsletter["editor_name"])
    newsletter["headings"] = {
        key: value.replace("{editor_name}", newsletter["editor_name"])
        for key, value in newsletter["headings"].items()}
    return settings


def mail_settings(root: Path = PROJECT) -> dict:
    settings = load_settings(root)
    _address(settings["email"]["login_address"], "email.login_address")
    return settings


def sender_header(settings: dict) -> str:
    newsletter = settings["newsletter"]
    if newsletter["sender_name"]:
        return formataddr((newsletter["sender_name"], newsletter["sender_address"]))
    return newsletter["sender_address"]


def apply_reply_to(message, settings: dict) -> None:
    address = settings["newsletter"]["reply_to_address"]
    if address:
        message["Reply-To"] = address


def prompt_text(text: str, settings: dict) -> str:
    """Substitute only explicit branding tokens, leaving JSON and source keys intact."""
    newsletter = settings["newsletter"]
    values = {"newsletter_name": newsletter["name"], "newsletter_short_name": newsletter["short_name"],
              "editor_name": newsletter["editor_name"]}
    for key, heading in newsletter["headings"].items():
        values[f"heading_{key}"] = heading
        values[f"heading_{key}_upper"] = heading.upper()
        values[f"heading_{key}_ascii"] = heading.replace("’", "'")
        values[f"heading_{key}_ascii_upper"] = heading.replace("’", "'").upper()
    values["heading_legacy_feed"] = ("The Feed" if newsletter["headings"]["feed"] == "The Lead"
                                      else newsletter["headings"]["feed"])
    def substitute(match):
        key = match.group(1)
        if key not in values:
            raise ValueError(f"Unknown prompt settings token: {key}")
        return values[key]
    return re.sub(r"\{\{([a-z_]+)\}\}", substitute, text)


def _local_path(root: Path, value: str) -> str:
    path = Path(value).expanduser()
    return str((root / path).resolve() if not path.is_absolute() else path)


def data_directory(root: Path = PROJECT) -> Path:
    """Resolve private runtime storage from an installation root, without moving files."""
    return Path(_local_path(Path(root), load_settings(root)["storage"]["data_directory"])).resolve()


def codex_executable(root: Path, *, prefer_exe: bool = False) -> str | None:
    override = load_settings(root)["codex"]["executable"]
    if override is not None:
        return _local_path(root, override)
    return (shutil.which("codex.exe") or shutil.which("codex")) if prefer_exe else shutil.which("codex")


def codex_environment(root: Path, *, profile_defaults: bool = True) -> dict:
    environment = os.environ.copy()
    environment.pop("CODEX_API_KEY", None)
    environment.pop("OPENAI_API_KEY", None)
    override = load_settings(root)["codex"]["home_directory"]
    if override is not None:
        environment["CODEX_HOME"] = _local_path(root, override)
    if profile_defaults:
        environment.setdefault("HOME", environment.get("USERPROFILE", ""))
        if not environment.get("CODEX_HOME") and environment.get("USERPROFILE"):
            environment["CODEX_HOME"] = str(Path(environment["USERPROFILE"]) / ".codex")
    return environment
