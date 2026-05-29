"""Black-Scholes pricing helpers for LEAP backtest.

Why this exists: yfinance and Polygon free tiers don't have multi-year
historical option chains. To backtest LEAP strategies we synthesize option
prices from underlying spot + assumed IV via Black-Scholes. Accuracy is
"good enough for strategy comparison" not "matches the real market mid."

Conventions:
  - All times are in years.
  - Rates are annualized continuous compounding (5% = 0.05).
  - IV is annualized standard deviation (20% = 0.20).
  - Prices and strikes are in the same units as the underlying.
"""
from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    """Standard normal CDF using erf. Matches scipy.stats.norm.cdf to 1e-12."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_call_price(spot: float, strike: float, ttm: float,
                  iv: float, rate: float = 0.045, div: float = 0.0) -> float:
    """Black-Scholes call price. ttm in years, iv annualized."""
    if ttm <= 0:
        return max(0.0, spot - strike)
    if iv <= 0:
        # Limit: deterministic forward price discounted
        fwd = spot * math.exp((rate - div) * ttm)
        return max(0.0, math.exp(-rate * ttm) * (fwd - strike))
    d1 = (math.log(spot / strike) + (rate - div + 0.5 * iv * iv) * ttm) / (iv * math.sqrt(ttm))
    d2 = d1 - iv * math.sqrt(ttm)
    return spot * math.exp(-div * ttm) * _norm_cdf(d1) - strike * math.exp(-rate * ttm) * _norm_cdf(d2)


def bs_call_delta(spot: float, strike: float, ttm: float,
                  iv: float, rate: float = 0.045, div: float = 0.0) -> float:
    if ttm <= 0:
        return 1.0 if spot > strike else 0.0
    if iv <= 0:
        return 1.0 if spot * math.exp((rate - div) * ttm) > strike else 0.0
    d1 = (math.log(spot / strike) + (rate - div + 0.5 * iv * iv) * ttm) / (iv * math.sqrt(ttm))
    return math.exp(-div * ttm) * _norm_cdf(d1)


def strike_for_delta(spot: float, ttm: float, target_delta: float,
                     iv: float, rate: float = 0.045, div: float = 0.0,
                     tol: float = 1e-4, max_iter: int = 60) -> float:
    """Bisection: find the strike that gives `target_delta` for a call with
    these params. For a long call, delta is monotonic in strike (lower strike
    -> higher delta), so bisection converges quickly.
    """
    lo, hi = spot * 0.05, spot * 5.0
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        d = bs_call_delta(spot, mid, ttm, iv, rate, div)
        if abs(d - target_delta) < tol:
            return mid
        if d > target_delta:
            lo = mid  # raise strike to lower delta
        else:
            hi = mid
    return 0.5 * (lo + hi)
