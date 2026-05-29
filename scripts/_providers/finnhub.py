"""Finnhub API client.

Free tier: 60 requests/minute. Generous enough for live use.

Best uses (this is the workhorse):
  - news/company news (with sentiment) — better than yfinance.news
  - earnings calendar (full universe in one call)
  - earnings surprises (historical beat/miss + reaction)
  - insider sentiment (aggregated form-4 direction)
  - recommendation trends
  - IPO calendar
  - splits/dividends

Free tier limits NOTABLY exclude:
  - real-time options chains
  - high-frequency quote endpoints (paid)
  - earnings transcripts (paid)

Docs: https://finnhub.io/docs/api
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Optional

from ._base import BaseProvider


class Finnhub(BaseProvider):
    name = "finnhub"
    base_url = "https://finnhub.io/api/v1"
    rate_limit_per_minute = 55  # leave headroom under 60
    rate_limit_per_day = None
    api_key_env = "FINNHUB_API_KEY"
    auth_method = "query"
    auth_param = "token"

    # ---------- news ----------

    def company_news(self, symbol: str, days: int = 7) -> Optional[list[dict]]:
        """Recent company news. Includes datetime, headline, summary, source, url, image, related."""
        end = date.today()
        start = end - timedelta(days=days)
        return self.get(
            "company-news",
            params={
                "symbol": symbol.upper(),
                "from": start.isoformat(),
                "to": end.isoformat(),
            },
            cache_key=f"company_news_{symbol.upper()}_{days}d",
            cache_ttl=3600,  # 1h
        )

    def market_news(self, category: str = "general") -> Optional[list[dict]]:
        """Market-wide news. category in {general, forex, crypto, merger}."""
        return self.get(
            "news",
            params={"category": category},
            cache_key=f"market_news_{category}",
            cache_ttl=900,
        )

    def news_sentiment(self, symbol: str) -> Optional[dict]:
        """Aggregated buzz/sentiment scoring for a symbol."""
        return self.get(
            "news-sentiment",
            params={"symbol": symbol.upper()},
            cache_key=f"news_sentiment_{symbol.upper()}",
            cache_ttl=4 * 3600,
        )

    # ---------- earnings ----------

    def earnings_calendar(self, days_ahead: int = 14, days_back: int = 0,
                          symbol: Optional[str] = None,
                          chunk_days: int = 7) -> Optional[list[dict]]:
        """Earnings calendar.

        Free tier truncates to ~1500 items per call. In earnings season (~600
        names per week), a 30-day window gets cut off. We chunk by `chunk_days`
        and concatenate to ensure full coverage. Each chunk is cached
        independently so subsequent calls are free.
        """
        today = date.today()
        all_items: list[dict] = []
        # March from -days_back to +days_ahead in chunk_days windows
        cur_offset = -days_back
        end_offset = days_ahead
        while cur_offset <= end_offset:
            chunk_end_offset = min(cur_offset + chunk_days, end_offset)
            params = {
                "from": (today + timedelta(days=cur_offset)).isoformat(),
                "to": (today + timedelta(days=chunk_end_offset)).isoformat(),
            }
            if symbol:
                params["symbol"] = symbol.upper()
            ck = f"earnings_cal_{params['from']}_{params['to']}_{symbol or 'all'}"
            data = self.get("calendar/earnings", params=params,
                            cache_key=ck, cache_ttl=12 * 3600)
            if isinstance(data, dict):
                items = data.get("earningsCalendar", [])
                if items:
                    all_items.extend(items)
            cur_offset = chunk_end_offset + 1
        # Dedupe by (symbol, date)
        seen = set()
        out = []
        for it in all_items:
            key = (it.get("symbol"), it.get("date"))
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
        # Sort by date asc
        out.sort(key=lambda x: x.get("date", ""))
        return out

    def earnings_history(self, symbol: str) -> Optional[list[dict]]:
        """Last several quarters of earnings results: estimate, actual, surprise %."""
        return self.get(
            "stock/earnings",
            params={"symbol": symbol.upper()},
            cache_key=f"earnings_history_{symbol.upper()}",
            cache_ttl=24 * 3600,
        )

    # ---------- insiders ----------

    def insider_sentiment(self, symbol: str, months_back: int = 6) -> Optional[dict]:
        """Aggregated insider transaction direction (MSPR — Monthly Share Purchase Ratio).
        Positive MSPR = net buying; negative = net selling.
        """
        end = date.today()
        start = end - timedelta(days=30 * months_back)
        return self.get(
            "stock/insider-sentiment",
            params={
                "symbol": symbol.upper(),
                "from": start.isoformat(),
                "to": end.isoformat(),
            },
            cache_key=f"insider_sentiment_{symbol.upper()}_{months_back}mo",
            cache_ttl=12 * 3600,
        )

    def insider_transactions(self, symbol: str, days: int = 90) -> Optional[dict]:
        """Recent Form-4 transactions (richer than just SEC EDGAR listing)."""
        end = date.today()
        start = end - timedelta(days=days)
        return self.get(
            "stock/insider-transactions",
            params={
                "symbol": symbol.upper(),
                "from": start.isoformat(),
                "to": end.isoformat(),
            },
            cache_key=f"insider_tx_{symbol.upper()}_{days}d",
            cache_ttl=12 * 3600,
        )

    # ---------- recommendations / fundamentals ----------

    def recommendations(self, symbol: str) -> Optional[list[dict]]:
        """Analyst recommendation trends — strongBuy/buy/hold/sell/strongSell counts by month."""
        return self.get(
            "stock/recommendation",
            params={"symbol": symbol.upper()},
            cache_key=f"recommendations_{symbol.upper()}",
            cache_ttl=24 * 3600,
        )

    def quote(self, symbol: str) -> Optional[dict]:
        """Real-time quote (delayed on free tier). Returns c (current), h, l, o, pc."""
        return self.get(
            "quote",
            params={"symbol": symbol.upper()},
            cache_key=f"quote_{symbol.upper()}",
            cache_ttl=60,  # quote is short-lived
        )

    def basic_financials(self, symbol: str) -> Optional[dict]:
        """Wide grid of fundamentals (PE/PB/52w high/52w low/etc)."""
        return self.get(
            "stock/metric",
            params={"symbol": symbol.upper(), "metric": "all"},
            cache_key=f"financials_{symbol.upper()}",
            cache_ttl=24 * 3600,
        )

    # ---------- IPO / dividends / splits ----------

    def ipo_calendar(self, days_ahead: int = 30) -> Optional[list[dict]]:
        end = date.today() + timedelta(days=days_ahead)
        data = self.get(
            "calendar/ipo",
            params={"from": date.today().isoformat(), "to": end.isoformat()},
            cache_key=f"ipo_cal_{days_ahead}d",
            cache_ttl=12 * 3600,
        )
        if isinstance(data, dict):
            return data.get("ipoCalendar", [])
        return data
