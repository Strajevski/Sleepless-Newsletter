"""One-time local setup for the Circus Google app password."""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.imap import load_mail_config, connect, stored_password
from circus_newsletter.secret_store import store_password


parser = argparse.ArgumentParser(description="Store and verify the Circus IMAP app password")
parser.add_argument("--check", action="store_true", help="Verify the stored password without replacing it")
parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
args = parser.parse_args()

try:
    if args.check:
        password = stored_password(args.root.resolve())
    else:
        password = "".join(getpass.getpass("Google app password (input hidden): ").split())
        if not password:
            parser.exit(2, "No app password entered.\n")
    connection = connect(args.root.resolve(), password)
    connection.logout()
    if not args.check:
        store_password(args.root.resolve(), password)
except Exception as exc:
    parser.exit(1, f"IMAP setup failed: {exc}\n")

print(f"IMAP access verified for {load_mail_config(args.root.resolve())['account']}; encrypted credential is in config/secrets")
