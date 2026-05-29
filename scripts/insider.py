#!/usr/bin/env python3
"""Insider transactions.

Two complementary signals:
  1. SEC EDGAR Form 4 cluster activity (counts of filings per issuer over N days).
     Raw, comprehensive, free. EDGAR rate-limit 10 req/sec. UA header required.
  2. Finnhub /stock/insider-sentiment per ticker (--ticker mode): aggregated
     MSPR (Monthly Share Purchase Ratio). Cleaner directional signal than raw
     Form-4 counts. Positive MSPR = net buying.

Usage:
  insider.py --days 30                  # cluster buys (EDGAR)
  insider.py --ticker NVDA              # per-ticker EDGAR + Finnhub MSPR
  insider.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

from _cache import cache_get, cache_put
from _terse import emit, step_result
from _apikeys import get_key

_contact = get_key("SEC_CONTACT_EMAIL") or "trader@example.com"
UA = f"Trader-Skill/1.0 ({_contact})"  # SEC requires identification; set SEC_CONTACT_EMAIL in .env
CACHE_TTL = 4 * 3600

EDGAR_FTS = "https://efts.sec.gov/LATEST/search-index"


def _http_get(url: str, params: dict | None = None) -> dict | None:
    headers = {"User-Agent": UA, "Accept": "application/json"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def fetch_form4_recent(days: int = 30, max_results: int = 200) -> list[dict]:
    cache_key = f"edgar_form4_{days}d"
    cached = cache_get(cache_key, ttl_seconds=CACHE_TTL)
    if cached is not None:
        return cached

    end = date.today()
    start = end - timedelta(days=days)
    params = {
        "q": '"4"',  # form type
        "dateRange": "custom",
        "startdt": start.isoformat(),
        "enddt": end.isoformat(),
        "forms": "4",
    }
    data = _http_get(EDGAR_FTS, params=params)
    if not data:
        return []

    hits = (data.get("hits") or {}).get("hits") or []
    out = []
    for h in hits[:max_results]:
        src = h.get("_source", {})
        out.append({
            "issuer": src.get("display_names", [""])[0] if src.get("display_names") else "",
            "filed": src.get("file_date"),
            "form": src.get("form"),
            "accession": h.get("_id"),
            "ciks": src.get("ciks", []),
        })
    cache_put(cache_key, out)
    return out


def fetch_company_insider(ticker: str, days: int = 90) -> list[dict]:
    """Get Form 4 filings for a specific issuer ticker.

    EDGAR's company-facts JSON requires CIK lookup. We use the company-tickers map
    (cached daily) to resolve ticker → CIK, then pull filings.
    """
    cik = _ticker_to_cik(ticker)
    if not cik:
        return []
    cache_key = f"edgar_insider_{ticker.upper()}_{days}d"
    cached = cache_get(cache_key, ttl_seconds=CACHE_TTL)
    if cached is not None:
        return cached

    url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
    data = _http_get(url)
    if not data:
        return []
    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    out = []
    for form, dt, acc in zip(forms, dates, accessions):
        if form == "4" and dt >= cutoff:
            out.append({"form": form, "filed": dt, "accession": acc})
    cache_put(cache_key, out)
    return out


def _ticker_to_cik(ticker: str) -> str | None:
    cache_key = "edgar_ticker_to_cik_map"
    cached = cache_get(cache_key, ttl_seconds=24 * 3600)
    if cached is None:
        url = "https://www.sec.gov/files/company_tickers.json"
        data = _http_get(url)
        if not data:
            return None
        cached = {v["ticker"].upper(): str(v["cik_str"]) for v in data.values()}
        cache_put(cache_key, cached)
    return cached.get(ticker.upper())


def cluster_buys(days: int = 30, min_cluster: int = 3) -> list[dict]:
    """Aggregate by issuer; report names with >= min_cluster Form-4 filings."""
    items = fetch_form4_recent(days=days, max_results=500)
    by_issuer: dict[str, int] = {}
    for it in items:
        name = it.get("issuer", "")
        if name:
            by_issuer[name] = by_issuer.get(name, 0) + 1
    clusters = sorted(((n, c) for n, c in by_issuer.items() if c >= min_cluster),
                      key=lambda x: -x[1])
    return [{"issuer": n, "filings": c} for n, c in clusters]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--ticker", default=None, help="Specific issuer ticker.")
    ap.add_argument("--cluster-min", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.ticker:
        items = fetch_company_insider(args.ticker, args.days)

        # Augment with Finnhub MSPR if available
        mspr = None
        try:
            from _apikeys import has_key
            if has_key("FINNHUB_API_KEY"):
                from _providers import finnhub
                fh = finnhub()
                sentiment = fh.insider_sentiment(args.ticker, months_back=6)
                if sentiment and "data" in sentiment:
                    rows = sentiment["data"]
                    if rows:
                        # Latest month MSPR + 3-month average
                        recent = sorted(rows, key=lambda x: (x.get("year", 0), x.get("month", 0)))[-3:]
                        avg_mspr = sum(r.get("mspr", 0) for r in recent) / len(recent)
                        latest = recent[-1] if recent else None
                        mspr = {
                            "latest_year": latest.get("year") if latest else None,
                            "latest_month": latest.get("month") if latest else None,
                            "latest_mspr": round(latest.get("mspr", 0), 3) if latest else None,
                            "latest_change": latest.get("change") if latest else None,
                            "3mo_avg_mspr": round(avg_mspr, 3),
                            "interpretation": ("net buying" if avg_mspr > 0.1
                                               else "net selling" if avg_mspr < -0.1
                                               else "neutral"),
                        }
        except Exception:
            pass

        if args.json:
            flags = []
            headline = f"{args.ticker} {len(items)} Form-4 filings in {args.days}d"
            if mspr:
                headline += f"; MSPR(3mo)={mspr['3mo_avg_mspr']} ({mspr['interpretation']})"
                if mspr["3mo_avg_mspr"] > 0.3:
                    flags.append(f"insider_buying_{args.ticker.lower()}")
                elif mspr["3mo_avg_mspr"] < -0.3:
                    flags.append(f"insider_selling_{args.ticker.lower()}")
            emit(step_result("insider", ok=True, headline=headline,
                             data={"ticker": args.ticker, "items": items[:20], "mspr": mspr},
                             flags=flags))
            return 0
        print(f"=== Insider Form-4 filings: {args.ticker.upper()} (last {args.days}d) ===")
        if not items:
            print("(none found or fetch failed)")
            return 0
        for it in items:
            print(f"  {it['filed']}  form={it['form']}  acc={it['accession']}")
        return 0

    clusters = cluster_buys(days=args.days, min_cluster=args.cluster_min)
    if args.json:
        flags = []
        if clusters:
            flags.append("insider_clusters_active")
        top5 = ",".join(c["issuer"] for c in clusters[:5])
        emit(step_result("insider", ok=True,
                         headline=f"{len(clusters)} clusters in {args.days}d; top: {top5}",
                         data={"clusters": clusters[:30]}, flags=flags))
        return 0
    print(f"=== Form-4 cluster activity (last {args.days}d, ≥ {args.cluster_min} filings) ===")
    if not clusters:
        print("(no clusters found — try --days 60 or lower --cluster-min)")
        return 0
    for c in clusters[:30]:
        print(f"  {c['filings']:>3}  {c['issuer']}")
    print("\nNote: Form-4 includes BOTH purchases and sales. Cross-check direction with openinsider.com")
    print("for the same name before treating as a buy signal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
