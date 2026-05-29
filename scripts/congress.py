#!/usr/bin/env python3
"""Congressional trade disclosures — STOCK Act filings.

Status of free data sources (audited 2026-04-25):
  - capitoltrades.com /api/trades:    503 (broken upstream)
  - capitoltrades.com /trades (HTML): scrapable but brittle
  - quiverquant.com API:              401 without key (free tier requires signup)
  - house.gov /api/PublicDisclosure:  no JSON endpoint
  - senate.gov efdsearch:             gated by tos checkbox

Implemented:
  - PRIMARY: Quiver Quant API IF env var QUIVER_API_KEY is set
    (signup at quiverquant.com — free tier 100 req/day; $10/mo basic)
  - FALLBACK: cached HTML scrape of capitoltrades.com (table parsing)
  - Both cached for 12h.

Premium upgrades worth considering for cleaner data:
  - Quiver Quant Basic ($10/mo) or Pro ($50/mo): API + Capitol Hill dashboards.
  - Unusual Whales ($48-99/mo): congress + dark pool + flow in one product.

Usage:
  congress.py                   # last 7 days, cluster + size signals
  congress.py --ticker NVDA     # filings on a specific name
  congress.py --days 14
  congress.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta

import requests

from _cache import cache_get, cache_put
from _terse import emit, step_result, data_gap

CACHE_TTL = 12 * 3600

QUIVER_BASE = "https://api.quiverquant.com/beta"
CAPITOLTRADES_HTML = "https://www.capitoltrades.com/trades"


def _quiver_key() -> str | None:
    return os.environ.get("QUIVER_API_KEY")


def _http_get(url: str, headers: dict | None = None, params: dict | None = None) -> tuple[int, str]:
    h = {"User-Agent": "Mozilla/5.0 trader-skill", "Accept": "application/json"}
    if headers:
        h.update(headers)
    try:
        r = requests.get(url, headers=h, params=params, timeout=20)
        return r.status_code, r.text
    except Exception as e:
        return 0, f"error: {e}"


def fetch_quiver(days: int) -> list[dict] | None:
    key = _quiver_key()
    cache_key = f"quiver_congress_{days}d"
    cached = cache_get(cache_key, ttl_seconds=CACHE_TTL)
    if cached is not None:
        return cached
    extra = {"Authorization": f"Bearer {key}"} if key else {}
    code, body = _http_get(
        f"{QUIVER_BASE}/live/congresstrading",
        headers=extra,
    )
    if code != 200:
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    out = [r for r in data if str(r.get("TransactionDate", "")) >= cutoff]
    cache_put(cache_key, out)
    return out


def fetch_capitoltrades_html(days: int) -> list[dict] | None:
    """Fallback HTML scrape. Brittle by nature; cached aggressively.

    Returns None if scrape fails — caller treats as a data gap.
    """
    cache_key = f"capitoltrades_html_{days}d"
    cached = cache_get(cache_key, ttl_seconds=CACHE_TTL)
    if cached is not None:
        return cached
    code, body = _http_get(CAPITOLTRADES_HTML)
    if code != 200 or not body:
        return None

    # The HTML uses table rows. We extract via regex (no bs4 dep). This is brittle.
    rows = []
    # Match table rows that likely contain a trade
    for m in re.finditer(
        r'<tr[^>]*>(.*?)</tr>',
        body, re.DOTALL,
    ):
        row_html = m.group(1)
        # Simple heuristic: trade rows contain a ticker pattern $XXX or similar
        ticker_m = re.search(r'\b([A-Z]{2,5})\b\s*(?:</span>|</a>|<)', row_html)
        date_m = re.search(r'(\d{4}-\d{2}-\d{2})', row_html)
        amount_m = re.search(r'(\$[\d,]+\s*-\s*\$[\d,]+)', row_html)
        if ticker_m and date_m:
            rows.append({
                "ticker": ticker_m.group(1),
                "filed": date_m.group(1),
                "amount_range": amount_m.group(1) if amount_m else "",
            })

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = [r for r in rows if r["filed"] >= cutoff]
    cache_put(cache_key, rows)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    source = "quiver"
    data = fetch_quiver(args.days)
    if data is None:
        source = "capitoltrades_html_fallback"
        data = fetch_capitoltrades_html(args.days)
    if data is None:
        if args.json:
            emit(step_result("congress", ok=False, headline="data gap",
                             data={"reason": "no working free source"},
                             errors=["Set QUIVER_API_KEY for clean JSON, or upgrade to Quiver Pro / Unusual Whales."],
                             actions=[data_gap("congress", "no working source")]))
        else:
            print("[DATA GAP] congress: no working source. Set QUIVER_API_KEY.")
        return 1

    if args.ticker:
        data = [r for r in data if (r.get("Ticker") or r.get("ticker", "")).upper() == args.ticker.upper()]

    if args.json:
        emit(step_result("congress", ok=True,
                         headline=f"{len(data)} items from {source}",
                         data={"source": source, "items": data[:30]}))
        return 0

    print(f"=== Congressional trades (last {args.days}d, source={source}) ===")
    if not data:
        print("(no items)")
        return 0
    for r in data[:30]:
        ticker = r.get("Ticker") or r.get("ticker", "")
        rep = r.get("Representative") or r.get("representative", "")
        side = r.get("Transaction") or r.get("transaction", "")
        amt = r.get("Amount") or r.get("amount_range", "")
        dt = r.get("TransactionDate") or r.get("filed", "")
        print(f"  {dt}  {ticker:<6} {side:<10} {amt:<25} {rep}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
