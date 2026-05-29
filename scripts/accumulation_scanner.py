#!/usr/bin/env python3
"""accumulation_scanner.py — early-stage accumulation signal.

Identifies tickers showing insider accumulation while NOT yet extended.

Filter (technical health):
  - RSI14 < 60 (not overbought)
  - Price within 10% of 50DMA (not extended either way)

Signal (smart-money accumulation):
  - Finnhub Form-4 insider buys (transactionCode='P') over 60d window
  - Distinct buyer count (diversity > one-off)
  - Net buy value vs sell value

Score (0-100): weighted combination of insider strength, technical health,
and trend alignment (above 200MA bonus).

Why this scanner exists: the system's existing scanners (movers, breakouts,
PEAD, social) fire AFTER price has moved. By that point the FOMO ceiling
rule blocks new entries. This scanner catches names BEFORE they break out,
when accumulation is visible but the chart hasn't ripped — so the breakout
trade is takeable when it eventually fires.

Cold pull: ~5-10 min on the 550-ticker stock universe. Finnhub provider has
12h cache so warm calls are instant.

Usage:
  accumulation_scanner.py [--top N] [--days 60] [--max-rsi 60] [--limit N] [--json]
"""
from __future__ import annotations

import argparse
import sys
import warnings
from datetime import date

import numpy as np
import pandas as pd
import yfinance as yf

from _providers import finnhub
from _terse import emit, step_result
from _universe import (
    COMMODITIES_ETFS,
    INDEX_ETFS,
    LEVERAGED_INVERSE,
    SECTOR_ETFS,
    get_universe,
)
from price import rsi


def _filter_universe(u: set[str]) -> list[str]:
    """Drop ETFs from the universe — they have no Form 4 insider data."""
    drop = SECTOR_ETFS | INDEX_ETFS | COMMODITIES_ETFS | LEVERAGED_INVERSE
    return sorted(u - drop)


