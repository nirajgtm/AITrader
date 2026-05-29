#!/usr/bin/env python3
"""Sector relative-strength scan.

Modes:
  sector_scan.py          # human dashboard
  sector_scan.py --json   # compressed JSON for runbook ingest
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from _market import fetch_bulk, ret_pct, vs_ma
from _terse import emit, step_result

SECTORS = {
    "XLK": "Tech",
    "XLE": "Energy",
    "XLF": "Financials",
    "XLV": "Health",
    "XLI": "Industrials",
    "XLU": "Utilities",
    "XLP": "Staples",
    "XLY": "Discretionary",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "XLC": "Comm Services",
}


def _build() -> dict | None:
    """One bulk fetch for SPY + 11 sectors."""
    syms = ["SPY"] + list(SECTORS.keys())
    bulk = fetch_bulk(syms, period="3mo")
    spy = bulk.get("SPY")
    if spy is None or spy.empty:
        return None
    spy_5d = ret_pct(spy, 5)
    spy_20d = ret_pct(spy, 20)

    rows = []
    for tkr, name in SECTORS.items():
        df = bulk.get(tkr)
        if df is None or df.empty:
            continue
        df = df.copy()
        df["MA20"] = df["Close"].rolling(20).mean()
        df["MA50"] = df["Close"].rolling(50).mean()
        last = df.iloc[-1]
        r5 = ret_pct(df, 5)
        r20 = ret_pct(df, 20)
        rs5 = r5 - spy_5d
        rs20 = r20 - spy_20d
        rows.append({
            "etf": tkr, "sector": name,
            "close": round(float(last["Close"]), 2),
            "ret5d": round(r5, 2),
            "ret20d": round(r20, 2),
            "rs5d": round(rs5, 2),
            "rs20d": round(rs20, 2),
            "rotation": round(rs5 - rs20, 2),
            "vs_ma20": vs_ma(last["Close"], last["MA20"]),
            "vs_ma50": vs_ma(last["Close"], last["MA50"]),
        })

    by_rs5d = sorted(rows, key=lambda x: -x["rs5d"])
    by_rotation = sorted(rows, key=lambda x: -x["rotation"])

    return {
        "spy_5d_pct": round(spy_5d, 2),
        "spy_20d_pct": round(spy_20d, 2),
        "leaders_5d": [{"etf": r["etf"], "rs5d": r["rs5d"]} for r in by_rs5d[:3]],
        "laggards_5d": [{"etf": r["etf"], "rs5d": r["rs5d"]} for r in by_rs5d[-3:]],
        "rotation_in": [{"etf": r["etf"], "rotation": r["rotation"]} for r in by_rotation[:3]],
        "rotation_out": [{"etf": r["etf"], "rotation": r["rotation"]} for r in by_rotation[-3:]],
        "all": rows,
    }


def cmd_dashboard(d: dict) -> None:
    print("=== Sector RS scan ===\n")
    print(f"SPY benchmark: 5d {d['spy_5d_pct']:+.2f}%  20d {d['spy_20d_pct']:+.2f}%\n")
    df = pd.DataFrame(d["all"]).sort_values("rs5d", ascending=False)
    print(df.to_string(index=False))
    print("\n5d leaders:  ", ", ".join(f"{r['etf']} ({r['rs5d']:+.2f})" for r in d["leaders_5d"]))
    print("5d laggards:", ", ".join(f"{r['etf']} ({r['rs5d']:+.2f})" for r in d["laggards_5d"]))
    print("Rotation IN: ", ", ".join(f"{r['etf']} ({r['rotation']:+.2f})" for r in d["rotation_in"]))
    print("Rotation OUT:", ", ".join(f"{r['etf']} ({r['rotation']:+.2f})" for r in d["rotation_out"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    d = _build()
    if d is None:
        result = step_result("sectors", ok=False, headline="SPY fetch failed",
                             errors=["spy fetch failed"])
        if args.json:
            emit(result)
        else:
            print("SPY fetch failed", file=sys.stderr)
        return 1

    flags = []
    # Strong rotation signal threshold
    for r in d["rotation_in"]:
        if r["rotation"] > 10:
            flags.append(f"rotation_in_{r['etf'].lower()}")
    for r in d["rotation_out"]:
        if r["rotation"] < -10:
            flags.append(f"rotation_out_{r['etf'].lower()}")

    leaders = ",".join(r["etf"] for r in d["leaders_5d"])
    rotation_top = d["rotation_in"][0] if d["rotation_in"] else None
    headline = f"5d leaders={leaders}"
    if rotation_top:
        headline += f"; rotation→{rotation_top['etf']} ({rotation_top['rotation']:+.1f})"

    result = step_result("sectors", ok=True, headline=headline,
                         data=d, flags=flags)
    if args.json:
        emit(result)
    else:
        cmd_dashboard(d)
        if flags:
            print(f"\nFlags: {flags}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
