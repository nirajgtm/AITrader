#!/usr/bin/env python3
"""Option-chain snapshot with IV, OI, volume, unusual-volume, and IV-rank.

Usage:
  options.py TICKER                       # list expirations
  options.py TICKER --exp YYYY-MM-DD      # chain for that expiry
  options.py TICKER --exp YYYY-MM-DD --side calls
  options.py TICKER --exp YYYY-MM-DD --near 10
  options.py TICKER --iv-rank             # 1y IV percentile (uses ATM front-month proxy)
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from _cache import cache_get, cache_put

pd.set_option("display.max_rows", 200)
pd.set_option("display.width", 200)

# Column names every chain DataFrame uses, regardless of source. Matches the
# yfinance schema so the rendering/IV logic below is source-agnostic.
_CHAIN_COLS = ["strike", "lastPrice", "bid", "ask", "volume", "openInterest",
               "impliedVolatility"]


def _public_expirations(ticker: str) -> list[str] | None:
    """Listed expirations from public.com, or None on failure."""
    try:
        import publicdotcom_api as pub
        exps = pub.get_option_expirations(ticker)
        return exps or None
    except Exception:
        return None


def _public_chain_df(ticker: str, expiration: str):
    """Return (calls_df, puts_df) from public.com with the yfinance column
    schema, or (None, None) on failure."""
    try:
        import publicdotcom_api as pub
        chain = pub.get_option_chain(ticker, expiration)
    except Exception:
        return None, None

    def to_df(contracts: list) -> pd.DataFrame:
        recs = []
        for c in contracts:
            p = pub.parse_option_contract(c)
            if not p or p.get("strike") is None:
                continue
            recs.append({
                "strike": p["strike"],
                "lastPrice": p.get("last") or 0.0,
                "bid": p.get("bid") or 0.0,
                "ask": p.get("ask") or 0.0,
                "volume": p.get("volume") or 0,
                "openInterest": p.get("open_interest") or 0,
                "impliedVolatility": p.get("implied_volatility") or 0.0,
            })
        return pd.DataFrame(recs, columns=_CHAIN_COLS)

    calls, puts = to_df(chain.get("calls", [])), to_df(chain.get("puts", []))
    if calls.empty and puts.empty:
        return None, None
    return calls, puts


def _atm_front_iv(tk: yf.Ticker, spot: float, symbol: str | None = None) -> tuple[float | None, str | None]:
    """ATM call IV from the nearest expiry. public.com first, yfinance fallback.
    Returns (iv_fraction, source) where source is 'public.com' / 'yfinance' /
    None.

    public.com does not compute greeks for 0-1 DTE weeklies, so when sourcing
    from it we walk forward to the first expiry whose chain carries IV."""
    if symbol:
        exps = _public_expirations(symbol)
        for e in (exps or [])[:6]:
            calls, _ = _public_chain_df(symbol, e)
            if calls is None or calls.empty:
                continue
            c = calls[calls["impliedVolatility"] > 0].copy()
            if c.empty:
                continue
            c["d"] = (c["strike"] - spot).abs()
            iv = float(c.nsmallest(1, "d").iloc[0]["impliedVolatility"])
            if iv > 0:
                return iv, "public.com"
    exps = tk.options or []
    if not exps:
        return None, None
    try:
        chain = tk.option_chain(exps[0])
    except Exception:
        return None, None
    calls = chain.calls
    if calls.empty:
        return None, None
    calls = calls.copy()
    calls["d"] = (calls["strike"] - spot).abs()
    row = calls.nsmallest(1, "d").iloc[0]
    iv = float(row.get("impliedVolatility", 0))
    return (iv, "yfinance") if iv > 0 else (None, None)


def cmd_iv_rank(ticker: str) -> int:
    """Compute a rough 1y IV-rank using realized vol as a proxy.

    True IV-rank requires a 1y history of ATM IV which yfinance does not store.
    Workaround: use 1y of close prices' rolling 30-day realized vol as a proxy
    distribution; report current ATM IV's percentile within that distribution.

    Caveat: this conflates IV with RV. On the free-tier setup it is a directional
    indicator, not a tradable IV-rank. Premium-data path: Polygon options or
    Tradier free-tier, both recommended once budget allows.
    """
    cache_key = f"iv_rank_{ticker.upper()}"
    cached = cache_get(cache_key, ttl_seconds=4 * 3600)
    if cached is not None:
        print(json.dumps(cached, indent=2))
        return 0

    tk = yf.Ticker(ticker)
    hist = tk.history(period="1y", auto_adjust=False)
    if hist.empty or len(hist) < 60:
        print(f"Not enough history for {ticker}")
        return 1

    spot = float(hist["Close"].iloc[-1])
    log_ret = pd.Series(hist["Close"].pct_change().dropna().tolist())
    rolling_rv = log_ret.rolling(window=30).std() * math.sqrt(252)
    rolling_rv = rolling_rv.dropna()
    if rolling_rv.empty:
        print("rolling RV empty")
        return 1

    rv_now = float(rolling_rv.iloc[-1])
    iv_now, iv_source = _atm_front_iv(tk, spot, symbol=ticker)

    # Percentile of current IV within the 1y RV distribution
    if iv_now is not None:
        rank_iv = float((rolling_rv < iv_now).mean() * 100)
    else:
        rank_iv = None
    rank_rv = float((rolling_rv < rv_now).mean() * 100)

    out = {
        "ticker": ticker.upper(),
        "spot": round(spot, 2),
        "atm_front_iv": round(iv_now, 4) if iv_now is not None else None,
        "iv_source": iv_source,
        "rv_30d_now": round(rv_now, 4),
        "iv_rank_pct_vs_1y_rv": round(rank_iv, 1) if rank_iv is not None else None,
        "rv_rank_pct_vs_1y_rv": round(rank_rv, 1),
        "interpretation": _interpret_rank(rank_iv if rank_iv is not None else rank_rv),
        "_caveat": "Proxy: IV-rank computed against 1y realized-vol distribution. "
                   "For true IV-rank, upgrade to Polygon options or Tradier.",
    }
    cache_put(cache_key, out)
    import json as _json
    print(_json.dumps(out, indent=2))
    return 0


def _interpret_rank(rank: float | None) -> str:
    if rank is None:
        return "unknown"
    if rank < 30:
        return "LOW IV — long premium cheap; favor long calls/puts/calendars."
    if rank > 70:
        return "HIGH IV — premium expensive; favor debit spreads / short premium / vol-crush."
    return "MID IV — no IV-driven edge; pick vehicle on directional thesis."


def _spot_price(ticker: str, tk: yf.Ticker) -> float | None:
    """Real-time spot from public.com, falling back to yfinance daily close."""
    try:
        import publicdotcom_api as pub
        q = pub.get_quote(ticker) or {}
        s = pub._to_float(q.get("last")) or pub._to_float(q.get("previousClose"))
        if s:
            return s
    except Exception:
        pass
    try:
        return float(tk.history(period="1d")["Close"].iloc[-1])
    except Exception:
        return None


def cmd_chain(args: argparse.Namespace) -> int:
    t = args.ticker.upper()
    tk = yf.Ticker(t)

    # Expirations: public.com first, yfinance fallback.
    source = "public.com"
    expirations = _public_expirations(t)
    if not expirations:
        source = "yfinance"
        expirations = tk.options
    if not expirations:
        print(f"No options data for {t}", file=sys.stderr)
        return 1

    if not args.exp:
        print(f"{t} available expirations: [source: {source}]")
        for e in expirations:
            print(f"  {e}")
        return 0

    if args.exp not in expirations:
        print(f"Expiry {args.exp} not available. Options: {expirations}", file=sys.stderr)
        return 1

    # Chain: public.com first, yfinance fallback. Track which source served it.
    calls_df, puts_df = _public_chain_df(t, args.exp)
    chain_source = "public.com"
    if calls_df is None:
        chain_source = "yfinance"
        try:
            ch = tk.option_chain(args.exp)
            calls_df, puts_df = ch.calls, ch.puts
        except Exception:
            print(f"No chain for {t} {args.exp}", file=sys.stderr)
            return 1

    spot = _spot_price(t, tk)
    if spot is None:
        print(f"No spot price for {t}", file=sys.stderr)
        return 1

    print(f"=== {t} @ ${spot:.2f} | exp {args.exp} ===  [source: {chain_source}]\n")

    def fmt(df: pd.DataFrame, label: str) -> None:
        if df is None or df.empty:
            return
        df = df.copy()
        df["moneyness"] = (df["strike"] - spot).round(2)
        df["_dist"] = (df["strike"] - spot).abs()
        df = df.nsmallest(args.near * 2, "_dist").sort_values("strike").drop(columns="_dist")

        df["unusual"] = (df["volume"].fillna(0) > 2 * df["openInterest"].fillna(0)) & \
                        (df["volume"].fillna(0) > 50)
        df["spread"] = (df["ask"] - df["bid"]).round(3)
        df["spread_pct"] = ((df["ask"] - df["bid"]) / ((df["ask"] + df["bid"]) / 2 + 1e-9) * 100).round(1)
        keep = [
            "strike", "lastPrice", "bid", "ask", "spread", "spread_pct",
            "volume", "openInterest", "impliedVolatility", "moneyness", "unusual"
        ]
        view = df[keep].copy()
        view["impliedVolatility"] = (view["impliedVolatility"] * 100).round(1)
        view = view.rename(columns={"impliedVolatility": "IV%", "moneyness": "m$",
                                    "spread_pct": "spr%"})
        view["volume"] = view["volume"].fillna(0).astype(int)
        view["openInterest"] = view["openInterest"].fillna(0).astype(int)
        print(f"-- {label} --")
        print(view.to_string(index=False))
        print()

    if args.side in ("calls", "both"):
        fmt(calls_df, "CALLS")
    if args.side in ("puts", "both"):
        fmt(puts_df, "PUTS")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--exp", help="Expiration date YYYY-MM-DD")
    ap.add_argument("--side", choices=["calls", "puts", "both"], default="both")
    ap.add_argument("--near", type=int, default=10, help="Strikes +/- N around spot")
    ap.add_argument("--iv-rank", action="store_true",
                    help="Print IV rank (proxy via 1y RV distribution)")
    args = ap.parse_args()

    if args.iv_rank:
        return cmd_iv_rank(args.ticker)
    return cmd_chain(args)


if __name__ == "__main__":
    import json  # used by cmd_iv_rank
    sys.exit(main())
