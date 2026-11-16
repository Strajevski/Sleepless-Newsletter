"""One-time local setup for a private Discord incoming webhook."""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.settings import data_directory
from circus_newsletter.discord import INVALID_FILE, baseline_existing, post_webhook, validate_webhook_url
from circus_newsletter.secret_store import DISCORD_SECRET_NAME, read_secret, store_secret


parser = argparse.ArgumentParser(description="Verify and store a Discord webhook for Circus status alerts")
parser.add_argument("--check", action="store_true", help="Send a test alert using the stored webhook")
parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
args = parser.parse_args()
root = args.root.resolve()

try:
    if args.check:
        url = read_secret(root, DISCORD_SECRET_NAME)
    else:
        url = validate_webhook_url(getpass.getpass("Discord webhook URL (input hidden): "))
    message_id = post_webhook(url, "Circus Newsletter notifications connected. This is a setup test.")
    if not args.check:
        old_events = baseline_existing(root)
        store_secret(root, DISCORD_SECRET_NAME, url)
        (data_directory(root) / INVALID_FILE).unlink(missing_ok=True)
        print(f"Webhook verified; {old_events} existing event(s) left out of new alerts")
    else:
        print("Stored webhook verified")
    print(f"Discord confirmed setup message {message_id}")
except Exception as exc:
    parser.exit(1, f"Discord setup failed: {exc}\n")
