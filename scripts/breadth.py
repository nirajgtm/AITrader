#!/usr/bin/env python3
"""Market breadth — broad participation in the rally/selloff.

yfinance no longer carries the StockCharts breadth tickers (^MMTH, ^MMFI, etc).
Workaround: compute breadth from a representative large-cap basket and report
sector-ETF + ratio-pair indicators that ARE available.

Indicators:
  1. Basket breadth: % of S&P-100 representative basket above 50MA / 200MA.
     Computed live from a 60-stock cached basket (cached 1 day).
  2. Sector breadth: % of 11 sector ETFs above 50MA / 200MA.
  3. RSP/SPY ratio: equal-weight vs cap-weight — diverging = narrow rally.
  4. IWM/SPY ratio: small caps vs large caps — risk-on broadens, risk-off narrows.
  5. QQQ/SPY ratio: tech leadership.

Cached for 30 minutes.

Usage:
  breadth.py
  breadth.py --json
"""
from __future__ import annotations

import argparse
import json
import sys

import pandas as pd
import yfinance as yf

from _cache import cache_get, cache_put
from _market import fetch_bulk
from _terse import emit, step_result

CACHE_TTL = 1800

# 60-name large-cap basket. Cap-weighted leaders + diversification across sectors.
BASKET = [
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AVGO", "ORCL", "CRM",
    "ADBE", "CSCO", "INTC", "AMD", "QCOM",
    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "AXP", "V", "MA",
    # Healthcare
    "UNH", "JNJ", "PFE", "MRK", "ABBV", "LLY", "TMO", "ABT", "DHR",
    # Industrials
    "BA", "CAT", "GE", "HON", "RTX", "LMT", "UPS",
    # Energy
    "XOM", "CVX", "COP",
    # Staples
    "PG", "KO", "PEP", "WMT", "COST",
    # Discretionary
    "HD", "MCD", "NKE", "SBUX",
    # Comm
    "DIS", "T", "VZ",
    # Materials/RE/Util
    "LIN", "DE", "AMT", "NEE",
]

SECTORS = ["XLK", "XLF", "XLV", "XLI", "XLE", "XLP", "XLY", "XLB", "XLRE", "XLU", "XLC"]


def _basket_breadth(basket: list[str], ma_period: int, cache_key: str) -> dict | None:
    """Compute % of basket above MA. Single bulk fetch."""
    cached = cache_get(cache_key, ttl_seconds=CACHE_TTL)
    if cached is not None:
        return cached
    bulk = fetch_bulk(basket, period="1y")
    above = 0
    n = 0
    failures = 0
    for t in basket:
        df = bulk.get(t.upper())
        if df is None or df.empty or len(df) < ma_period:
            failures += 1
            continue
        ma = df["Close"].rolling(ma_period).mean().iloc[-1]
        if pd.isna(ma):
            failures += 1
            continue
        n += 1
        if df["Close"].iloc[-1] > ma:
            above += 1
    if n == 0:
        return None
    pct = round(above / n * 100, 1)
    out = {"basket_size": len(basket), "sampled": n, "above_ma": above,
           "pct_above": pct, "fetch_failures": failures}
    cache_put(cache_key, out)
    return out


def _ratio(num: str, denom: str) -> dict | None:
    bulk = fetch_bulk([num, denom], period="3mo")
    n_h = bulk.get(num.upper())
    d_h = bulk.get(denom.upper())
    if n_h is None or d_h is None or n_h.empty or d_h.empty or len(n_h) < 22 or len(d_h) < 22:
        return None
    try:
        ratio_now = float(n_h["Close"].iloc[-1] / d_h["Close"].iloc[-1])
        ratio_5d_ago = float(n_h["Close"].iloc[-6] / d_h["Close"].iloc[-6])
        ratio_20d_ago = float(n_h["Close"].iloc[-21] / d_h["Close"].iloc[-21])
        return {
            "ratio": round(ratio_now, 4),
            "5d_chg_pct": round((ratio_now / ratio_5d_ago - 1) * 100, 2),
            "20d_chg_pct": round((ratio_now / ratio_20d_ago - 1) * 100, 2),
        }
    except Exception:
        return None


