#!/usr/bin/env python3
"""News fetcher.

PRIMARY: Finnhub /company-news (richer; includes datetime, summary, source).
FALLBACK: yfinance .news (used when FINNHUB_API_KEY absent).

Both cached 1h.

Usage:
  news.py NVDA AAPL
  news.py --from-watchlist
  news.py --majors
  news.py NVDA --hours 6
  news.py NVDA --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

from _cache import cache_get, cache_put
from _terse import emit, step_result
from _apikeys import has_key
import watchlist_store

ROOT = Path(__file__).resolve().parent.parent
CACHE_TTL = 3600  # 1 hour

MAJORS = ["SPY", "QQQ", "IWM", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA"]


def watchlist_tickers() -> list[str]:
    return watchlist_store.active_tickers()


def fetch_news(ticker: str, max_items: int = 10) -> list[dict]:
    cache_key = f"news_{ticker.upper()}"
    cached = cache_get(cache_key, ttl_seconds=CACHE_TTL)
    if cached is not None:
        return cached

    # Primary: Finnhub
    if has_key("FINNHUB_API_KEY"):
        try:
            from _providers import finnhub
            fh = finnhub()
            items = fh.company_news(ticker, days=7) or []
            if items:
                out = []
                for it in items[:max_items]:
                    ts = it.get("datetime", 0)
                    iso = ""
                    if ts:
                        iso = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec="seconds")
                    out.append({
                        "title": (it.get("headline") or "")[:200],
                        "publisher": (it.get("source") or "")[:30],
                        "link": it.get("url") or "",
                        "published": iso,
                        "summary": (it.get("summary") or "")[:300],
                    })
                cache_put(cache_key, out)
                return out
        except Exception:
            pass

    # Fallback: yfinance
    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        return []

    out = []
    for it in items[:max_items]:
        # yfinance shape varies; defensive extraction
        content = it.get("content", it)
        title = (content.get("title") or it.get("title") or "").strip()
        publisher = ""
        if isinstance(content.get("provider"), dict):
            publisher = content["provider"].get("displayName", "")
        elif content.get("publisher"):
            publisher = content["publisher"]

        link = ""
        cu = content.get("canonicalUrl") or content.get("clickThroughUrl")
        if isinstance(cu, dict):
            link = cu.get("url", "")
        elif isinstance(cu, str):
            link = cu
        link = link or it.get("link", "")

        ts = (content.get("pubDate") or content.get("displayTime")
              or it.get("providerPublishTime") or "")
        # int seconds → ISO
        if isinstance(ts, (int, float)) and ts > 0:
            ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
        if not title:
            continue
        out.append({"title": title, "publisher": publisher, "link": link, "published": str(ts)})
    cache_put(cache_key, out)
    return out


def _age_hours(ts: str) -> float | None:
    try:
        s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--from-watchlist", action="store_true")
    ap.add_argument("--majors", action="store_true")
    ap.add_argument("--hours", type=float, default=None,
                    help="Filter to items within last N hours.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    tickers: list[str] = [t.upper() for t in args.tickers]
    if args.majors:
        tickers = MAJORS
    elif args.from_watchlist:
        tickers = watchlist_tickers()
    if not tickers:
        print("No tickers. Pass names, --majors, or --from-watchlist.", file=sys.stderr)
        return 1

    all_results: dict[str, list[dict]] = {}
    for t in tickers:
        items = fetch_news(t)
        if args.hours is not None:
            filtered = []
            for it in items:
                age = _age_hours(it.get("published", ""))
                if age is not None and age <= args.hours:
                    filtered.append({**it, "age_hours": round(age, 2)})
            items = filtered
        all_results[t] = items

    if args.json:
        # Compact: just title + publisher + age per ticker; no links
        compact = {}
        for t, items in all_results.items():
            compact[t] = [
                {"title": it.get("title", "")[:120],
                 "pub": it.get("publisher", "")[:30],
                 "age_h": it.get("age_hours")}
                for it in items[:5]
            ]
        total = sum(len(v) for v in compact.values())
        headline = f"{total} items across {len(compact)} tickers"
        emit(step_result("news", ok=True, headline=headline, data=compact))
        return 0

    for t, items in all_results.items():
        print(f"\n=== {t} === ({len(items)} item(s))")
        if not items:
            print("  (none)")
            continue
        for it in items:
            age = _age_hours(it.get("published", ""))
            age_s = f"{age:.1f}h" if age is not None else "?"
            print(f"  [{age_s:>6}] {it.get('publisher', '?'):<20}  {it.get('title', '')[:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
