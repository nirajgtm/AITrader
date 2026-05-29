"""Polygon.io API client (formerly massive.com).

Polygon.io is a top-tier financial data provider. Free tier confirmed
(probed 2026-04-26):
  ✅ /v2/aggs/...                  — OHLC bars (15-min delayed)
  ✅ /v2/aggs/ticker/X/prev        — previous close
  ✅ /v2/aggs/grouped/.../{date}   — ALL ~12k US stocks OHLC for one date (huge!)
  ✅ /v2/reference/news            — news with sentiment scores
  ✅ /v3/reference/tickers/X       — fundamentals snapshot
  ✅ /v3/reference/options/contracts — options chain reference
  ✅ /v1/indicators/{rsi,sma,ema,macd}/X — technical indicators server-side

Paid-only (skip on free):
  ❌ /v2/snapshot/...              — real-time snapshots
  ❌ /v2/snapshot/.../gainers, losers, most-actives — paid movers

Rate limit: 5 req/min on free tier. Use grouped_daily for bulk efficiency.

Docs: https://polygon.io/docs

Note: The provider class is still named `Massive` for stable imports — the
underlying brand has been renamed to Polygon.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from ._base import BaseProvider


class Massive(BaseProvider):
    name = "polygon"
    base_url = "https://api.polygon.io"
    rate_limit_per_minute = 5
    rate_limit_per_day = None
    api_key_env = "MASSIVE_API_KEY"
    auth_method = "query"
    auth_param = "apiKey"

    # ---------- aggregates / OHLC ----------

    def aggs(self, ticker: str, multiplier: int = 1, timespan: str = "day",
             from_: Optional[str] = None, to: Optional[str] = None,
             limit: int = 365) -> Optional[list[dict]]:
        """OHLCV bars. timespan: minute|hour|day|week|month.

        Returns list of {t (ms), o, h, l, c, v, vw, n}.
        """
        if from_ is None:
            from_ = (date.today() - timedelta(days=365)).isoformat()
        if to is None:
            to = date.today().isoformat()
        path = f"v2/aggs/ticker/{ticker.upper()}/range/{multiplier}/{timespan}/{from_}/{to}"
        ck = f"aggs_{ticker.upper()}_{multiplier}_{timespan}_{from_}_{to}"
        data = self.get(path, params={"adjusted": "true", "sort": "asc", "limit": limit},
                        cache_key=ck, cache_ttl=600)
        if isinstance(data, dict):
            return data.get("results") or []
        return data

    def prev_close(self, ticker: str) -> Optional[dict]:
        """Previous trading day's OHLCV."""
        data = self.get(f"v2/aggs/ticker/{ticker.upper()}/prev",
                        params={"adjusted": "true"},
                        cache_key=f"prev_close_{ticker.upper()}", cache_ttl=300)
        if isinstance(data, dict):
            results = data.get("results") or []
            return results[0] if results else None
        return data

    def grouped_daily(self, day: Optional[str] = None) -> Optional[list[dict]]:
        """ALL US stocks OHLCV for a single date — ~12,000 tickers in ONE call.

        This is the killer endpoint for universe-wide scans (breakouts,
        breakdowns, vol expansion). Use the latest closed market day; weekend
        invocations should pass yesterday's date.
        """
        if day is None:
            day = date.today().isoformat()
        path = f"v2/aggs/grouped/locale/us/market/stocks/{day}"
        return _maybe_list(self.get(path, params={"adjusted": "true"},
                                    cache_key=f"grouped_daily_{day}",
                                    cache_ttl=12 * 3600))

    # ---------- news ----------

    def news(self, ticker: Optional[str] = None, limit: int = 20,
             order: str = "desc") -> Optional[list[dict]]:
        """News with sentiment scores. Polygon includes 'insights' per article
        with sentiment ('positive'/'neutral'/'negative') and reasoning.
        """
        params = {"limit": limit, "order": order, "sort": "published_utc"}
        if ticker:
            params["ticker"] = ticker.upper()
        ck = f"polygon_news_{ticker or 'all'}_{limit}"
        return _maybe_list(self.get("v2/reference/news", params=params,
                                    cache_key=ck, cache_ttl=3600))

    # ---------- ticker reference ----------

    def ticker_details(self, ticker: str) -> Optional[dict]:
        """Fundamentals snapshot: sector, industry, description, market cap,
        share class, primary exchange, etc.
        """
        data = self.get(f"v3/reference/tickers/{ticker.upper()}",
                        params={},
                        cache_key=f"ticker_details_{ticker.upper()}",
                        cache_ttl=24 * 3600)
        if isinstance(data, dict):
            return data.get("results")
        return data

    # ---------- options ----------

    def options_contracts(self, underlying: str,
                          expiration_date: Optional[str] = None,
                          contract_type: Optional[str] = None,
                          limit: int = 100) -> Optional[list[dict]]:
        """Options contract reference (strikes/expiries available).

        contract_type: 'call' | 'put' | None (both)
        """
        params = {"underlying_ticker": underlying.upper(), "limit": limit,
                  "expired": "false"}
        if expiration_date:
            params["expiration_date"] = expiration_date
        if contract_type:
            params["contract_type"] = contract_type
        ck = f"options_contracts_{underlying.upper()}_{expiration_date or 'all'}_{contract_type or 'both'}"
        return _maybe_list(self.get("v3/reference/options/contracts",
                                    params=params,
                                    cache_key=ck, cache_ttl=4 * 3600))

    # ---------- technical indicators (server-side) ----------

    def rsi(self, ticker: str, window: int = 14, timespan: str = "day",
            limit: int = 30) -> Optional[list[dict]]:
        path = f"v1/indicators/rsi/{ticker.upper()}"
        params = {"timespan": timespan, "window": window,
                  "series_type": "close", "order": "desc", "limit": limit,
                  "adjusted": "true"}
        ck = f"rsi_{ticker.upper()}_{window}_{timespan}"
        data = self.get(path, params=params, cache_key=ck, cache_ttl=600)
        if isinstance(data, dict):
            return (data.get("results") or {}).get("values") or []
        return data

    def sma(self, ticker: str, window: int, timespan: str = "day",
            limit: int = 30) -> Optional[list[dict]]:
        path = f"v1/indicators/sma/{ticker.upper()}"
        params = {"timespan": timespan, "window": window,
                  "series_type": "close", "order": "desc", "limit": limit,
                  "adjusted": "true"}
        ck = f"sma_{ticker.upper()}_{window}_{timespan}"
        data = self.get(path, params=params, cache_key=ck, cache_ttl=600)
        if isinstance(data, dict):
            return (data.get("results") or {}).get("values") or []
        return data

    def macd(self, ticker: str, timespan: str = "day",
             limit: int = 30) -> Optional[list[dict]]:
        path = f"v1/indicators/macd/{ticker.upper()}"
        params = {"timespan": timespan, "series_type": "close",
                  "order": "desc", "limit": limit, "adjusted": "true"}
        ck = f"macd_{ticker.upper()}_{timespan}"
        data = self.get(path, params=params, cache_key=ck, cache_ttl=600)
        if isinstance(data, dict):
            return (data.get("results") or {}).get("values") or []
        return data

    # ---------- ticker list (for universe construction) ----------

    def tickers_active(self, market: str = "stocks", limit: int = 1000,
                       cursor: Optional[str] = None) -> Optional[list[dict]]:
        """List active tickers in market. Free tier = 1000/page; pagination via cursor."""
        params = {"market": market, "active": "true", "limit": limit}
        if cursor:
            params["cursor"] = cursor
        ck = f"tickers_active_{market}_{limit}_{cursor or 'first'}"
        return _maybe_list(self.get("v3/reference/tickers", params=params,
                                    cache_key=ck, cache_ttl=24 * 3600))


def _maybe_list(data) -> Optional[list]:
    """Coerce Polygon response (dict with 'results' key) to a list."""
    if isinstance(data, dict):
        r = data.get("results")
        if isinstance(r, list):
            return r
        return []
    return data
