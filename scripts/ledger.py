#!/usr/bin/env python3
"""Trade ledger operations.

All entries are append-only JSONL. Each line is a discrete event:
  - INTENT: proposed trade before fill
  - OPEN:   filled entry
  - SCALE:  partial add / trim
  - CLOSE:  exit event (full or last partial)
  - NOTE:   free-form annotation

Usage:
  ledger.py list [--last N]
  ledger.py add --kind INTENT --ticker X --side LONG --qty 6 --entry 50 --stop 47 --target 56 \
      --thesis "..." --strategy breakout --horizon 5
  ledger.py add --kind OPEN   --ref <intent_id> --fill 50.10 --qty 6
  ledger.py add --kind CLOSE  --ref <open_id>   --exit 55.50 --qty 6 --reason target
  ledger.py add --kind NOTE   --text "..."
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Optional

from _common import append_ledger, now_iso, read_ledger


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def cmd_list(args: argparse.Namespace) -> int:
    entries = read_ledger(args.book)
    if args.last:
        entries = entries[-args.last:]
    for e in entries:
        print(json.dumps(e))
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    entry: dict = {
        "id": _new_id(),
        "ts": now_iso(),
        "kind": args.kind,
        "book": args.book,
    }
    # Map all optional args if present
    for k in (
        "ticker", "kind_of_asset", "side", "qty", "entry", "stop", "target",
        "thesis", "strategy", "horizon", "ref", "fill", "exit", "reason",
        "premium", "text", "expiration", "strike", "right"
    ):
        v = getattr(args, k, None)
        if v is not None:
            entry[k] = v
    append_ledger(entry, book=args.book)
    print(json.dumps(entry, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("list")
    lp.add_argument("--last", type=int, default=None)
    lp.add_argument("--book", default="real", choices=["real", "shadow"])

    ap_add = sub.add_parser("add")
    ap_add.add_argument("--book", default="real", choices=["real", "shadow"])
    ap_add.add_argument("--kind", required=True,
                        choices=["INTENT", "OPEN", "SCALE", "CLOSE", "NOTE"])
    ap_add.add_argument("--ticker")
    ap_add.add_argument("--kind-of-asset", dest="kind_of_asset",
                        choices=["stock", "option", "crypto", "etf"])
    ap_add.add_argument("--side", choices=["LONG", "SHORT"])
    ap_add.add_argument("--qty", type=float)
    ap_add.add_argument("--entry", type=float)
    ap_add.add_argument("--stop", type=float)
    ap_add.add_argument("--target", type=float)
    ap_add.add_argument("--thesis")
    ap_add.add_argument("--strategy")
    ap_add.add_argument("--horizon", type=int)
    ap_add.add_argument("--ref")
    ap_add.add_argument("--fill", type=float)
    ap_add.add_argument("--exit", dest="exit", type=float)
    ap_add.add_argument("--reason")
    ap_add.add_argument("--premium", type=float)
    ap_add.add_argument("--text")
    ap_add.add_argument("--expiration")
    ap_add.add_argument("--strike", type=float)
    ap_add.add_argument("--right", choices=["C", "P"])

    args = ap.parse_args()
    if args.cmd == "list":
        return cmd_list(args)
    return cmd_add(args)


if __name__ == "__main__":
    sys.exit(main())
