#!/usr/bin/env python3
"""Macro calendar — FOMC, CPI, jobs, GDP, and recent FRED prints.

Sources (all free):
  - PRIMARY: FMP /economic-calendar (free tier; event-time precision, US filter)
  - FALLBACK: maintained static FOMC + scheduled releases list (when FMP unavailable)
  - FRED for historical macro prints (CPI YoY, unemployment, rates, VIX, DXY)

Usage:
  macro.py                     # next 14d events + recent FRED prints
  macro.py --days 7
  macro.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from _cache import cache_get, cache_put
from _terse import emit, step_result

CACHE_TTL_FRED = 6 * 3600
CACHE_TTL_CAL = 24 * 3600

# Static FOMC schedule. Update twice a year (March/September Fed schedule release).
# Format: ISO date, hh:mm ET, kind, notes
FOMC_2026 = [
    {"date": "2026-01-28", "type": "FOMC decision", "time_et": "14:00"},
    {"date": "2026-03-18", "type": "FOMC decision + SEP + Powell presser", "time_et": "14:00"},
    {"date": "2026-04-29", "type": "FOMC decision + Powell presser", "time_et": "14:00"},
    {"date": "2026-06-17", "type": "FOMC decision + SEP", "time_et": "14:00"},
    {"date": "2026-07-29", "type": "FOMC decision + Powell presser", "time_et": "14:00"},
    {"date": "2026-09-16", "type": "FOMC decision + SEP", "time_et": "14:00"},
    {"date": "2026-11-04", "type": "FOMC decision + Powell presser", "time_et": "14:00"},
    {"date": "2026-12-09", "type": "FOMC decision + SEP", "time_et": "14:00"},
]

# Static known major releases (CPI, jobs typically first Friday). Update monthly.
SCHEDULED_RELEASES_2026 = [
    {"date": "2026-04-30", "type": "GDP Q1 advance", "time_et": "08:30"},
    {"date": "2026-05-02", "type": "Jobs Apr (NFP, unemployment)", "time_et": "08:30"},
    {"date": "2026-05-13", "type": "CPI Apr", "time_et": "08:30"},
    {"date": "2026-05-15", "type": "Retail Sales Apr", "time_et": "08:30"},
    {"date": "2026-05-29", "type": "PCE Apr", "time_et": "08:30"},
    {"date": "2026-06-06", "type": "Jobs May", "time_et": "08:30"},
    {"date": "2026-06-12", "type": "CPI May", "time_et": "08:30"},
]

# FRED series of interest
FRED_SERIES = {
    "CPI_YOY": "CPIAUCSL",            # CPI all items, YoY computed below
    "CORE_CPI_YOY": "CPILFESL",
    "UNEMPLOYMENT": "UNRATE",
    "FED_FUNDS_TARGET_UPPER": "DFEDTARU",
    "10Y": "DGS10",
    "2Y": "DGS2",
    "VIX": "VIXCLS",
    "DXY_PROXY": "DTWEXBGS",          # broad dollar index
}


def _fred(series: str) -> dict | None:
    """Fetch latest 2 observations for a FRED series.

    Uses fredapi if FRED_API_KEY env var is set; else uses the public unauth'd
    fred.stlouisfed.org/graph/fredgraph.csv endpoint as a fallback.
    """
    cache_key = f"fred_{series}"
    cached = cache_get(cache_key, ttl_seconds=CACHE_TTL_FRED)
    if cached is not None:
        return cached

    key = os.environ.get("FRED_API_KEY")
    if key:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {"series_id": series, "api_key": key, "file_type": "json",
                  "sort_order": "desc", "limit": 13}
        try:
            r = requests.get(url, params=params, timeout=15)
            data = r.json()
            obs = data.get("observations", [])
            if not obs:
                return None
            # latest non-".":
            valid = [o for o in obs if o.get("value") not in (".", "", None)]
            if not valid:
                return None
            latest = valid[0]
            yoy = None
            if len(valid) >= 13:
                year_ago = valid[12]
                try:
                    yoy = (float(latest["value"]) / float(year_ago["value"]) - 1) * 100
                except Exception:
                    yoy = None
            out = {
                "value": float(latest["value"]),
                "date": latest["date"],
                "yoy_pct": round(yoy, 2) if yoy is not None else None,
            }
            cache_put(cache_key, out)
            return out
        except Exception:
            return None
    else:
        # Fallback: graph CSV (no key required)
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
        try:
            r = requests.get(url, timeout=15)
            if r.status_code != 200:
                return None
            lines = [l for l in r.text.splitlines() if l]
            # CSV: DATE,SERIES_ID
            rows = [l.split(",") for l in lines[1:]]
            rows = [(d, v) for d, v in rows if v not in (".", "")]
            if not rows:
                return None
            latest_date, latest_val = rows[-1]
            yoy = None
            if len(rows) >= 13:
                ya_date, ya_val = rows[-13]
                try:
                    yoy = (float(latest_val) / float(ya_val) - 1) * 100
                except Exception:
                    yoy = None
            out = {
                "value": float(latest_val),
                "date": latest_date,
                "yoy_pct": round(yoy, 2) if yoy is not None else None,
            }
            cache_put(cache_key, out)
            return out
        except Exception:
            return None


def upcoming_calendar(days: int = 14) -> list[dict]:
    """Use FMP economic calendar (primary). Fall back to static list if FMP fails.

    Both shapes normalized to: {date, type, days_out, time_et?, impact?}
    """
    today = date.today()
    cutoff = today + timedelta(days=days)

    # Primary: FMP
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _providers import fmp as _fmp_factory
        f = _fmp_factory()
        events = f.economic_calendar(days_ahead=days, country="US", impact_min="Medium")
    except Exception as e:
        import os
        if os.environ.get("MACRO_DEBUG"):
            import traceback; traceback.print_exc()
        events = None
    try:
        if events is not None and len(events) > 0:
            out = []
            for e in events:
                # FMP date format: "2026-04-29 14:00:00"
                ed_str = e.get("date", "")
                try:
                    d_part = ed_str.split(" ")[0]
                    t_part = ed_str.split(" ")[1] if " " in ed_str else ""
                    d_obj = date.fromisoformat(d_part)
                except Exception:
                    continue
                days_out = (d_obj - today).days
                if days_out < 0:
                    continue
                out.append({
                    "date": d_part,
                    "type": e.get("event", "?"),
                    "time_et": t_part[:5] if t_part else None,
                    "impact": e.get("impact"),
                    "previous": e.get("previous"),
                    "estimate": e.get("estimate"),
                    "actual": e.get("actual"),
                    "days_out": days_out,
                    "_source": "fmp",
                })
            out.sort(key=lambda x: x["date"])
            return out
    except Exception as e:
        import os
        if os.environ.get("MACRO_DEBUG"):
            import traceback; traceback.print_exc()

    # Fallback: static list
    items = FOMC_2026 + SCHEDULED_RELEASES_2026
    out = []
    for it in items:
        try:
            d = date.fromisoformat(it["date"])
        except Exception:
            continue
        if today <= d <= cutoff:
            days_out = (d - today).days
            out.append({**it, "days_out": days_out, "_source": "static"})
    out.sort(key=lambda x: x["date"])
    return out


def fred_snapshot() -> dict:
    out = {}
    for label, series in FRED_SERIES.items():
        out[label] = _fred(series)
    # Yield curve flag
    ten = (out.get("10Y") or {}).get("value")
    two = (out.get("2Y") or {}).get("value")
    if ten and two:
        spread = round(ten - two, 2)
        out["yield_curve"] = {"10Y_minus_2Y": spread,
                              "shape": "INVERTED" if spread < 0 else "normal"}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14, help="Calendar window (days)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cal = upcoming_calendar(args.days)
    fred = fred_snapshot()

    flags = []
    if any(c["type"].startswith("FOMC") and c["days_out"] <= 7 for c in cal):
        flags.append("fomc_within_7d")
    if any("CPI" in c["type"] and c["days_out"] <= 7 for c in cal):
        flags.append("cpi_within_7d")
    if any(("Jobs" in c["type"] or "NFP" in c["type"]) and c["days_out"] <= 7 for c in cal):
        flags.append("nfp_within_7d")

    headline = ""
    if cal:
        next_e = cal[0]
        headline = f"next: {next_e['date']} T+{next_e['days_out']} {next_e['type']}"

    data = {"calendar": cal, "fred": fred}
    result = step_result("macro", ok=True, headline=headline, data=data, flags=flags)

    if args.json:
        emit(result)
        return 0

    print(f"=== Upcoming macro events ({args.days}d) ===")
    if not cal:
        print("(none in window)")
    else:
        for e in cal:
            print(f"  {e['date']}  T+{e['days_out']:<2}  {e.get('time_et','--:--')} ET  {e['type']}")
    print()
    print("=== Recent FRED prints ===")
    for label, d in fred.items():
        if d is None:
            print(f"  {label:<24}: n/a")
        elif label == "yield_curve":
            print(f"  yield_curve_10Y_2Y      : {d['10Y_minus_2Y']:+.2f}  ({d['shape']})")
        else:
            yoy = f"  YoY={d['yoy_pct']:+.2f}%" if d.get("yoy_pct") is not None else ""
            print(f"  {label:<24}: {d['value']:>8.3f}  ({d['date']}){yoy}")

    print()
    if os.environ.get("FRED_API_KEY"):
        print("[FRED_API_KEY detected — using fredapi endpoint]")
    else:
        print("[FRED_API_KEY not set — using fredgraph.csv fallback. "
              "Get free key at fredaccount.stlouisfed.org for richer data.]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
