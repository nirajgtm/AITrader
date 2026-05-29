"""Financial Modeling Prep (FMP) API client — stable v2 surface.

Free tier: 250 requests/day. Cache 6-12h on most calls.

Confirmed-working free endpoints (probed 2026-04-25):
  - /economic-calendar  — country-tagged event calendar with date/time/impact/previous/estimate/actual
  - /earnings-calendar  — earnings calendar with date/EPS-est/actual
  - /earnings           — per-symbol earnings history (with ?symbol=)
  - /sp500-constituent  — full S&P 500 list w/ sector
  - /nasdaq-constituent — Nasdaq 100 list w/ sector
  - /treasury-rates     — full treasury curve, daily
  - /biggest-gainers    — top movers up
  - /biggest-losers     — top movers down
  - /most-actives       — most active by volume
  - /analyst-estimates  — per-symbol forward estimates (with ?symbol=)
  - /quote              — per-symbol quote (with ?symbol=)
  - /profile            — per-symbol fundamentals (with ?symbol=)

Paid-tier (avoid):
  - /insider-trading, /senate-trading, /house-disclosure, /historical-rating, /sector-performance-snapshot

Docs: https://site.financialmodelingprep.com/developer/docs/stable
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from ._base import BaseProvider


class FMP(BaseProvider):
    name = "fmp"
    base_url = "https://financialmodelingprep.com/stable"
    rate_limit_per_minute = None
    rate_limit_per_day = 240  # leave headroom under 250
    api_key_env = "FMP_API_KEY"
    auth_method = "query"
    auth_param = "apikey"

    # ---------- macro ----------

    def economic_calendar(self, days_ahead: int = 14, days_back: int = 0,
                          country: str = "US", impact_min: str = "Medium") -> Optional[list[dict]]:
        """Filtered economic calendar.

        FMP returns global events. We default to US + Medium/High impact to keep
        signal tight. Pass country=None to disable filtering.
        impact_min in {"Low","Medium","High"}.
        """
        today = date.today()
        params = {
            "from": (today - timedelta(days=days_back)).isoformat(),
            "to": (today + timedelta(days=days_ahead)).isoformat(),
        }
        ck = f"econ_cal_{params['from']}_{params['to']}"
        data = self.get("economic-calendar", params=params,
                        cache_key=ck, cache_ttl=12 * 3600)
        if not isinstance(data, list):
            return data

        # Filter
        if country:
            data = [e for e in data if e.get("country") == country]
        if impact_min:
            order = {"Low": 0, "Medium": 1, "High": 2, "None": -1}
            min_rank = order.get(impact_min, 1)
            data = [e for e in data
                    if order.get(e.get("impact", "None"), -1) >= min_rank]
        return data

    def treasury_rates(self) -> Optional[list[dict]]:
        """Daily treasury yields across the curve (1m through 30y)."""
        return self.get("treasury-rates", params={},
                        cache_key="treasury_rates", cache_ttl=12 * 3600)

    # ---------- earnings ----------

    def earnings_calendar(self, days_ahead: int = 14,
                          days_back: int = 0) -> Optional[list[dict]]:
        """Universe-wide earnings calendar.

        Returns: symbol, date, epsActual, epsEstimate, revenueActual, revenueEstimate.
        """
        today = date.today()
        params = {
            "from": (today - timedelta(days=days_back)).isoformat(),
            "to": (today + timedelta(days=days_ahead)).isoformat(),
        }
        ck = f"fmp_earnings_cal_{params['from']}_{params['to']}"
        return self.get("earnings-calendar", params=params,
                        cache_key=ck, cache_ttl=12 * 3600)

    def earnings(self, symbol: str) -> Optional[list[dict]]:
        """Per-symbol earnings history."""
        return self.get("earnings", params={"symbol": symbol.upper()},
                        cache_key=f"fmp_earnings_{symbol.upper()}",
                        cache_ttl=24 * 3600)

    def analyst_estimates(self, symbol: str) -> Optional[list[dict]]:
        """Per-symbol forward analyst estimates."""
        return self.get("analyst-estimates", params={"symbol": symbol.upper()},
                        cache_key=f"fmp_estimates_{symbol.upper()}",
                        cache_ttl=24 * 3600)

    # ---------- universe ----------

    def sp500_constituents(self) -> Optional[list[dict]]:
        return self.get("sp500-constituent", params={},
                        cache_key="sp500_constituents",
                        cache_ttl=7 * 24 * 3600)

    def nasdaq100_constituents(self) -> Optional[list[dict]]:
        return self.get("nasdaq-constituent", params={},
                        cache_key="nasdaq100_constituents",
                        cache_ttl=7 * 24 * 3600)

    # ---------- movers ----------

    def biggest_gainers(self) -> Optional[list[dict]]:
        return self.get("biggest-gainers", params={},
                        cache_key="fmp_gainers", cache_ttl=600)

    def biggest_losers(self) -> Optional[list[dict]]:
        return self.get("biggest-losers", params={},
                        cache_key="fmp_losers", cache_ttl=600)

    def most_actives(self) -> Optional[list[dict]]:
        return self.get("most-actives", params={},
                        cache_key="fmp_actives", cache_ttl=600)

    # ---------- quote / profile ----------

    def quote(self, symbol: str) -> Optional[dict]:
        """Snapshot quote: price, change, volume, day range, 52w range, market cap, PE, EPS."""
        data = self.get("quote", params={"symbol": symbol.upper()},
                        cache_key=f"fmp_quote_{symbol.upper()}", cache_ttl=300)
        if isinstance(data, list) and data:
            return data[0]
        return data

    def profile(self, symbol: str) -> Optional[dict]:
        """Fundamentals snapshot: market cap, beta, range, sector, industry, employees, etc."""
        data = self.get("profile", params={"symbol": symbol.upper()},
                        cache_key=f"fmp_profile_{symbol.upper()}",
                        cache_ttl=24 * 3600)
        if isinstance(data, list) and data:
            return data[0]
        return data
