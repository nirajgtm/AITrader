"""Working universe definition.

The universe is the set of tickers we consider tradable / scannable. It is
NOT the set we research every day — it's the haystack that scanners (movers,
breakouts, vol expansion) filter to find tradable needles.

Composition:
  1. S&P 500 constituents      — via Wikipedia (cached 7d)
  2. Nasdaq 100 constituents   — via Wikipedia (cached 7d)
  3. 11 S&P sector ETFs        — static
  4. Leveraged + inverse ETFs  — static (TQQQ/SQQQ/SOXL/SOXS/UPRO/SPXS/etc.)
  5. Crypto-equity proxies     — static (MARA/RIOT/COIN/CLSK/MSTR/IBIT)
  6. Common watch list ETFs    — static (GLD/SLV/USO/UNG/TLT/HYG)

Functions:
  get_universe() -> set[str]
  is_in_universe(ticker) -> bool
  ticker_metadata(ticker) -> dict | None  (sector, industry if known)
  refresh() -> int  (force re-fetch, returns count)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from _cache import cache_get, cache_put  # noqa: E402

CACHE_TTL = 7 * 24 * 3600

# Static layers (always included)
SECTOR_ETFS = {"XLK", "XLE", "XLF", "XLV", "XLI", "XLU", "XLP", "XLY",
               "XLB", "XLRE", "XLC"}

INDEX_ETFS = {"SPY", "QQQ", "IWM", "DIA", "MDY", "RSP", "VOO", "VTI"}

LEVERAGED_INVERSE = {
    # 3x bull
    "TQQQ", "UPRO", "SOXL", "TNA", "FAS", "ERX", "LABU", "DRN",
    # 3x bear
    "SQQQ", "SPXS", "SPXU", "SOXS", "TZA", "FAZ", "ERY", "LABD", "DRV",
    # Vol
    "UVXY", "VXX", "SVXY", "VIXY",
    # 2x
    "SSO", "QLD", "USD", "URTY", "DDM",
    "SDS", "QID", "TWM", "DXD",
}

CRYPTO_EQUITIES = {"MARA", "RIOT", "COIN", "CLSK", "MSTR", "IBIT", "FBTC",
                   "GBTC", "ETHE", "BITO", "ETHA"}

COMMODITIES_ETFS = {"GLD", "SLV", "USO", "UNG", "TLT", "IEF", "HYG", "LQD",
                    "DBC", "DBA", "USCI"}

# High-volume retail favorites that sit OUTSIDE the S&P 500 and Nasdaq 100
# but routinely swing >5% on >10M ADV. Without this layer, the universe gate
# (used by movers / breakouts / breakdowns / scanner) silently drops them.
#
# Maintenance: reviewed on the first trader invocation of each calendar month
# per SKILL.md "Monthly maintenance — universe review". Marker:
# state/last_universe_review.txt.
HIGH_VOL_RETAIL = {
    # Fintech / lending
    "SOFI", "AFRM", "UPST", "LMND",
    # EV / mobility (non-S&P)
    "RIVN", "LCID", "QS", "NIO",
    # Travel / cruise
    "AAL", "CCL", "NCLH", "RCL",
    # Social / consumer internet
    "RDDT", "SNAP", "PINS",
    # Gaming / sports betting
    "DKNG", "PENN",
    # Recently public / hot retail
    "TOST", "ARM",
    # Health / wellness retail
    "HIMS",
    # Meme / retail favorites
    "AMC",
    # Crypto-leverage proxies not in CRYPTO_EQUITIES
    "HUT", "BITF",
}


_WIKI_UA = {"User-Agent": "Mozilla/5.0 (autonomous-trader universe refresh)"}


def _wiki_constituents(url: str, col: str, cache_key: str) -> set[str]:
    """Fetch a constituent symbol set from a Wikipedia table. Cached 7d. On a
    fetch failure, fall back to the last cached value even if stale, so a
    transient Wikipedia error never zeroes out the universe."""
    cached = cache_get(cache_key, ttl_seconds=CACHE_TTL)
    if cached:
        return set(cached)
    try:
        import requests
        import pandas as pd
        from io import StringIO
        r = requests.get(url, headers=_WIKI_UA, timeout=25)
        r.raise_for_status()
        syms: set[str] = set()
        for tbl in pd.read_html(StringIO(r.text)):
            if col in [str(c) for c in tbl.columns]:
                for s in tbl[col].tolist():
                    s = str(s).replace(".", "-").strip().upper()
                    if s and s != "NAN":
                        syms.add(s)
                break
        if syms:
            cache_put(cache_key, sorted(syms))
            return syms
    except Exception:
        pass
    stale = cache_get(cache_key, ttl_seconds=10 ** 9)
    return set(stale) if stale else set()


def _sp500_constituents() -> set[str]:
    return _wiki_constituents(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "Symbol", "universe_sp500")


def _nasdaq100_constituents() -> set[str]:
    return _wiki_constituents(
        "https://en.wikipedia.org/wiki/Nasdaq-100",
        "Ticker", "universe_nasdaq100")


def get_universe() -> set[str]:
    """Return the full union of working tickers.

    Don't cache the union if constituent fetches failed (would otherwise pin
    a tiny fallback-only universe for 7 days).
    """
    cached = cache_get("universe_full", ttl_seconds=CACHE_TTL)
    if cached and len(cached) >= 200:
        # Sanity threshold: a real universe has 500+; <200 means constituent
        # fetches failed and we cached a fallback. Force re-fetch.
        return set(cached)

    sp500 = _sp500_constituents()
    nasdaq = _nasdaq100_constituents()
    full = (sp500 | nasdaq | SECTOR_ETFS | INDEX_ETFS | LEVERAGED_INVERSE
            | CRYPTO_EQUITIES | COMMODITIES_ETFS | HIGH_VOL_RETAIL)
    # Only cache if we got real constituent data
    if len(sp500) >= 400:
        cache_put("universe_full", sorted(full))
    return full


def is_in_universe(ticker: str) -> bool:
    return ticker.upper() in get_universe()


def ticker_metadata(ticker: str) -> Optional[dict]:
    """Return cached metadata for a ticker if we have it (sector/industry).

    Sourced from FMP constituent data when available; static layers don't carry
    metadata.
    """
    cached = cache_get("universe_metadata", ttl_seconds=CACHE_TTL)
    if cached and ticker.upper() in cached:
        return cached[ticker.upper()]
    # Build metadata index from constituent data
    try:
        from _providers import fmp
        f = fmp()
        meta: dict = {}
        for d in (f.sp500_constituents() or []):
            sym = (d.get("symbol") or "").upper()
            if sym:
                meta[sym] = {"sector": d.get("sector"), "industry": d.get("subSector"),
                             "name": d.get("name")}
        for d in (f.nasdaq100_constituents() or []):
            sym = (d.get("symbol") or "").upper()
            if sym and sym not in meta:
                meta[sym] = {"sector": d.get("sector"), "industry": d.get("subSector"),
                             "name": d.get("name")}
        # Static layers — synthesize light metadata
        for sym in SECTOR_ETFS:
            meta.setdefault(sym, {"sector": "Sector ETF", "name": f"{sym} sector ETF"})
        for sym in INDEX_ETFS:
            meta.setdefault(sym, {"sector": "Index ETF", "name": f"{sym} index ETF"})
        for sym in LEVERAGED_INVERSE:
            meta.setdefault(sym, {"sector": "Leveraged/Inverse ETF",
                                  "name": f"{sym} lev/inv ETF"})
        for sym in CRYPTO_EQUITIES:
            meta.setdefault(sym, {"sector": "Crypto-related",
                                  "name": f"{sym} crypto-equity"})
        for sym in COMMODITIES_ETFS:
            meta.setdefault(sym, {"sector": "Commodity/Bond ETF",
                                  "name": f"{sym} commodity/bond ETF"})
        cache_put("universe_metadata", meta)
        return meta.get(ticker.upper())
    except Exception:
        return None


def refresh() -> int:
    """Force re-fetch of constituents."""
    from _cache import cache_clear
    for k in ("universe_sp500", "universe_nasdaq100",
              "universe_full", "universe_metadata"):
        cache_clear(k)
    return len(get_universe())


def main() -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--check", help="Check if a ticker is in universe")
    ap.add_argument("--meta", help="Show metadata for a ticker")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.refresh:
        n = refresh()
        print(f"Universe refreshed: {n} tickers")
        return 0
    if args.check:
        tk = args.check.upper()
        in_u = is_in_universe(tk)
        if args.json:
            print(json.dumps({"ticker": tk, "in_universe": in_u}))
        else:
            print(f"{tk}: {'YES' if in_u else 'NO'}")
        return 0
    if args.meta:
        m = ticker_metadata(args.meta)
        print(json.dumps(m, indent=2) if not args.json else json.dumps(m))
        return 0
    # Default: show stats
    u = get_universe()
    sp500 = _sp500_constituents()
    nasdaq = _nasdaq100_constituents()
    summary = {
        "total": len(u),
        "sp500": len(sp500),
        "nasdaq100": len(nasdaq),
        "sector_etfs": len(SECTOR_ETFS),
        "index_etfs": len(INDEX_ETFS),
        "leveraged_inverse": len(LEVERAGED_INVERSE),
        "crypto_equities": len(CRYPTO_EQUITIES),
        "commodities_bonds": len(COMMODITIES_ETFS),
    }
    if args.json:
        print(json.dumps(summary))
    else:
        print("=== Working universe ===")
        for k, v in summary.items():
            print(f"  {k:<20}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