def _fetch_prices(tickers: list[str]) -> dict[str, dict]:
    """Batch yfinance pull of 6mo daily history. Returns per-ticker indicators."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = yf.download(
            " ".join(tickers),
            period="1y",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
    out: dict[str, dict] = {}
    for tk in tickers:
        try:
            sub = df[tk] if isinstance(df.columns, pd.MultiIndex) else df
            close = sub["Close"].dropna()
            if len(close) < 60:
                continue
            ma50 = float(close.rolling(50).mean().iloc[-1])
            ma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
            r = float(rsi(close).iloc[-1])
            last = float(close.iloc[-1])
            if np.isnan(r) or np.isnan(ma50):
                continue
            dist_50 = (last - ma50) / ma50 * 100
            out[tk] = {
                "close": round(last, 2),
                "rsi14": round(r, 1),
                "ma50": round(ma50, 2),
                "ma200": round(ma200, 2) if ma200 is not None and not np.isnan(ma200) else None,
                "dist_50ma_pct": round(dist_50, 2),
            }
        except Exception:
            continue
    return out


def _aggregate_insiders(ticker: str, fh, days: int) -> dict | None:
    """Pull Form-4 transactions for ticker over `days` window, aggregate.

    Returns None on no data. Otherwise returns counts, dollar values, and
    distinct buyer list.
    """
    data = fh.insider_transactions(ticker, days=days)
    if data is None:
        return None
    items = data.get("data", []) if isinstance(data, dict) else (data or [])
    if not items:
        return None
    buys = [i for i in items if i.get("transactionCode") == "P"]
    sells = [i for i in items if i.get("transactionCode") == "S"]
    buy_value = sum(
        (i.get("share") or 0) * (i.get("transactionPrice") or 0) for i in buys
    )
    sell_value = sum(
        (i.get("share") or 0) * (i.get("transactionPrice") or 0) for i in sells
    )
    distinct_buyers = sorted({(i.get("name") or "").strip() for i in buys if i.get("name")})
    return {
        "ticker": ticker,
        "buys": len(buys),
        "sells": len(sells),
        "buy_value": round(buy_value, 0),
        "sell_value": round(sell_value, 0),
        "net_value": round(buy_value - sell_value, 0),
        "distinct_buyers": distinct_buyers[:5],
        "n_distinct_buyers": len(distinct_buyers),
    }


def _score(insider: dict, price: dict) -> float:
    """Composite 0-100 score. Higher = stronger accumulation in non-extended setup.

    Components:
      - Insider buy count (max 20)
      - Distinct-buyer diversity (max 10)
      - Buy/sell ratio strength (max 20)
      - RSI coldness (max 15)
      - Proximity to 50MA (max 10)
      - Above 200MA trend alignment (max 5)
    """
    s = 0.0
    s += min(20, insider["buys"] * 4)
    s += min(10, insider["n_distinct_buyers"] * 3)
    if insider["sell_value"] == 0 and insider["buy_value"] > 0:
        s += 20
    elif insider["buy_value"] > insider["sell_value"]:
        ratio = insider["buy_value"] / max(1.0, insider["sell_value"])
        s += min(20, ratio * 5)
    elif insider["buy_value"] > 0:
        s += 5
    rsi_v = price["rsi14"]
    if rsi_v < 50:
        s += 15
    elif rsi_v < 60:
        s += 10
    dist = abs(price["dist_50ma_pct"])
    if dist < 5:
        s += 10
    elif dist < 10:
        s += 5
    if price.get("ma200") and price["close"] > price["ma200"]:
        s += 5
    return round(min(100, s), 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="Insider lookback window")
    ap.add_argument("--top", type=int, default=15, help="Top N candidates to surface")
    ap.add_argument("--min-buys", type=int, default=1, help="Minimum buy count")
    ap.add_argument("--max-rsi", type=float, default=60, help="RSI ceiling")
    ap.add_argument("--max-dist-50ma", type=float, default=10,
                    help="Max abs %% distance from 50DMA")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap universe size (testing)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    universe = _filter_universe(get_universe())
    if args.limit:
        universe = universe[: args.limit]

    print(f"[accum] pulling prices for {len(universe)} tickers...", file=sys.stderr)
    prices = _fetch_prices(universe)
    print(f"[accum] got prices for {len(prices)} tickers", file=sys.stderr)

    candidates = [
        tk for tk, px in prices.items()
        if px["rsi14"] < args.max_rsi
        and abs(px["dist_50ma_pct"]) < args.max_dist_50ma
    ]
    print(
        f"[accum] {len(candidates)} pass technical filter "
        f"(RSI<{args.max_rsi}, |dist 50MA|<{args.max_dist_50ma}%)",
        file=sys.stderr,
    )

    fh = finnhub()
    rows: list[dict] = []
    for i, tk in enumerate(candidates, 1):
        if i % 25 == 0:
            print(f"[accum] insider {i}/{len(candidates)}...", file=sys.stderr)
        ins = _aggregate_insiders(tk, fh, args.days)
        if ins is None or ins["buys"] < args.min_buys:
            continue
        ins["price"] = prices[tk]
        ins["score"] = _score(ins, prices[tk])
        rows.append(ins)

    rows.sort(key=lambda r: -r["score"])
    top = rows[: args.top]

    headline = (
        f"{len(top)} accumulation candidates "
        f"(top: {','.join(r['ticker'] for r in top[:5])})"
        if top else "no accumulation candidates"
    )

    flags: list[str] = []
    if len(top) >= 5:
        flags.append("accumulation_cluster_strong")

    if args.json:
        emit(step_result(
            "accumulation", ok=True, headline=headline,
            data={
                "candidates": top,
                "scanned_technically": len(candidates),
                "universe_size": len(universe),
                "filters": {
                    "max_rsi": args.max_rsi,
                    "max_dist_50ma_pct": args.max_dist_50ma,
                    "min_buys": args.min_buys,
                    "days": args.days,
                },
            },
            flags=flags,
            actions=[],
        ))
        return 0

    print(f"=== Accumulation candidates ({date.today()}) ===")
    if not top:
        print("(none)")
        return 0
    for r in top:
        px = r["price"]
        print(
            f"  {r['ticker']:<6} score={r['score']:>5.1f} "
            f"buys={r['buys']:>2} sells={r['sells']:>2} "
            f"net=${r['net_value']:>14,.0f}  "
            f"close=${px['close']:.2f} RSI={px['rsi14']} "
            f"50MA=${px['ma50']:.2f} (dist {px['dist_50ma_pct']:+.1f}%)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
