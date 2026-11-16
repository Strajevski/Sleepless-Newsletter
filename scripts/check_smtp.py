"""Verify the stored Circus app password can authenticate to Gmail SMTP; send nothing."""

from __future__ import annotations

import argparse
import smtplib
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.settings import mail_settings
from circus_newsletter.secret_store import read_password


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    root = parser.parse_args().root.resolve()
    try:
        email = mail_settings(root)["email"]
        with smtplib.SMTP_SSL(email["smtp"]["host"], email["smtp"]["port"], context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(email["login_address"], read_password(root))
    except Exception as exc:
        print(f"SMTP authentication failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"SMTP authentication verified for {email['login_address']}; no message was sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
