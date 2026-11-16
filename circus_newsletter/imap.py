"""Read Gmail messages without changing mailbox state."""

from __future__ import annotations

import imaplib
import re
import ssl
from datetime import datetime
from pathlib import Path
from typing import Iterator


from circus_newsletter.settings import mail_settings

_GMAIL_ID = re.compile(rb"\bX-GM-MSGID (\d+)\b")
_INTERNAL_DATE = re.compile(rb'\bINTERNALDATE "([^"]+)"')
_UID = re.compile(rb"\bUID (\d+)\b")


def load_mail_config(root: Path) -> dict:
    settings = mail_settings(root)
    return {"account": settings["email"]["login_address"],
            **{key: settings["email"]["imap"][key] for key in ("host", "port", "mailbox")}}



def stored_password(root: Path) -> str:
    from circus_newsletter.secret_store import read_password

    return read_password(root)


def connect(root: Path, password: str | None = None):
    """Authenticate as the dedicated account and select INBOX read-only."""
    config = load_mail_config(root)
    secret = password if password is not None else stored_password(root)
    connection = imaplib.IMAP4_SSL(config["host"], config["port"],
                                   ssl_context=ssl.create_default_context(), timeout=30)
    try:
        connection.login(config["account"], secret)
        status, _ = connection.select(config["mailbox"], readonly=True)
        if status != "OK":
            raise RuntimeError("Could not open Gmail INBOX read-only")
        capabilities = {item.decode("ascii") if isinstance(item, bytes) else item
                        for item in connection.capabilities}
        if "X-GM-EXT-1" not in capabilities:
            raise RuntimeError("Gmail unique-message-ID extension is unavailable")
        return connection
    except Exception:
        try:
            connection.logout()
        except Exception:
            pass
        raise


def _fetch(connection, uid: bytes, parts: str) -> list:
    status, data = connection.uid("FETCH", uid, parts)
    if status != "OK" or not data:
        raise RuntimeError(f"IMAP fetch failed for UID {uid.decode('ascii')}")
    return data


def _metadata(connection, uid: bytes) -> tuple[str, datetime]:
    data = _fetch(connection, uid, "(UID X-GM-MSGID INTERNALDATE)")
    headers = [item[0] if isinstance(item, tuple) else item for item in data]
    header = next((item for item in headers if isinstance(item, bytes) and _UID.search(item)), None)
    if not header or _UID.search(header).group(1) != uid:
        raise RuntimeError(f"Missing matching IMAP UID {uid.decode('ascii')}")
    gmail_id = _GMAIL_ID.search(header)
    internal_date = _INTERNAL_DATE.search(header)
    if not gmail_id or not internal_date:
        raise RuntimeError(f"Missing Gmail ID or received date for UID {uid.decode('ascii')}")
    source_id = f"gmail:{int(gmail_id.group(1)):x}"
    received_at = datetime.strptime(internal_date.group(1).decode("ascii"), "%d-%b-%Y %H:%M:%S %z")
    return source_id, received_at


def _raw_message(connection, uid: bytes) -> bytes:
    data = _fetch(connection, uid, "(UID BODY.PEEK[])")
    for item in data:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
            if _UID.search(item[0]) and _UID.search(item[0]).group(1) == uid:
                return item[1]
    raise RuntimeError(f"Missing raw message for UID {uid.decode('ascii')}")


def messages(connection, known_source_ids: set[str]) -> Iterator[tuple[str, bytes, datetime]]:
    """Scan all INBOX IDs, downloading only messages not already archived."""
    status, data = connection.uid("SEARCH", None, "ALL")
    if status != "OK" or not data:
        raise RuntimeError("Could not list Gmail INBOX")
    for uid in data[0].split():
        source_id, received_at = _metadata(connection, uid)
        if source_id not in known_source_ids:
            yield source_id, _raw_message(connection, uid), received_at
