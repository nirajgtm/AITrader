#!/usr/bin/env python3
"""Unusual options volume scan.

For each ticker, pulls the nearest 2 expirations and flags strikes where
volume > 2× open interest (and volume ≥ 50 contracts). These are the fresh
positioning prints — the closest free-tier proxy for institutional flow.

Usage:
  flow_scan.py SPY QQQ NVDA AAPL
  flow_scan.py --from-watchlist
  flow_scan.py --majors     # SPY QQQ IWM DIA + mega-caps
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

import watchlist_store

ROOT = Path(__file__).resolve().parent.parent

MAJORS = ["SPY", "QQQ", "IWM", "DIA", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA"]


def watchlist_tickers() -> list[str]:
    return watchlist_store.active_tickers()


def _chain_via_public(ticker: str, n_expirations: int):
    """Pull spot + the nearest N expirations' chains from public.com (real-time).

    Returns (spot, [(exp, calls_df, puts_df), ...], source) where each df uses
    the same column names yfinance gives us (strike, volume, openInterest,
    impliedVolatility, lastPrice) so the scan logic below is source-agnostic.
    Returns (None, [], None) on any failure so the caller falls back to yfinance.
    """
    try:
        import publicdotcom_api as pub
    except Exception:
        return None, [], None
    try:
        quote = pub.get_quote(ticker) or {}
        spot = pub._to_float(quote.get("last")) or pub._to_float(quote.get("previousClose"))
        exps = pub.get_option_expirations(ticker)
        if spot is None or not exps:
            return None, [], None
    except Exception:
        return None, [], None

    def to_df(contracts: list) -> pd.DataFrame:
        recs = []
        for c in contracts:
            p = pub.parse_option_contract(c)
            if not p or p.get("strike") is None:
                continue
            recs.append({
                "strike": p["strike"],
                "volume": p.get("volume") or 0,
                "openInterest": p.get("open_interest") or 0,
                "impliedVolatility": p.get("implied_volatility") or 0.0,
                "lastPrice": p.get("last") or 0.0,
            })
        return pd.DataFrame(recs)

    chains = []
    for exp in exps[:n_expirations]:
        try:
            ch = pub.get_option_chain(ticker, exp)
        except Exception:
            continue
        calls = to_df(ch.get("calls", []))
        puts = to_df(ch.get("puts", []))
        if calls.empty and puts.empty:
            continue
        chains.append((exp, calls, puts))
    if not chains:
        return None, [], None
    return spot, chains, "public.com"


def _chain_via_yfinance(ticker: str, n_expirations: int):
    """Fallback: spot + nearest N expirations' chains from yfinance (delayed).
    Same return contract as _chain_via_public."""
    tk = yf.Ticker(ticker)
    exps = tk.options or []
    if not exps:
        return None, [], None
    try:
        spot = float(tk.history(period="1d")["Close"].iloc[-1])
    except Exception:
        return None, [], None
    chains = []
    for exp in exps[:n_expirations]:
        try:
            ch = tk.option_chain(exp)
        except Exception:
            continue
        chains.append((exp, ch.calls, ch.puts))
    if not chains:
        return None, [], None
    return spot, chains, "yfinance"


def scan(ticker: str, n_expirations: int = 2, vol_min: int = 50) -> pd.DataFrame:
    # public.com first (real-time option volume/OI/IV), yfinance fallback.
    spot, chains, source = _chain_via_public(ticker, n_expirations)
    if spot is None:
        spot, chains, source = _chain_via_yfinance(ticker, n_expirations)
    if spot is None or not chains:
        return pd.DataFrame()

    rows: list[dict] = []
    for exp, calls, puts in chains:
        for side, df in [("C", calls), ("P", puts)]:
            if df is None or df.empty:
                continue
            df = df.copy()
            df["vol"] = df["volume"].fillna(0).astype(int)
            df["oi"] = df["openInterest"].fillna(0).astype(int)
            df = df[(df["vol"] > 2 * df["oi"]) & (df["vol"] >= vol_min)]
            for _, row in df.iterrows():
                rows.append({
                    "Ticker": ticker,
                    "Exp": exp,
                    "Side": side,
                    "Strike": row["strike"],
                    "Spot": round(spot, 2),
                    "m$": round(row["strike"] - spot, 2),
                    "Vol": int(row["vol"]),
                    "OI": int(row["oi"]),
                    "V/OI": round(row["vol"] / max(row["oi"], 1), 1),
                    "IV%": round(float(row.get("impliedVolatility", 0)) * 100, 1),
                    "Last$": round(float(row.get("lastPrice", 0)), 2),
                })
    df_out = pd.DataFrame(rows)
    df_out.attrs["source"] = source  # carried out-of-band; not a visible column
    return df_out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--from-watchlist", action="store_true")
    ap.add_argument("--majors", action="store_true")
    ap.add_argument("--exp", type=int, default=2, help="How many expirations out to scan")
    ap.add_argument("--vol-min", type=int, default=50)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    from _terse import emit, step_result

    tickers: list[str] = [t.upper() for t in args.tickers]
    if args.majors:
        tickers = MAJORS
    elif args.from_watchlist:
        tickers = watchlist_tickers()

    if not tickers:
        print("No tickers. Pass names, --majors, or --from-watchlist.", file=sys.stderr)
        return 1

    all_rows = []
    sources: dict[str, str] = {}
    for t in tickers:
        df = scan(t, args.exp, args.vol_min)
        src = df.attrs.get("source") if hasattr(df, "attrs") else None
        if src:
            sources[t] = src
        if not df.empty:
            all_rows.append(df)

    if not all_rows:
        if args.json:
            emit(step_result("flow", ok=True, headline="no unusual activity",
                             data={"items": [], "scanned": tickers, "sources": sources}))
        else:
            print("No unusual activity detected in scanned universe.")
        return 0
    out = pd.concat(all_rows, ignore_index=True)
    out = out.sort_values(["V/OI", "Vol"], ascending=False).head(20)

    if args.json:
        items = []
        for _, r in out.iterrows():
            items.append({
                "tk": r["Ticker"], "exp": r["Exp"], "side": r["Side"],
                "strike": r["Strike"], "spot": r["Spot"],
                "vol": int(r["Vol"]), "oi": int(r["OI"]),
                "v_oi": float(r["V/OI"]),
            })
        # Surface tickers with significant flow
        by_ticker: dict = {}
        for it in items:
            by_ticker.setdefault(it["tk"], []).append(it)
        flagged = [tk for tk, lst in by_ticker.items() if max(it["v_oi"] for it in lst) >= 5]
        flags = [f"flow_{tk.lower()}" for tk in flagged]
        headline = f"{len(items)} unusual strikes; flagged={','.join(flagged) or 'none'}"
        emit(step_result("flow", ok=True, headline=headline,
                         data={"items": items, "scanned": tickers, "by_ticker": by_ticker,
                               "sources": sources},
                         flags=flags))
        return 0

    print("=== Unusual options activity (volume > 2× OI, vol ≥ min) ===\n")
    print(out.to_string(index=False))
    print(f"\nScanned {len(tickers)} tickers × {args.exp} expirations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
