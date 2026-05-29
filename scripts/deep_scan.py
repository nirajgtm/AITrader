#!/usr/bin/env python3
"""deep_scan.py — Expanded universe scan for research/triage.

Reads the latest scanner_history cache (built by scanner.py) and computes a
single structured digest with the full top-of-tape that brief.py truncates:
  - top 25 gainers / 25 losers (by 1-day pct change), with vol multiplier
  - all breakouts with vol confirmation (vol_x_avg >= --vol-min, default 1.5)
  - all breakdowns with vol confirmation
  - HIGH_VOL_RETAIL segment moves (the 28-name set added 2026-04-29)

Why this exists:
  brief.py's `candidates` array filters to cross-scanner clusters (>=2 sources).
  When a session needs to triage beyond the cluster (e.g. "why didn't you flag X"),
  this script returns the raw broad-tape view in one structured call instead of
  re-running multiple per-scanner scripts.

Output (--json): step_result schema
  {step, ok, ts, headline, data: {gainers, losers, bo_vol_confirm, bd_vol_confirm,
  hvr_moves, universe_size}, flags, actions, errors}

CLI:
  deep_scan.py --json
  deep_scan.py --json --vol-min 2.0
  deep_scan.py --json --top-n 50

Note: relies on scanner_history_21d cache being fresh. If stale or missing, run
`scanner.py --days 21 --json > /dev/null` first to rebuild cache.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "state" / "cache"

HIGH_VOL_RETAIL = {
    "SOFI", "RIVN", "LCID", "AFRM", "HIMS", "RDDT", "CART", "GME", "PLTR", "HOOD",
    "MARA", "RIOT", "COIN", "PATH", "UPST", "OPEN", "NIO", "XPEV", "LI", "BB",
    "AMC", "SOUN", "BBAI", "RKLB", "ASTS", "ACHR", "JOBY", "U", "FUBO", "PSKY", "BBBY",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _latest_history_cache() -> Path | None:
    files = sorted(CACHE.glob("scanner_history_21d_*.json"))
    return files[-1] if files else None


def _compute(bars: list, ticker: str) -> dict | None:
    if not bars or len(bars) < 5:
        return None
    today = bars[-1]
    prior = bars[-2]
    prior_c = float(prior.get("c") or 0)
    if not prior_c:
        return None
    pct = (float(today["c"]) - prior_c) / prior_c * 100
    avg_vol = sum(float(b.get("v", 0)) for b in bars[:-1]) / max(1, len(bars) - 1)
    today_v = float(today.get("v", 0))
    vol_mult = today_v / avg_vol if avg_vol else 0
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    is_bo = float(today["c"]) >= max(highs[:-1]) if len(highs) > 1 else False
    is_bd = float(today["c"]) <= min(lows[:-1]) if len(lows) > 1 else False
    return {
        "tk": ticker,
        "c": round(float(today["c"]), 2),
        "pct": round(pct, 2),
        "vol_x_avg": round(vol_mult, 2),
        "bo": is_bo,
        "bd": is_bd,
        "hvr": ticker in HIGH_VOL_RETAIL,
        "date": today.get("date"),
    }


def run(top_n: int = 25, vol_min: float = 1.5) -> dict:
    started = _now()
    errors: list = []
    flags: list = []

    cache_file = _latest_history_cache()
    if not cache_file:
        return {
            "step": "deep_scan",
            "ok": False,
            "ts": started,
            "headline": "no scanner_history cache found",
            "data": {},
            "flags": [],
            "actions": [],
            "errors": ["scanner_history_21d_*.json missing — run scanner.py first"],
        }

    try:
        cache = json.loads(cache_file.read_text())
    except Exception as e:
        errors.append(f"cache parse: {e}")
        cache = {"data": {}}

    universe = cache.get("data", {})
    rows = []
    for ticker, bars in universe.items():
        r = _compute(bars, ticker)
        if r:
            rows.append(r)

    rows.sort(key=lambda x: x["pct"])
    losers = rows[:top_n]
    gainers = rows[-top_n:][::-1]

    bo_confirm = sorted(
        [r for r in rows if r["bo"] and r["vol_x_avg"] >= vol_min],
        key=lambda x: -x["pct"],
    )
    bd_confirm = sorted(
        [r for r in rows if r["bd"] and r["vol_x_avg"] >= vol_min],
        key=lambda x: x["pct"],
    )
    hvr_moves = sorted(
        [r for r in rows if r["hvr"]],
        key=lambda x: -abs(x["pct"]),
    )

    if any(abs(r["pct"]) >= 10 for r in rows[:5]):
        flags.append("extreme_movers_today")
    if len(bd_confirm) > 2 * len(bo_confirm) and len(bd_confirm) >= 10:
        flags.append("distribution_skew")
    if len(bo_confirm) > 2 * len(bd_confirm) and len(bo_confirm) >= 10:
        flags.append("accumulation_skew")
    hvr_red = sum(1 for r in hvr_moves if r["pct"] < -3)
    hvr_green = sum(1 for r in hvr_moves if r["pct"] > 3)
    if hvr_red >= 5:
        flags.append("retail_risk_off")
    if hvr_green >= 5:
        flags.append("retail_risk_on")

    last_bar_date = next(
        (universe[t][-1].get("date") for t in universe if universe[t]),
        None,
    )

    headline = (
        f"univ={len(rows)} BO_v={len(bo_confirm)} BD_v={len(bd_confirm)} "
        f"HVR={len(hvr_moves)} cache_date={last_bar_date}"
    )

    return {
        "step": "deep_scan",
        "ok": True,
        "ts": started,
        "headline": headline,
        "data": {
            "universe_size": len(rows),
            "cache_date": last_bar_date,
            "cache_file": cache_file.name,
            "vol_min_threshold": vol_min,
            "gainers": gainers,
            "losers": losers,
            "bo_vol_confirm": bo_confirm,
            "bd_vol_confirm": bd_confirm,
            "hvr_moves": hvr_moves,
        },
        "flags": flags,
        "actions": [],
        "errors": errors,
    }


def _print_human(d: dict) -> None:
    data = d.get("data", {})
    print(f"=== DEEP SCAN === {d['headline']}")
    print(f"flags: {','.join(d.get('flags', [])) or '-'}")
    print()
    print(f"--- TOP {len(data.get('losers', []))} LOSERS ---")
    for r in data.get("losers", []):
        tags = " ".join(t for t in [
            "BD" if r["bd"] else "",
            "HVR" if r["hvr"] else "",
        ] if t)
        print(f"{r['tk']:6s} {r['pct']:>+7.2f}% close={r['c']:>9.2f} vol×{r['vol_x_avg']:>4.1f}  {tags}")
    print()
    print(f"--- TOP {len(data.get('gainers', []))} GAINERS ---")
    for r in data.get("gainers", []):
        tags = " ".join(t for t in [
            "BO" if r["bo"] else "",
            "HVR" if r["hvr"] else "",
        ] if t)
        print(f"{r['tk']:6s} {r['pct']:>+7.2f}% close={r['c']:>9.2f} vol×{r['vol_x_avg']:>4.1f}  {tags}")
    print()
    print(f"--- BREAKDOWNS vol×>={data.get('vol_min_threshold')} ---")
    for r in data.get("bd_vol_confirm", []):
        hvr = " HVR" if r["hvr"] else ""
        print(f"{r['tk']:6s} {r['pct']:>+7.2f}% close={r['c']:>9.2f} vol×{r['vol_x_avg']:>4.1f}{hvr}")
    print()
    print(f"--- BREAKOUTS vol×>={data.get('vol_min_threshold')} ---")
    for r in data.get("bo_vol_confirm", []):
        hvr = " HVR" if r["hvr"] else ""
        print(f"{r['tk']:6s} {r['pct']:>+7.2f}% close={r['c']:>9.2f} vol×{r['vol_x_avg']:>4.1f}{hvr}")
    print()
    print(f"--- HVR moves (all {len(data.get('hvr_moves', []))}) ---")
    for r in data.get("hvr_moves", []):
        tag = "BO" if r["bo"] else "BD" if r["bd"] else ""
        print(f"{r['tk']:6s} {r['pct']:>+7.2f}% close={r['c']:>9.2f} vol×{r['vol_x_avg']:>4.1f}  {tag}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="emit step_result JSON")
    ap.add_argument("--top-n", type=int, default=25, help="top N gainers/losers (default 25)")
    ap.add_argument("--vol-min", type=float, default=1.5,
                    help="minimum vol multiplier for BO/BD confirmation (default 1.5)")
    args = ap.parse_args()

    result = run(top_n=args.top_n, vol_min=args.vol_min)

    if args.json:
        print(json.dumps(result))
    else:
        _print_human(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
