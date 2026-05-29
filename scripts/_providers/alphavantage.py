"""AlphaVantage API client.

Free tier: 25 requests/day, 5/minute. TIGHT — cache HARD. Reserve for high-value calls only.

Best uses (where AV has an edge):
  - Company overview (one call gets PE, PB, dividend, sector, industry, 52w hi/lo, EPS, etc.)
  - News & sentiment (with topic filters + sentiment scores)
  - Top gainers/losers/most-active (alternative to Yahoo screener)
  - Currency exchange rates
  - Sector performance (alternative reading)
  - Treasury yield + Fed funds (alternative to FRED)
  - Earnings (per-symbol; Finnhub better for full-cal)

Skip for routine use:
  - Time-series price (yfinance is free unlimited; AV would burn the daily budget)
  - Technical indicators (we compute these locally from yfinance)

Docs: https://www.alphavantage.co/documentation/

Strategy: gate calls behind explicit per-call methods; never auto-loop a list of
tickers (that would burn the 25/day budget instantly).
"""
from __future__ import annotations

from typing import Optional

from ._base import BaseProvider


class AlphaVantage(BaseProvider):
    name = "alphavantage"
    base_url = "https://www.alphavantage.co"
    rate_limit_per_minute = 5
    rate_limit_per_day = 25
    api_key_env = "ALPHAVANTAGE_API_KEY"
    auth_method = "query"
    auth_param = "apikey"

    # AV uses /query?function=FOO style. We expose narrow methods.

    def _q(self, function: str, params: dict, cache_key: str,
           cache_ttl: int = 24 * 3600) -> Optional[dict]:
        full_params = {"function": function, **params}
        return self.get("query", params=full_params,
                        cache_key=cache_key, cache_ttl=cache_ttl)

    def company_overview(self, symbol: str) -> Optional[dict]:
        """One-call snapshot: name, sector, industry, market cap, PE, PB, EPS,
        dividend yield, beta, 52w-high/low, profit margin, etc.

        Cache 24h — these don't change intraday.
        """
        return self._q(
            "OVERVIEW",
            params={"symbol": symbol.upper()},
            cache_key=f"av_overview_{symbol.upper()}",
            cache_ttl=24 * 3600,
        )

    def news_sentiment(self, tickers: Optional[list[str]] = None,
                       topics: Optional[list[str]] = None,
                       limit: int = 50) -> Optional[dict]:
        """News with topic filters + sentiment scores.

        topics: ["technology","earnings","ipo","mergers_and_acquisitions","financial_markets",
                 "economy_fiscal","economy_monetary","economy_macro","energy_transportation",
                 "finance","life_sciences","manufacturing","real_estate","retail_wholesale"]

        Cached 1h.
        """
        params = {"limit": min(limit, 1000)}
        ck_parts = []
        if tickers:
            params["tickers"] = ",".join(t.upper() for t in tickers)
            ck_parts.append(f"t-{params['tickers']}")
        if topics:
            params["topics"] = ",".join(topics)
            ck_parts.append(f"o-{params['topics']}")
        ck = "av_news_" + ("_".join(ck_parts) or "general") + f"_lim{limit}"
        return self._q("NEWS_SENTIMENT", params=params, cache_key=ck, cache_ttl=3600)

    def top_movers(self) -> Optional[dict]:
        """Returns top_gainers, top_losers, most_actively_traded as one dict."""
        return self._q(
            "TOP_GAINERS_LOSERS",
            params={},
            cache_key="av_top_movers",
            cache_ttl=600,
        )

    def earnings(self, symbol: str) -> Optional[dict]:
        """Annual + quarterly earnings (last several years)."""
        return self._q(
            "EARNINGS",
            params={"symbol": symbol.upper()},
            cache_key=f"av_earnings_{symbol.upper()}",
            cache_ttl=24 * 3600,
        )

    def earnings_calendar(self, horizon: str = "3month",
                          symbol: Optional[str] = None) -> Optional[str]:
        """Earnings calendar. Returns CSV text (AV exception to JSON)."""
        params = {"horizon": horizon}
        if symbol:
            params["symbol"] = symbol.upper()
        # CSV — bypass our JSON cache by using a different code path
        full_params = {"function": "EARNINGS_CALENDAR", **params, "apikey": self.api_key}
        try:
            self._check_and_record_call()
            r = self.session.get(self.base_url + "/query", params=full_params, timeout=self.timeout)
            return r.text if r.status_code == 200 else None
        except Exception:
            return None

    def fx_rate(self, from_currency: str, to_currency: str) -> Optional[dict]:
        """Real-time-ish FX rate."""
        return self._q(
            "CURRENCY_EXCHANGE_RATE",
            params={"from_currency": from_currency, "to_currency": to_currency},
            cache_key=f"av_fx_{from_currency}_{to_currency}",
            cache_ttl=600,
        )

    def federal_funds_rate(self) -> Optional[dict]:
        return self._q(
            "FEDERAL_FUNDS_RATE",
            params={"interval": "monthly"},
            cache_key="av_fed_funds",
            cache_ttl=24 * 3600,
        )
