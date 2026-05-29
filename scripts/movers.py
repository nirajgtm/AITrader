#!/usr/bin/env python3
"""Top movers — gainers, losers, most-active.

Sources (free):
  - yfinance has built-in `Screener` class for Yahoo's predefined screeners
    (most_actives, day_gainers, day_losers).
  - Pre-market data: yfinance includes pre-market via `period="1d", interval="1m"`
    snapshot; we report gap from prior close.

Usage:
  movers.py                       # gainers + losers + most active
  movers.py --gainers
  movers.py --premarket NVDA AAPL ...
  movers.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

import yfinance as yf

from _cache import cache_get, cache_put
from _terse import emit, step_result

CACHE_TTL = 600  # 10 min


def _screener(name: str) -> list[dict] | None:
    """Get top movers using a fallback chain: FMP → Yahoo screener → AlphaVantage.

    `name` is one of: "day_gainers", "day_losers", "most_actives".
    Returns normalized list of dicts.
    """
    cache_key = f"screener_{name}"
    cached = cache_get(cache_key, ttl_seconds=CACHE_TTL)
    if cached is not None:
        return cached

    # Path 1: FMP (clean, free 250/day)
    try:
        from _apikeys import has_key
        if has_key("FMP_API_KEY"):
            from _providers import fmp
            f = fmp()
            data = None
            if name == "day_gainers":
                data = f.biggest_gainers()
            elif name == "day_losers":
                data = f.biggest_losers()
            elif name == "most_actives":
                data = f.most_actives()
            if data:
                out = []
                for q in data:
                    out.append({
                        "symbol": q.get("symbol"),
                        "shortName": q.get("name"),
                        "price": q.get("price"),
                        "change_pct": q.get("changesPercentage"),
                        "volume": q.get("volume") or q.get("avgVolume"),
                        "premarket_pct": None,
                        "premarket_price": None,
                    })
                cache_put(cache_key, out)
                return out
    except Exception:
        pass

    # Path 2: Yahoo screener via yfinance
    try:
        from yfinance import Screener
        s = Screener()
        s.set_predefined_body(name)
        body = s.response
        quotes = body.get("finance", {}).get("result", [{}])[0].get("quotes", [])
        if quotes:
            out = _normalize_yahoo(quotes)
            cache_put(cache_key, out)
            return out
    except Exception:
        pass

    # Path 3: Yahoo direct API
    try:
        import requests
        url = f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?count=25&scrIds={name}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        data = r.json()
        quotes = data.get("finance", {}).get("result", [{}])[0].get("quotes", [])
        if quotes:
            out = _normalize_yahoo(quotes)
            cache_put(cache_key, out)
            return out
    except Exception:
        pass

    # Path 4: AlphaVantage (last resort — burns daily budget)
    try:
        from _apikeys import has_key
        if has_key("ALPHAVANTAGE_API_KEY"):
            from _providers import alphavantage
            av = alphavantage()
            data = av.top_movers()
            if isinstance(data, dict):
                key = {"day_gainers": "top_gainers",
                       "day_losers": "top_losers",
                       "most_actives": "most_actively_traded"}.get(name)
                items = data.get(key, []) or []
                out = []
                for q in items:
                    out.append({
                        "symbol": q.get("ticker"),
                        "shortName": None,
                        "price": float(q.get("price", 0)) if q.get("price") else None,
                        "change_pct": float(q.get("change_percentage", "0").rstrip("%"))
                                       if q.get("change_percentage") else None,
                        "volume": int(q.get("volume", 0)) if q.get("volume") else None,
                        "premarket_pct": None,
                        "premarket_price": None,
                    })
                cache_put(cache_key, out)
                return out
    except Exception:
        pass

    return None


def _normalize_yahoo(quotes: list[dict]) -> list[dict]:
    out = []
    for q in quotes:
        out.append({
            "symbol": q.get("symbol"),
            "shortName": q.get("shortName") or q.get("longName"),
            "price": q.get("regularMarketPrice"),
            "change_pct": q.get("regularMarketChangePercent"),
            "volume": q.get("regularMarketVolume"),
            "premarket_pct": q.get("preMarketChangePercent"),
            "premarket_price": q.get("preMarketPrice"),
        })
    return out


def premarket(tickers: list[str]) -> list[dict]:
    out = []
    for t in tickers:
        try:
            tk = yf.Ticker(t)
            info = getattr(tk, "fast_info", None) or {}
            prev = info.get("previousClose") or info.get("previous_close")
            pre = info.get("preMarketPrice") or info.get("regularMarketPreviousClose")
            # yfinance fast_info doesn't always include premarket; pull from quote
            quote = tk.history(period="2d", prepost=True, interval="1m")
            if quote.empty:
                continue
            last = float(quote["Close"].iloc[-1])
            if prev is None and len(quote) > 1:
                # prior close = last regular-session close
                prev = float(quote["Close"].iloc[0])
            chg_pct = (last / float(prev) - 1) * 100 if prev else None
            out.append({
                "ticker": t.upper(),
                "premarket_last": round(last, 2),
                "prev_close": round(float(prev), 2) if prev else None,
                "gap_pct": round(chg_pct, 2) if chg_pct is not None else None,
            })
        except Exception as e:
            out.append({"ticker": t.upper(), "error": str(e)[:80]})
    return out


def _qualify(items: list[dict]) -> list[dict]:
    """Quality filter for mover output.

    Broadened from index-only to a liquid-equity gate so non-index movers
    surface too (the old gate silently dropped any name outside the curated
    S&P/Nasdaq/retail set). Keeps only:
      - price >= $5 for curated names, >= $10 for uncurated (crude liquidity
        proxy; the mind verifies market cap via a single quote at deep-dive)
      - abs(change_pct) between 2 and 25 (drops saturation moves and noise)
    Tags each surviving item ``in_universe`` so the mind knows which names are
    known-quality vs need a liquidity check.
    """
    from _universe import is_in_universe
    out = []
    for it in items:
        sym = (it.get("symbol") or "").upper()
        if not sym:
            continue
        px = it.get("price")
        if px is None or px < 5:
            continue
        chg = it.get("change_pct")
        if chg is None:
            continue
        try:
            achg = abs(float(chg))
        except Exception:
            continue
        if not (2.0 <= achg <= 25.0):
            continue
        in_u = is_in_universe(sym)
        if not in_u and px < 10:
            continue
        it["in_universe"] = in_u
        out.append(it)
    return out


def _fetch_headline(ticker: str) -> str | None:
    """Single most recent news headline within 24h, via news.fetch_news. None if nothing found."""
    try:
        from news import fetch_news, _age_hours
    except Exception:
        return None
    try:
        items = fetch_news(ticker, max_items=5)
    except Exception:
        return None
    for it in items:
        age = _age_hours(it.get("published", ""))
        if age is not None and age <= 24:
            t = (it.get("title") or "").strip()
            if t:
                return t[:120]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gainers", action="store_true")
    ap.add_argument("--losers", action="store_true")
    ap.add_argument("--actives", action="store_true")
    ap.add_argument("--premarket", nargs="*", help="Tickers for premarket gap scan")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-filter", action="store_true",
                    help="Disable qualification filter (return raw screener output)")
    args = ap.parse_args()

    out: dict = {}

    if args.premarket:
        out["premarket"] = premarket(args.premarket)

    do_all = not any([args.gainers, args.losers, args.actives, args.premarket])
    if args.gainers or do_all:
        raw = _screener("day_gainers") or []
        out["gainers"] = raw if args.no_filter else _qualify(raw)
    if args.losers or do_all:
        raw = _screener("day_losers") or []
        out["losers"] = raw if args.no_filter else _qualify(raw)
    if args.actives or do_all:
        raw = _screener("most_actives") or []
        out["most_actives"] = raw if args.no_filter else _qualify(raw)

    if args.json:
        # Trim to actionable: top 8 each, only the fields runbook needs
        compact = {}
        for k in ("gainers", "losers", "most_actives"):
            items = out.get(k) or []
            compact[k] = [
                {"sym": it.get("symbol"), "px": it.get("price"),
                 "pct": round(it.get("change_pct", 0) or 0, 2),
                 "vol": it.get("volume")}
                for it in items[:8]
            ]
        if "premarket" in out:
            compact["premarket"] = out["premarket"]
        for bucket in ("gainers", "losers"):
            items = compact.get(bucket, [])
            for it in items[:5]:
                h = _fetch_headline(it["sym"])
                if h:
                    it["top_headline"] = h
        result = step_result(
            "movers", ok=True,
            headline=("gainers={}; losers={}".format(
                ",".join(it["sym"] for it in compact.get("gainers", [])[:5]),
                ",".join(it["sym"] for it in compact.get("losers", [])[:5]))),
            data=compact,
        )
        emit(result)
        return 0

    for label, items in out.items():
        print(f"\n=== {label.upper()} ===")
        if items is None:
            print("  (fetch failed)")
            continue
        if not items:
            print("  (none)")
            continue
        for it in items[:15]:
            if label == "premarket":
                print(f"  {it.get('ticker'):<6}  last={it.get('premarket_last')}  "
                      f"gap={it.get('gap_pct')}%")
            else:
                sym = it.get("symbol", "?")
                pct = it.get("change_pct", 0) or 0
                px = it.get("price", "?")
                vol = it.get("volume", 0) or 0
                pre = it.get("premarket_pct")
                pre_s = f"  pre={pre:+.2f}%" if pre else ""
                print(f"  {sym:<6} ${px}  {pct:+.2f}%  vol={vol:,}{pre_s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
