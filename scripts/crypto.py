#!/usr/bin/env python3
"""Crypto coverage — full RH-tradable list, BTC dominance, trending.

Replaces the BTC-only step in runbook. Surfaces:
  - Per-coin price/RSI/ATR for the RH-tradable list
  - BTC dominance (% of total crypto market cap held by BTC)
  - CoinGecko trending — top searched coins of the past 24h

Sources:
  - yfinance for OHLCV per coin (cached 10min via _market.py bulk)
  - CoinGecko /global for BTC dominance (free, no auth)
  - CoinGecko /search/trending for trending names (free, no auth)
  - Optional: COINGECKO_DEMO_API_KEY for higher rate limits

Usage:
  crypto.py
  crypto.py --json
  crypto.py --trending-only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from _cache import cache_get, cache_put  # noqa: E402
from _market import fetch_bulk, with_indicators, ret_pct  # noqa: E402
from _terse import emit, step_result  # noqa: E402
from _apikeys import get_key  # noqa: E402

# RH-tradable list (as of 2026; verify periodically). Some Yahoo symbols
# differ from the underlying ticker after rebrands (MATIC→POL, etc.).
RH_TRADABLE = [
    "BTC", "ETH", "SOL", "XRP", "DOGE", "AVAX", "LINK", "POL",  # POL = MATIC rebrand
    "LTC", "BCH", "ETC", "XLM", "UNI7083", "AAVE", "COMP5692", "SHIB",
]

# Display name overrides (so we show MATIC even if Yahoo uses POL)
DISPLAY_OVERRIDES = {"POL": "MATIC", "UNI7083": "UNI", "COMP5692": "COMP"}


def _yf_symbol(coin: str) -> str:
    return f"{coin.upper()}-USD"


def _display_name(coin: str) -> str:
    return DISPLAY_OVERRIDES.get(coin.upper(), coin.upper())


CG_BASE = "https://api.coingecko.com/api/v3"
CACHE_TTL_TREND = 1800  # 30 min
CACHE_TTL_GLOBAL = 600  # 10 min


def _cg_get(path: str, params: dict | None = None) -> Optional[dict]:
    headers = {"User-Agent": "trader-skill/1.2"}
    key = get_key("COINGECKO_DEMO_API_KEY")
    if key:
        headers["x-cg-demo-api-key"] = key
    try:
        r = requests.get(f"{CG_BASE}{path}", params=params or {},
                         headers=headers, timeout=15)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def fetch_trending() -> Optional[list[dict]]:
    cached = cache_get("cg_trending", ttl_seconds=CACHE_TTL_TREND)
    if cached is not None:
        return cached
    data = _cg_get("/search/trending")
    if not data:
        return None
    coins = data.get("coins") or []
    out = []
    for c in coins[:10]:
        item = c.get("item") or {}
        d = item.get("data") or {}
        out.append({
            "symbol": item.get("symbol", "").upper(),
            "name": item.get("name"),
            "rank": item.get("market_cap_rank"),
            "price_usd": d.get("price"),
            "price_btc": item.get("price_btc"),
            "chg_24h_pct": (d.get("price_change_percentage_24h") or {}).get("usd"),
        })
    cache_put("cg_trending", out)
    return out


def fetch_global() -> Optional[dict]:
    cached = cache_get("cg_global", ttl_seconds=CACHE_TTL_GLOBAL)
    if cached is not None:
        return cached
    data = _cg_get("/global")
    if not data:
        return None
    d = data.get("data") or {}
    mc = d.get("market_cap_percentage") or {}
    out = {
        "btc_dominance_pct": round(float(mc.get("btc", 0)), 2),
        "eth_dominance_pct": round(float(mc.get("eth", 0)), 2),
        "total_market_cap_usd": d.get("total_market_cap", {}).get("usd"),
        "total_volume_24h_usd": d.get("total_volume", {}).get("usd"),
        "active_cryptocurrencies": d.get("active_cryptocurrencies"),
    }
    cache_put("cg_global", out)
    return out


def per_coin_metrics() -> list[dict]:
    """Bulk-fetch OHLCV for all RH-tradable coins; compute close, %, RSI, ATR."""
    syms = [_yf_symbol(c) for c in RH_TRADABLE]
    bulk = fetch_bulk(syms, period="1y")
    out: list[dict] = []
    for c in RH_TRADABLE:
        df = bulk.get(_yf_symbol(c))
        if df is None or df.empty:
            out.append({"coin": _display_name(c), "data_gap": True})
            continue
        df = with_indicators(df)
        last = df.iloc[-1]
        out.append({
            "coin": _display_name(c),
            "close": round(float(last["Close"]), 2),
            "chg5d_pct": round(ret_pct(df, 5), 2),
            "chg24h_pct": round(ret_pct(df, 1), 2),
            "rsi14": round(float(last["RSI14"]) if not _isnan(last["RSI14"]) else 0, 1),
            "vs_ma20": "above" if last["Close"] > last["MA20"] else "below",
            "vs_ma200": "above" if (not _isnan(last["MA200"]) and last["Close"] > last["MA200"]) else "below",
        })
    return out


def _isnan(x) -> bool:
    try:
        import math
        return math.isnan(x)
    except Exception:
        return False


def holdings_crypto_metrics() -> list[dict]:
    """Per-position technicals for user-held crypto. Reads portfolio.json directly."""
    from _common import load_portfolio
    try:
        p = load_portfolio("primary")
    except Exception:
        return []
    held = [pos for pos in (p.get("user_positions") or []) if pos.get("kind") == "crypto"]
    if not held:
        return []
    syms = list({_yf_symbol(pos.get("tk") or pos.get("ticker")) for pos in held})
    bulk = fetch_bulk(syms, period="1y")
    out = []
    for pos in held:
        tk = pos.get("tk") or pos.get("ticker")
        df = bulk.get(_yf_symbol(tk))
        if df is None or df.empty:
            out.append({"coin": _display_name(tk), "data_gap": True,
                        "qty": pos.get("qty"), "entry": pos.get("entry")})
            continue
        df = with_indicators(df)
        last = df.iloc[-1]
        close = float(last["Close"])
        hi_52w = float(df["Close"].max())
        pct_from_high = round((close - hi_52w) / hi_52w * 100, 2)
        entry = float(pos.get("entry") or 0)
        pnl_pct = round((close - entry) / entry * 100, 2) if entry else None
        out.append({
            "coin": _display_name(tk),
            "qty": pos.get("qty"),
            "entry": entry,
            "close": round(close, 2),
            "pnl_pct": pnl_pct,
            "chg5d_pct": round(ret_pct(df, 5), 2),
            "chg24h_pct": round(ret_pct(df, 1), 2),
            "rsi14": round(float(last["RSI14"]) if not _isnan(last["RSI14"]) else 0, 1),
            "vs_ma20": "above" if close > last["MA20"] else "below",
            "vs_ma200": "above" if (not _isnan(last["MA200"]) and close > last["MA200"]) else "below",
            "pct_from_52w_high": pct_from_high,
            "high_52w": round(hi_52w, 2),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--trending-only", action="store_true")
    ap.add_argument("--include-holdings", action="store_true")
    args = ap.parse_args()

    g = fetch_global()
    trend = fetch_trending() or []
    coins = [] if args.trending_only else per_coin_metrics()
    holdings = holdings_crypto_metrics() if args.include_holdings else []

    flags: list[str] = []
    btc_dom = (g or {}).get("btc_dominance_pct")
    if btc_dom is not None and btc_dom < 50:
        flags.append("altseason_signal")
    # Coins with extreme RSI
    for c in coins:
        rsi = c.get("rsi14")
        if rsi and rsi >= 80:
            flags.append(f"rsi_extreme_{c['coin'].lower()}")
        elif rsi and rsi <= 25:
            flags.append(f"rsi_oversold_{c['coin'].lower()}")
    for h in holdings:
        if h.get("data_gap"):
            continue
        rsi = h.get("rsi14")
        if rsi and rsi >= 80:
            flags.append(f"holding_extreme_{h['coin'].lower()}")
        if h.get("pct_from_52w_high") is not None and h["pct_from_52w_high"] >= -5:
            flags.append(f"holding_at_ath_{h['coin'].lower()}")
    # Trending coins that are RH-tradable = high-priority
    rh_set = {_display_name(c) for c in RH_TRADABLE}
    rh_trending = [t["symbol"] for t in trend if t["symbol"] in rh_set]
    if rh_trending:
        flags.append("rh_crypto_trending")

    headline_parts = []
    if g:
        headline_parts.append(f"BTC.D={btc_dom}%")
    btc_row = next((c for c in coins if c["coin"] == "BTC"), None)
    if btc_row:
        headline_parts.append(f"BTC={btc_row.get('close')} ({btc_row.get('chg24h_pct'):+.1f}%)")
    if rh_trending:
        headline_parts.append(f"trending(RH)={','.join(rh_trending)}")
    if len(holdings) > 0:
        headline_parts.append(f"holdings={len(holdings)}")
    headline = "; ".join(headline_parts)

    data = {
        "global": g,
        "trending": trend[:7],
        "rh_tradable": coins,
        "rh_trending_overlap": rh_trending,
        "holdings_crypto": holdings,
    }
    result = step_result("crypto", ok=True, headline=headline, data=data, flags=flags)
    if args.json:
        emit(result)
    else:
        if g:
            print(f"BTC.D={g.get('btc_dominance_pct')}%  ETH.D={g.get('eth_dominance_pct')}%  "
                  f"Total MC=${g.get('total_market_cap_usd', 0)/1e12:.2f}T")
        print("\n=== Trending (CoinGecko, 24h) ===")
        for t in trend[:7]:
            tag = "[RH]" if t["symbol"] in rh_set else ""
            print(f"  {t['symbol']:<6} {t.get('name','?'):<25} rank={t.get('rank')} "
                  f"24h={t.get('chg_24h_pct')} {tag}")
        if not args.trending_only:
            print("\n=== RH-tradable (per-coin) ===")
            for c in coins:
                if c.get("data_gap"):
                    print(f"  {c['coin']:<6} (data gap)")
                    continue
                print(f"  {c['coin']:<6} ${c.get('close'):>10}  24h={c.get('chg24h_pct'):+.2f}%  "
                      f"5d={c.get('chg5d_pct'):+.2f}%  RSI={c.get('rsi14')}  "
                      f"{c.get('vs_ma20')}20MA  {c.get('vs_ma200')}200MA")
        if args.include_holdings and holdings:
            print("\n=== Holdings ===")
            for h in holdings:
                if h.get("data_gap"):
                    print(f"  {h['coin']:<6} (data gap)  qty={h.get('qty')} entry={h.get('entry')}")
                    continue
                pnl = h.get("pnl_pct")
                pnl_str = f"{pnl:+.2f}%" if pnl is not None else "n/a"
                print(f"  {h['coin']:<6} ${h.get('close'):>10}  qty={h.get('qty')}  entry=${h.get('entry')}  "
                      f"PnL={pnl_str}  24h={h.get('chg24h_pct'):+.2f}%  5d={h.get('chg5d_pct'):+.2f}%  "
                      f"RSI={h.get('rsi14')}  {h.get('vs_ma20')}20MA  {h.get('vs_ma200')}200MA  "
                      f"from52wH={h.get('pct_from_52w_high')}%")
        if flags:
            print(f"\nFlags: {flags}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
