#!/usr/bin/env python3
"""The signal inbox -- an ADVISORY push rail for the mind's signal subagents.

A "signal" subagent (e.g. a congress tracker) surfaces a ticker it thinks is worth
the mind's attention by pushing it here (state/mind/agent_signals.json). The inbox is
ADVISORY ONLY: it NEVER auto-adds to the watchlist or the conviction board. The mind
drains it each run, deep-dives every surfaced name, and OWNS every add/remove -- it may
park, convict, buy, or pass freely, and may ignore or remove anything.

One entry per (agent, ticker): a repeat surface of the same ticker by the same agent
UPSERTS (refreshes why/source/ts, resets status to "new") instead of stacking duplicates.

Entry shape: {ticker, why, source, agent, ts, status}. status is "new" until the mind
gives it a disposition, then "processed" (with the disposition string stored).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_DIR = Path(__file__).resolve().parent
PATH = _DIR / "state" / "mind" / "agent_signals.json"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load() -> list:
    if PATH.exists():
        try:
            return json.loads(PATH.read_text()).get("signals", [])
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save(signals: list) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps({"updated": _now(), "signals": signals}, indent=2))


def push(ticker: str, why: str = "", source: str = "", agent: str = "") -> dict:
    """Upsert a {status:"new"} signal, keyed by (agent, ticker). A repeat surface of the
    same ticker by the same agent refreshes why/source/ts and resets status to "new"."""
    tk = ticker.upper()
    e = {"ticker": tk, "why": why, "source": source, "agent": agent,
         "ts": _now(), "status": "new"}
    signals = [s for s in load()
               if not (s.get("ticker") == tk and s.get("agent") == agent)]
    signals.append(e)
    _save(signals)
    return e


def pending() -> list:
    """The signals still awaiting the mind's disposition (status == "new")."""
    return [s for s in load() if s.get("status") == "new"]


def mark_processed(ticker: str, agent: str = "", disposition: str = "") -> bool:
    """Stamp a (agent, ticker) signal processed and store the mind's disposition string
    (e.g. "parked", "convicted", "bought", "passed: <reason>"). Returns whether it matched."""
    tk = ticker.upper()
    signals = load()
    hit = False
    for s in signals:
        if s.get("ticker") == tk and s.get("agent") == agent:
            s["status"] = "processed"
            s["disposition"] = disposition
            s["processed_ts"] = _now()
            hit = True
    if hit:
        _save(signals)
    return hit


def prune(keep_days: int = 14) -> int:
    """Drop "processed" signals older than keep_days. Returns how many were removed.
    "new" signals are never pruned (they still need a disposition)."""
    now = datetime.now(timezone.utc).astimezone()
    kept = []
    dropped = 0
    for s in load():
        if s.get("status") != "processed":
            kept.append(s)
            continue
        stamp = s.get("processed_ts") or s.get("ts")
        try:
            age_days = (now - datetime.fromisoformat(stamp)).days
        except (TypeError, ValueError):
            kept.append(s)
            continue
        if age_days >= keep_days:
            dropped += 1
        else:
            kept.append(s)
    if dropped:
        _save(kept)
    return dropped


def main() -> int:
    ap = argparse.ArgumentParser(description="The mind's advisory signal inbox (push rail).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("push", help="surface a ticker to the mind (upsert by agent+ticker)")
    pp.add_argument("ticker")
    pp.add_argument("--why", default="")
    pp.add_argument("--source", default="")
    pp.add_argument("--agent", default="")
    sub.add_parser("pending", help="print the signals awaiting a disposition (JSON)")
    pm = sub.add_parser("mark-processed", help="stamp a signal processed with a disposition")
    pm.add_argument("ticker")
    pm.add_argument("--agent", default="")
    pm.add_argument("--disposition", default="")
    pr = sub.add_parser("prune", help="drop processed signals older than --keep-days")
    pr.add_argument("--keep-days", type=int, default=14)
    args = ap.parse_args()
    if args.cmd == "push":
        print(json.dumps(push(args.ticker, why=args.why, source=args.source, agent=args.agent)))
    elif args.cmd == "pending":
        print(json.dumps(pending(), indent=2))
    elif args.cmd == "mark-processed":
        ok = mark_processed(args.ticker, agent=args.agent, disposition=args.disposition)
        print(json.dumps({"matched": ok}))
    elif args.cmd == "prune":
        print(json.dumps({"pruned": prune(keep_days=args.keep_days)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
