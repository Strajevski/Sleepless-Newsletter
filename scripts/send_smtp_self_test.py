"""Send one [TEST] email from Circus to its own inbox; never contact roster members."""

from __future__ import annotations

import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.settings import mail_settings, sender_header, apply_reply_to
from circus_newsletter.secret_store import read_password


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    settings = mail_settings(root)
    identity, email = settings["newsletter"], settings["email"]
    account = identity["sender_address"]
    mail = EmailMessage()
    mail["From"] = sender_header(settings)
    apply_reply_to(mail, settings)
    mail["To"] = account
    mail["Subject"] = f"[TEST] {identity['short_name']} SMTP transport"
    mail["Date"] = format_datetime(datetime.now(timezone.utc))
    mail["Message-ID"] = make_msgid(domain=identity["message_id_domain"])
    mail.set_content(f"This is a transport test for {identity['name']}. It is not a submission.")
    try:
        with smtplib.SMTP_SSL(email["smtp"]["host"], email["smtp"]["port"], context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(email["login_address"], read_password(root))
            refused = smtp.sendmail(account, [account], mail.as_bytes())
        if refused:
            raise RuntimeError("Gmail SMTP did not accept the self-test recipient")
    except Exception as exc:
        print(f"SMTP self-test failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("Gmail SMTP accepted one [TEST] message addressed only to the Circus account")
    return 0


if __name__ == "__main__":
    sys.exit(main())
