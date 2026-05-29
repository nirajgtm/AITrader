#!/usr/bin/env python3
"""Sentiment / volatility-structure dashboard.

Pulls:
  - VIX (^VIX) — 30-day implied vol on SPX
  - VIX9D (^VIX9D) — 9-day forward
  - VIX3M (^VIX3M) — 3-month forward
  - VVIX (^VVIX) — vol of vol
  - SKEW (^SKEW) — tail-risk pricing
  - VIX/VIX3M ratio — contango (<1 = healthy) / backwardation (>1 = stress)

Cached for 15 minutes (intraday-stale OK for swing decisions).

Usage:
  sentiment.py                # full dashboard
  sentiment.py --json         # machine-readable
"""
from __future__ import annotations

import argparse
import json
import sys

import yfinance as yf

from _cache import cached
from _terse import emit, step_result

CACHE_TTL = 900  # 15 minutes


@cached("sentiment_snapshot", ttl_seconds=CACHE_TTL)
def fetch_snapshot() -> dict:
    out: dict = {}
    symbols = {
        "VIX": "^VIX",
        "VIX9D": "^VIX9D",
        "VIX3M": "^VIX3M",
        "VVIX": "^VVIX",
        "SKEW": "^SKEW",
    }
    for label, sym in symbols.items():
        try:
            h = yf.Ticker(sym).history(period="20d")
            if h.empty:
                out[label] = None
                continue
            last = float(h["Close"].iloc[-1])
            prev = float(h["Close"].iloc[-2]) if len(h) > 1 else last
            chg = last - prev
            chg_pct = chg / prev * 100 if prev else 0
            ma5 = float(h["Close"].tail(5).mean())
            ma20 = float(h["Close"].mean())
            out[label] = {
                "last": round(last, 2),
                "chg": round(chg, 2),
                "chg_pct": round(chg_pct, 2),
                "ma5": round(ma5, 2),
                "ma20": round(ma20, 2),
            }
        except Exception as e:
            out[label] = {"error": str(e)[:80]}

    # VIX term-structure ratio
    vix = (out.get("VIX") or {}).get("last")
    vix3m = (out.get("VIX3M") or {}).get("last")
    if vix and vix3m:
        ratio = vix / vix3m
        out["VIX_VIX3M_ratio"] = round(ratio, 3)
        if ratio < 0.95:
            out["term_structure"] = "STEEP_CONTANGO — healthy / risk-on bias"
        elif ratio < 1.0:
            out["term_structure"] = "CONTANGO — normal"
        elif ratio < 1.05:
            out["term_structure"] = "FLAT — vol pricing in stress"
        else:
            out["term_structure"] = "BACKWARDATION — stress regime; mean-revert windows on SPY"

    # SKEW interpretation
    skew = (out.get("SKEW") or {}).get("last")
    if skew:
        if skew < 130:
            out["skew_read"] = "low tail-risk pricing"
        elif skew < 145:
            out["skew_read"] = "normal tail-risk"
        else:
            out["skew_read"] = "ELEVATED tail-risk pricing"

    # Put/call ratio (CBOE total) — proxy via SPY chain (free)
    try:
        spy = yf.Ticker("SPY")
        exps = spy.options or []
        if exps:
            chain = spy.option_chain(exps[0])
            put_vol = float(chain.puts["volume"].fillna(0).sum())
            call_vol = float(chain.calls["volume"].fillna(0).sum())
            pcr = put_vol / max(call_vol, 1)
            out["spy_front_pcr"] = {
                "put_volume": int(put_vol),
                "call_volume": int(call_vol),
                "ratio": round(pcr, 3),
                "read": ("BEARISH skew" if pcr > 1.2
                         else "BULLISH skew" if pcr < 0.7
                         else "neutral"),
                "_caveat": "front-expiry SPY only, not CBOE total PCR",
            }
    except Exception as e:
        out["spy_front_pcr"] = {"error": str(e)[:80]}

    return out


def cmd_dashboard(snap: dict) -> None:
    print("=== Sentiment / Vol Structure ===\n")
    for label in ("VIX", "VIX9D", "VIX3M", "VVIX", "SKEW"):
        d = snap.get(label)
        if d is None:
            print(f"{label}: n/a")
        elif "error" in d:
            print(f"{label}: error — {d['error']}")
        else:
            print(f"{label:<6} {d['last']:>7.2f}  ({d['chg']:+.2f}, {d['chg_pct']:+.2f}%)  "
                  f"MA5={d['ma5']:.2f}  MA20={d['ma20']:.2f}")
    print()
    if "VIX_VIX3M_ratio" in snap:
        print(f"VIX/VIX3M = {snap['VIX_VIX3M_ratio']}  →  {snap.get('term_structure', '?')}")
    if "skew_read" in snap:
        print(f"SKEW read: {snap['skew_read']}")
    pcr = snap.get("spy_front_pcr")
    if pcr and "ratio" in pcr:
        print(f"SPY front PCR: {pcr['ratio']}  ({pcr['read']})  "
              f"[puts={pcr['put_volume']}, calls={pcr['call_volume']}]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    snap = fetch_snapshot()
    flags = []
    if "term_structure" in snap and "BACKWARDATION" in snap["term_structure"]:
        flags.append("vix_term_backwardation")
    if "term_structure" in snap and "FLAT" in snap["term_structure"]:
        flags.append("vix_term_flat")
    if snap.get("skew_read", "").startswith("ELEVATED"):
        flags.append("skew_elevated")
    pcr = snap.get("spy_front_pcr") or {}
    if pcr.get("ratio") and pcr["ratio"] > 1.2:
        flags.append("pcr_bearish_skew")

    vix = (snap.get("VIX") or {}).get("last")
    headline_parts = []
    if vix:
        headline_parts.append(f"VIX={vix}")
    if "VIX_VIX3M_ratio" in snap:
        ts = snap.get("term_structure", "").split(" — ")[0]
        headline_parts.append(f"VIX/VIX3M={snap['VIX_VIX3M_ratio']} ({ts})")
    if pcr.get("ratio"):
        headline_parts.append(f"PCR={pcr['ratio']}")
    headline = "; ".join(headline_parts)

    result = step_result("sentiment", ok=True, headline=headline,
                         data=snap, flags=flags)
    if args.json:
        emit(result)
    else:
        cmd_dashboard(snap)
        if flags:
            print(f"\nFlags: {flags}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
