#!/usr/bin/env python3
"""Send a message to active recipients in broadcast_recipients.json via iMessage.

Usage:
    broadcast.py "<message>"
    broadcast.py --stdin                          # read message from stdin
    broadcast.py --dry-run "..."                  # print plan, do not send
    broadcast.py --list                           # list active recipients and exit
    broadcast.py --to <phone> "..."               # send only to one phone
    broadcast.py --to <phone1> --to <phone2> "..." # repeat for multiple

The default behavior (no --to) sends to every recipient with active=true.
With --to, the active set is filtered to entries whose `phone` matches any
of the provided values. Exit code 3 if --to filters to an empty set.

Identifying the user's own contact: in broadcast_recipients.json the entry
marked portfolio_id="primary"). The
hourly_broadcast.py orchestrator uses --to to deliver portfolio-specific
messages to "Me" while sending generic messages to other recipients.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECIPIENTS_PATH = ROOT / "broadcast_recipients.json"
SEND_SCRIPT = ROOT / "scripts" / "send_message.spct"


def load_active(filter_phones: list[str] | None = None) -> list[dict]:
    """Return active recipients, optionally filtered to a phone allow-list."""
    data = json.loads(RECIPIENTS_PATH.read_text())
    active = [r for r in data["recipients"] if r.get("active", True)]
    if filter_phones:
        allow = set(filter_phones)
        active = [r for r in active if r.get("phone") in allow]
    return active


def cmd_list(args: argparse.Namespace) -> int:
    active = load_active(args.to)
    for r in active:
        line = f"{r.get('name', '?')}\t{r['phone']}"
        if r.get("portfolio_id"):
            line += f"\tportfolio_id={r['portfolio_id']}"
        print(line)
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    if args.stdin:
        message = sys.stdin.read().strip()
    else:
        message = args.message or ""
    if not message:
        print("error: empty message", file=sys.stderr)
        return 1

    active = load_active(args.to)
    if not active:
        if args.to:
            print(f"error: no active recipient matched --to filter: {args.to}",
                  file=sys.stderr)
            return 3
        print("error: no active recipients", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"[DRY RUN] would send to {len(active)} recipient(s):")
        for r in active:
            print(f"  - {r.get('name', '?')} ({r['phone']})")
        print(f"[DRY RUN] message ({len(message)} chars):")
        print(message)
        return 0

    sent, failed = [], []
    for r in active:
        phone = r["phone"]
        label = f"{r.get('name', '?')} ({phone})"
        try:
            subprocess.run(
                ["osascript", str(SEND_SCRIPT), phone, message],
                check=True,
                capture_output=True,
                text=True,
            )
            sent.append(label)
        except subprocess.CalledProcessError as e:
            failed.append(f"{label}: {e.stderr.strip() or e.stdout.strip() or 'unknown error'}")

    print(f"sent: {len(sent)}/{len(active)}")
    for s in sent:
        print(f"  ok  - {s}")
    for f in failed:
        print(f"  err - {f}")
    if failed:
        return 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Send iMessage to active recipients in broadcast_recipients.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("message", nargs="?", default=None,
                    help="Message text. Omit if using --stdin.")
    ap.add_argument("--stdin", action="store_true",
                    help="Read message from stdin instead of argv.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print plan without sending.")
    ap.add_argument("--list", action="store_true",
                    help="List active recipients (honors --to) and exit.")
    ap.add_argument("--to", action="append", default=None, metavar="PHONE",
                    help="Limit to this phone (repeatable). E.164 format, e.g. +12125551234.")
    args = ap.parse_args()

    if args.list:
        return cmd_list(args)
    return cmd_send(args)


if __name__ == "__main__":
    sys.exit(main())
