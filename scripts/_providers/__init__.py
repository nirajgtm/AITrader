"""API provider clients.

Each provider wraps one external service. All providers:
  - Inherit from BaseProvider (rate limit, cache, http retry).
  - Use _apikeys.get_key() for auth.
  - Cache responses through _cache.
  - Expose narrow methods that return parsed dicts/lists.

Usage:
    from _providers import finnhub, fmp, alphavantage, massive
    fh = finnhub()
    quote = fh.quote("SPY")

CLI:
    python3 _providers --status        # show key presence + per-process call counts
"""
from __future__ import annotations

from ._base import BaseProvider, RateLimitExceeded  # noqa: F401


def alphavantage():
    from .alphavantage import AlphaVantage
    return AlphaVantage()


def finnhub():
    from .finnhub import Finnhub
    return Finnhub()


def fmp():
    from .fmp import FMP
    return FMP()


def massive():
    from .massive import Massive
    return Massive()


def all_providers() -> list[BaseProvider]:
    return [alphavantage(), finnhub(), fmp(), massive()]


def status() -> list[dict]:
    return [p.info() for p in all_providers()]


if __name__ == "__main__":
    import json
    import sys
    if "--status" in sys.argv or len(sys.argv) == 1:
        for info in status():
            print(f"\n[{info['provider']}]")
            for k, v in info.items():
                if k == "provider":
                    continue
                print(f"  {k}: {v}")