def fetch_snapshot() -> dict:
    cached = cache_get("breadth_full_v2", ttl_seconds=CACHE_TTL)
    if cached is not None:
        return cached

    out: dict = {}
    out["basket_above_50ma"] = _basket_breadth(BASKET, 50, "breadth_basket_50ma")
    out["basket_above_200ma"] = _basket_breadth(BASKET, 200, "breadth_basket_200ma")
    out["sector_above_50ma"] = _basket_breadth(SECTORS, 50, "breadth_sector_50ma")
    out["sector_above_200ma"] = _basket_breadth(SECTORS, 200, "breadth_sector_200ma")
    out["RSP_SPY"] = _ratio("RSP", "SPY")
    out["IWM_SPY"] = _ratio("IWM", "SPY")
    out["QQQ_SPY"] = _ratio("QQQ", "SPY")

    # Interpretation
    p200 = (out["basket_above_200ma"] or {}).get("pct_above")
    p50 = (out["basket_above_50ma"] or {}).get("pct_above")
    if p200 is not None:
        if p200 < 30:
            out["regime_breadth"] = "WASHOUT — capitulation zone (mean-revert long bias when stable)"
        elif p200 < 50:
            out["regime_breadth"] = "WEAK — most leaders below 200MA"
        elif p200 < 65:
            out["regime_breadth"] = "RECOVERING — improving but not confirmed bull"
        elif p200 < 80:
            out["regime_breadth"] = "HEALTHY BULL — most leaders above 200MA"
        else:
            out["regime_breadth"] = "EUPHORIC — overbought; mean-revert short risk"

    if p50 is not None and p200 is not None:
        gap = p50 - p200
        out["short_long_gap"] = round(gap, 1)
        if gap < -10:
            out["divergence"] = "BEARISH — short-term breadth lagging (selling pressure)"
        elif gap > 10:
            out["divergence"] = "BULLISH — short-term breadth leading"
        else:
            out["divergence"] = "neutral"

    rsp = out.get("RSP_SPY")
    if rsp:
        d20 = rsp.get("20d_chg_pct", 0)
        if d20 < -2:
            out["narrow_rally_warning"] = ("Cap-weighted SPY beating equal-weight RSP by "
                                           f"{abs(d20)}% over 20d — narrow leadership.")

    cache_put("breadth_full_v2", out)
    return out


def cmd_dashboard(snap: dict) -> None:
    print("=== Market Breadth ===\n")
    for key in ("basket_above_50ma", "basket_above_200ma",
                "sector_above_50ma", "sector_above_200ma"):
        d = snap.get(key)
        if d is None:
            print(f"{key:<25}: n/a")
        else:
            print(f"{key:<25}: {d['pct_above']:>5.1f}%  "
                  f"({d['above_ma']}/{d['sampled']} sampled, {d['fetch_failures']} failures)")
    print()
    for key in ("RSP_SPY", "IWM_SPY", "QQQ_SPY"):
        d = snap.get(key)
        if d:
            print(f"{key:<10} ratio={d['ratio']:.4f}  "
                  f"5d={d['5d_chg_pct']:+.2f}%  20d={d['20d_chg_pct']:+.2f}%")
    print()
    if "regime_breadth" in snap:
        print(f"Regime: {snap['regime_breadth']}")
    if "divergence" in snap:
        print(f"Divergence: {snap['divergence']}  (50MA% − 200MA% = {snap.get('short_long_gap', '?')})")
    if "narrow_rally_warning" in snap:
        print(f"[!] {snap['narrow_rally_warning']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    snap = fetch_snapshot()
    flags = []
    p200 = (snap.get("basket_above_200ma") or {}).get("pct_above")
    p50 = (snap.get("basket_above_50ma") or {}).get("pct_above")
    if "narrow_rally_warning" in snap:
        flags.append("breadth_narrow")
    if snap.get("regime_breadth", "").startswith("WASHOUT"):
        flags.append("breadth_washout")
    if snap.get("regime_breadth", "").startswith("EUPHORIC"):
        flags.append("breadth_euphoric")
    if snap.get("divergence", "").startswith("BEARISH"):
        flags.append("breadth_divergence_bearish")
    if snap.get("divergence", "").startswith("BULLISH"):
        flags.append("breadth_divergence_bullish")

    h_parts = []
    if p200 is not None:
        h_parts.append(f"200MA={p200}%")
    if p50 is not None:
        h_parts.append(f"50MA={p50}%")
    rsp = (snap.get("RSP_SPY") or {}).get("20d_chg_pct")
    if rsp is not None:
        h_parts.append(f"RSP/SPY 20d={rsp:+.1f}%")
    headline = "; ".join(h_parts)

    result = step_result("breadth", ok=True, headline=headline, data=snap, flags=flags)
    if args.json:
        emit(result)
    else:
        cmd_dashboard(snap)
        if flags:
            print(f"\nFlags: {flags}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
