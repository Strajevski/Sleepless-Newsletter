"""Check installation readiness without sending mail, notifications, or model requests."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.readiness import inspect, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--online", action="store_true", help="Also authenticate to IMAP/SMTP and query Codex usage read-only")
    mode.add_argument("--offline", action="store_true", help="Local checks only (the default)")
    parser.add_argument("--json", action="store_true", help="Machine-readable results; service error text and secrets are never included")
    args = parser.parse_args()
    result = report(inspect(args.root, online=args.online), online=args.online)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for item in result["checks"]:
            print(f"[{item['status'].upper()}] {item['name']}: {item['message']}")
            if item["action"]:
                print(f"  Next: {item['action']}")
        print("Checks passed." if result["passed"] else "Some checks need attention; follow the Next steps above.")
        if not args.online:
            print("Offline checks do not verify service authentication. Use --online when ready.")
        print("No messages sent, AI generated, scheduled tasks changed, or content published.")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
