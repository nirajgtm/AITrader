#!/usr/bin/env python3
"""Research log — track when scans were last run so we don't redo work.

Each completed research session writes one JSONL line with start/end timestamps,
scripts run, and a summary. Subsequent invocations call `research.py freshness`
to decide whether cached data is still good or a re-run is needed.

Usage:
  research.py log --start <ISO> --end <ISO> --kind full|quick \
                  --scripts "regime,sector_scan,flow_scan" \
                  --summary "..." [--decisions "..."] [--gaps "..."]
  research.py last                 # most recent entry + age
  research.py today                # all entries from today (local date)
  research.py freshness            # stale | fresh-quick | fresh-full + age_hours
                                   # exit code: 0=fresh, 1=stale
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _common import RESEARCH_LOG_PATH, now_iso

# Freshness thresholds (in hours)
FRESH_FULL_HOURS = 4     # < this → full freshness, no need to re-run anything
FRESH_QUICK_HOURS = 12   # < this → fresh enough for slow-changing inputs
                         # (earnings cal, congress, insider, macro events)
                         # but re-pull regime/sectors/flow for intraday accuracy


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> datetime:
    s = s.replace("Z", "+00:00") if s.endswith("Z") else s
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_hours(ts: str) -> float:
    return (_now_utc() - _parse_iso(ts)).total_seconds() / 3600


def _read_all() -> list[dict]:
    if not RESEARCH_LOG_PATH.exists():
        return []
    out = []
    with RESEARCH_LOG_PATH.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return out


def _local_date_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def cmd_log(args: argparse.Namespace) -> int:
    end_ts = args.end or now_iso()
    start_ts = args.start or end_ts
    # Sanity: if start > end, swap.
    try:
        if _parse_iso(start_ts) > _parse_iso(end_ts):
            start_ts, end_ts = end_ts, start_ts
    except Exception:
        pass
    duration_min = None
    try:
        d = (_parse_iso(end_ts) - _parse_iso(start_ts)).total_seconds() / 60
        duration_min = round(max(d, 0), 1)
    except Exception:
        pass

    entry = {
        "ts_start": start_ts,
        "ts_end": end_ts,
        "duration_min": duration_min,
        "local_date": _local_date_str(),
        "kind": args.kind,
        "scripts": [s.strip() for s in (args.scripts or "").split(",") if s.strip()],
        "summary": args.summary,
    }
    if args.decisions:
        entry["decisions"] = args.decisions
    if args.gaps:
        entry["data_gaps"] = [g.strip() for g in args.gaps.split(",") if g.strip()]
    if args.key_data:
        try:
            entry["key_data"] = json.loads(args.key_data)
        except Exception:
            entry["key_data_raw"] = args.key_data

    with RESEARCH_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    print(json.dumps(entry, indent=2))
    return 0


def cmd_last(_args: argparse.Namespace) -> int:
    entries = _read_all()
    if not entries:
        print("No research sessions logged yet.")
        return 0
    last = entries[-1]
    age_h = _age_hours(last["ts_end"])
    print(f"=== Last research session ===")
    print(f"  When:         {last['ts_end']}  ({_human_age(age_h)} ago)")
    print(f"  Local date:   {last.get('local_date','?')}")
    print(f"  Kind:         {last.get('kind','?')}")
    print(f"  Duration:     {last.get('duration_min','?')} min")
    print(f"  Scripts run:  {', '.join(last.get('scripts', []) or ['—'])}")
    print(f"  Summary:      {last.get('summary','—')}")
    if last.get("decisions"):
        print(f"  Decisions:    {last['decisions']}")
    if last.get("data_gaps"):
        print(f"  Data gaps:    {', '.join(last['data_gaps'])}")
    if last.get("key_data"):
        print(f"  Key data:     {json.dumps(last['key_data'])}")
    return 0


def cmd_today(_args: argparse.Namespace) -> int:
    today = _local_date_str()
    entries = [e for e in _read_all() if e.get("local_date") == today]
    if not entries:
        print(f"No research sessions today ({today}).")
        return 0
    print(f"=== Research sessions today ({today}) ===")
    for e in entries:
        age_h = _age_hours(e["ts_end"])
        print(f"  [{_human_age(age_h)} ago] {e.get('kind','?'):>5}  "
              f"{e.get('summary','—')[:80]}")
    return 0


def cmd_freshness(_args: argparse.Namespace) -> int:
    entries = _read_all()
    if not entries:
        print("STALE  age=∞  no sessions logged")
        return 1
    # Hourly runs are lightweight (no scanner/breadth/insider/congress/sectors
    # rebuild) and must NOT count as a fresh research session. Otherwise the
    # /trader skill picks FRESH-FULL after every cron fire and serves stale
    # data on the next user invocation. Filter to "heavy" kinds for the
    # baseline; fall back to whatever's there if nothing heavy logged.
    HEAVY_KINDS = {"full", "quick", "manual"}
    heavy = [e for e in entries if e.get("kind") in HEAVY_KINDS]
    last = heavy[-1] if heavy else entries[-1]
    age_h = _age_hours(last["ts_end"])
    today = _local_date_str()
    same_day = last.get("local_date") == today

    if not same_day:
        status = "STALE"
        rec = "full re-run required (not same calendar day)"
        rc = 1
    elif age_h < FRESH_FULL_HOURS:
        status = "FRESH-FULL"
        rec = "use cached results; offer user a re-run; do not re-run unless asked"
        rc = 0
    elif age_h < FRESH_QUICK_HOURS:
        status = "FRESH-QUICK"
        rec = "re-pull only fast-changing data (regime, sectors, flow); reuse cached fundamentals"
        rc = 0
    else:
        status = "STALE"
        rec = "full re-run required"
        rc = 1

    print(f"{status}  age={_human_age(age_h)}  last_kind={last.get('kind','?')}")
    print(f"  recommendation: {rec}")
    print(f"  last summary:   {last.get('summary','—')}")
    return rc


def _human_age(hours: float) -> str:
    if hours < 1:
        return f"{int(hours*60)}m"
    if hours < 24:
        h = int(hours)
        m = int((hours - h) * 60)
        return f"{h}h{m:02d}m"
    days = hours / 24
    return f"{days:.1f}d"


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("log")
    lp.add_argument("--start", help="ISO timestamp (default: same as --end)")
    lp.add_argument("--end", help="ISO timestamp (default: now)")
    lp.add_argument("--kind", choices=["full", "quick", "manual", "hourly"], default="full")
    lp.add_argument("--scripts", help="comma-separated scripts run")
    lp.add_argument("--summary", required=True)
    lp.add_argument("--decisions")
    lp.add_argument("--gaps", help="comma-separated data gaps")
    lp.add_argument("--key-data", help="JSON dict of key data points captured")

    sub.add_parser("last")
    sub.add_parser("today")
    sub.add_parser("freshness")

    args = ap.parse_args()
    return {"log": cmd_log, "last": cmd_last, "today": cmd_today,
            "freshness": cmd_freshness}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
