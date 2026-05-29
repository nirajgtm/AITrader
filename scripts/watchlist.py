#!/usr/bin/env python3
"""Human-readable view of the watchlist (the names /trader always analyzes).

The watchlist lives in state/watchlist.json (the single source of truth, read by
every script via watchlist_store). This command prints it for a person to glance
at -- it replaces the old hand-maintained knowledge/watchlist.md table.

Usage:
  watchlist.py show           # static view from JSON (fast): thesis, trigger, flags
  watchlist.py show --live    # also pull current price + trigger status per name
  watchlist.py list           # just the active tickers, comma-separated
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import watchlist_store  # noqa: E402

PY = SCRIPTS / ".venv" / "bin" / "python3"
PRICE = SCRIPTS / "price.py"
WCHECK = SCRIPTS / "watchlist_check.py"


def _live_metrics(ticker: str) -> dict | None:
    try:
        out = subprocess.run([str(PY), str(PRICE), ticker, "--json"],
                             capture_output=True, text=True, timeout=25)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout).get("data") or None
    except Exception:
        return None


def _wcheck_status() -> dict:
    try:
        out = subprocess.run([str(PY), str(WCHECK), "--json"],
                             capture_output=True, text=True, timeout=90)
        d = json.loads(out.stdout)
        items = (d.get("data") or {}).get("items") or d.get("items") or []
        return {it.get("ticker"): it.get("status", "") for it in items if it.get("ticker")}
    except Exception:
        return {}


def cmd_show(args: argparse.Namespace) -> int:
    entries = watchlist_store.active_entries()
    if not entries:
        print("(watchlist is empty)")
        return 0
    statuses = _wcheck_status() if args.live else {}
    print(f"WATCHLIST — {len(entries)} active names (source: state/watchlist.json)\n")
    for e in entries:
        tk = e.get("ticker", "?")
        lv = e.get("levels", {}) or {}
        trig = lv.get("entry_trigger")
        cond = lv.get("entry_trigger_condition") or ""
        flag = "  [NEEDS VALIDATION]" if e.get("needs_validation") else ""
        line = f"{tk:6} {e.get('direction',''):6} trig={trig} {cond}".rstrip()
        if args.live:
            m = _live_metrics(tk) or {}
            close = m.get("close")
            rsi = m.get("rsi14")
            st = statuses.get(tk, "")
            line += f"   last={close} rsi={rsi} status={st}"
        print(line + flag)
        print(f"       {e.get('angle','')}")
        thesis = (e.get("thesis") or "").strip()
        if thesis:
            print(f"       thesis: {thesis[:160]}{'...' if len(thesis) > 160 else ''}")
        print()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    print(", ".join(watchlist_store.active_tickers()))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="View the watchlist (state/watchlist.json).")
    sub = p.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("show", help="Print the watchlist for a human to read.")
    ps.add_argument("--live", action="store_true",
                    help="Also fetch current price + trigger status per name (slower).")
    ps.set_defaults(func=cmd_show)
    pl = sub.add_parser("list", help="Print active tickers, comma-separated.")
    pl.set_defaults(func=cmd_list)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
