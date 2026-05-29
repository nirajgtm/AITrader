#!/usr/bin/env python3
"""The autonomous trader's OWN watchlist (state/autonomous_watchlist.json).

Separate from the brief's candidates and from the broader /trader watchlist. When
the system (or the LLM during a run) likes a name but the entry isn't right yet, it
parks it here. Two kinds of entry:

- trigger: has a price level + condition; goes `ready` when price crosses the level.
  The "enter at X" setups. {ticker, kind:"trigger", condition(at_or_below|at_or_above),
  level, expected_trend, hypothesis, target, stop, added, notes}
- monitor: no hard trigger, just a name to actively watch (a dip, a surge, an event,
  or any thesis). Never goes `ready` on its own; it persists as something each run
  re-reads and judges. {ticker, kind:"monitor", watch_for, expected_trend, hypothesis,
  target, stop, added, notes}

Either way, being `ready` or on the monitor list is never itself a reason to buy,
only a reason to run the SAME full deep-dive + FOR/AGAINST as any other action.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

DIR = Path(__file__).resolve().parent
PATH = DIR / "state" / "autonomous_watchlist.json"
MIRROR = DIR / "state" / "watching.json"


def load() -> list:
    if PATH.exists():
        try:
            return json.loads(PATH.read_text()).get("entries", [])
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save(entries: list) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps({"updated": date.today().isoformat(), "entries": entries}, indent=2))
    _sync_dashboard_mirror(entries)


def add(ticker: str, condition: str, level: float, *, hypothesis: str = "",
        expected_trend: str = "", target: float | None = None, stop: float | None = None,
        notes: str = "") -> dict:
    """Upsert a watch by ticker. condition in {at_or_below, at_or_above}."""
    if condition not in ("at_or_below", "at_or_above"):
        raise ValueError("condition must be at_or_below or at_or_above")
    entries = load()
    e = {"ticker": ticker.upper(), "kind": "trigger", "condition": condition, "level": float(level),
         "expected_trend": expected_trend, "hypothesis": hypothesis,
         "target": target, "stop": stop, "added": date.today().isoformat(), "notes": notes}
    entries = [x for x in entries if x["ticker"] != ticker.upper()] + [e]
    _save(entries)
    return e


def watch(ticker: str, watch_for: str, *, hypothesis: str = "", expected_trend: str = "",
          target: float | None = None, stop: float | None = None, notes: str = "") -> dict:
    """Park a name to actively MONITOR with no hard price trigger (a dip, a surge, an
    event, or any thesis). Never becomes `ready` on its own; it persists as something
    each run re-reads and judges. Upserts by ticker."""
    entries = load()
    e = {"ticker": ticker.upper(), "kind": "monitor", "watch_for": watch_for,
         "expected_trend": expected_trend, "hypothesis": hypothesis,
         "target": target, "stop": stop, "added": date.today().isoformat(), "notes": notes}
    entries = [x for x in entries if x["ticker"] != ticker.upper()] + [e]
    _save(entries)
    return e


def remove(ticker: str) -> None:
    _save([x for x in load() if x["ticker"] != ticker.upper()])


def _kind(entry: dict) -> str:
    k = entry.get("kind")
    if k in ("trigger", "monitor"):
        return k
    return "trigger" if entry.get("level") is not None else "monitor"


def is_ready(entry: dict, last: float | None) -> bool:
    # Monitor entries have no price trigger; they never auto-fire, the run judges them.
    if _kind(entry) == "monitor" or entry.get("level") is None or last is None:
        return False
    if entry["condition"] == "at_or_below":
        return last <= entry["level"]
    return last >= entry["level"]


def _sync_dashboard_mirror(entries: list) -> None:
    """Keep watching.json (the dashboard watch-tab source) in sync with the live
    watchlist so adds/removes show immediately, not only after the next context build.
    Carries over each ticker's cached `last` from the existing mirror and recomputes
    `ready`; new entries get last=None/ready=False until the next run enriches them."""
    try:
        prev = {}
        if MIRROR.exists():
            for w in (json.loads(MIRROR.read_text()).get("watching") or []):
                if isinstance(w, dict) and w.get("ticker"):
                    prev[w["ticker"]] = w
        watching = []
        for e in entries:
            last = prev.get(e.get("ticker"), {}).get("last")
            watching.append({**e, "last": last, "ready": is_ready(e, last)})
        MIRROR.write_text(json.dumps(
            {"updated": date.today().isoformat(), "watching": watching}, indent=2))
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Autonomous trader's own watchlist.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pa = sub.add_parser("add")
    pa.add_argument("ticker"); pa.add_argument("condition", choices=["at_or_below", "at_or_above"])
    pa.add_argument("level", type=float)
    pa.add_argument("--hypothesis", default=""); pa.add_argument("--trend", default="")
    pa.add_argument("--target", type=float, default=None); pa.add_argument("--stop", type=float, default=None)
    pa.add_argument("--notes", default="")
    pw = sub.add_parser("watch")
    pw.add_argument("ticker"); pw.add_argument("--for", dest="watch_for", required=True)
    pw.add_argument("--hypothesis", default=""); pw.add_argument("--trend", default="")
    pw.add_argument("--target", type=float, default=None); pw.add_argument("--stop", type=float, default=None)
    pw.add_argument("--notes", default="")
    pr = sub.add_parser("remove"); pr.add_argument("ticker")
    sub.add_parser("list")
    args = ap.parse_args()
    if args.cmd == "add":
        print(json.dumps(add(args.ticker, args.condition, args.level, hypothesis=args.hypothesis,
                             expected_trend=args.trend, target=args.target, stop=args.stop,
                             notes=args.notes)))
    elif args.cmd == "watch":
        print(json.dumps(watch(args.ticker, args.watch_for, hypothesis=args.hypothesis,
                               expected_trend=args.trend, target=args.target, stop=args.stop,
                               notes=args.notes)))
    elif args.cmd == "remove":
        remove(args.ticker); print("removed")
    elif args.cmd == "list":
        print(json.dumps(load(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
