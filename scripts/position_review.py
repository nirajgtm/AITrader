#!/usr/bin/env python3
"""Deterministic open-position review.

For each open position, computes structured ACT/HOLD/EXIT recommendation based
on rules — no LLM judgment needed. Output is a single JSON dict that the
runbook digest forwards to Claude.

Rules applied per position:
  - horizon_expires_at <= today               → EXIT_HORIZON
  - last_price <= stop * 1.005 (proximity)    → STOP_NEAR
  - last_price >= target * 0.995 (proximity)  → TARGET_NEAR
  - earnings within imminent blackout window   → EXIT_EARNINGS_BLACKOUT
  - FOMC within imminent window (index-direction trades only)
                                              → EXIT_FOMC_BLACKOUT
  - drawdown on position > 10% AND time elapsed > 5d
                                              → CONSIDER_REDUCE
  - invalidation field present + condition triggered (price-level invalidation only)
                                              → EXIT_INVALIDATED
  - else                                      → HOLD

Outputs JSON only (no prose). Exit code 0 even on alerts — runbook decides.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from _common import load_portfolio  # noqa: E402
from _cache import cache_get, cache_put  # noqa: E402
from _terse import emit, step_result  # noqa: E402
import publicdotcom_api as pub  # noqa: E402  (primary real-time source)

MEGA_CAPS = ["NVDA", "AAPL", "MSFT", "META", "AMZN", "GOOGL", "TSLA", "AMD", "AVGO", "NFLX"]
MEGA_CAP_SENSITIVE = {"SPY", "QQQ", "IWM", "DIA", "SQQQ", "SPXS", "TQQQ", "SPXL",
                      "XLK", "XLC", "XLY"}

# Static FOMC dates for 2026 (kept in sync with macro.py)
FOMC_2026 = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
             "2026-07-29", "2026-09-16", "2026-11-04", "2026-12-09"]

# An earnings/FOMC "blackout" only matters when the binary event is IMMINENT, not
# anywhere in a multi-month horizon. Without this cap a long-horizon hold (e.g. a
# 6-18mo reaccumulation) false-flags EXIT_EARNINGS_BLACKOUT for a print 60+ days
# out, and EXIT_FOMC_BLACKOUT always (there's an FOMC every ~6 weeks). Cap the
# look-ahead at this many days; the separate 2-day auto-blackout is the hard gate.
BLACKOUT_WINDOW_DAYS = 7


def _history_df(ticker: str):
    """1y daily OHLCV DataFrame from public.com (primary) or yfinance (fallback)."""
    try:
        bars = pub.get_daily_ohlcv(ticker, "YEAR")
        if len(bars) >= 20:
            df = pd.DataFrame(bars).rename(columns={
                "open": "Open", "high": "High", "low": "Low",
                "close": "Close", "volume": "Volume"})
            return df
    except Exception:
        pass
    try:
        hist = yf.Ticker(ticker).history(period="1y", auto_adjust=False)
        if not hist.empty:
            return hist
    except Exception:
        pass
    return None


def _last_price(ticker: str) -> float | None:
    cache_key = f"price_{ticker.upper()}"
    cached = cache_get(cache_key, ttl_seconds=300)
    if cached is not None:
        return float(cached)
    px = None
    # Primary: public.com real-time last
    try:
        q = pub.get_quote(ticker)
        if q and q.get("last") is not None:
            px = float(q["last"])
    except Exception:
        px = None
    # Fallback: yfinance
    if px is None:
        try:
            h = yf.Ticker(ticker).history(period="1d")
            if not h.empty:
                px = float(h["Close"].iloc[-1])
        except Exception:
            px = None
    if px is not None:
        cache_put(cache_key, px)
    return px


def _technicals(ticker: str) -> dict | None:
    """1y history + RSI(14) + MA20/50/200 + ATR(14) + 52w distance, cached 10min."""
    cache_key = f"tech_{ticker.upper()}"
    cached = cache_get(cache_key, ttl_seconds=600)
    if cached is not None:
        return cached
    hist = _history_df(ticker)
    if hist is None or len(hist) < 20:
        return None
    close = hist["Close"]
    high = hist["High"]
    low = hist["Low"]
    last_close = float(close.iloc[-1])
    ma20 = float(close.rolling(20).mean().iloc[-1])
    ma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    ma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
    delta = close.diff()
    up = delta.clip(lower=0).rolling(14).mean()
    down = (-delta.clip(upper=0)).rolling(14).mean()
    rs = up / down.replace(0, float("nan"))
    rsi_series = 100 - (100 / (1 + rs))
    rsi14 = float(rsi_series.iloc[-1]) if not pd.isna(rsi_series.iloc[-1]) else None
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr_series = tr.rolling(14).mean()
    atr14 = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else None
    hi_52w = float(close.max())
    result = {
        "close": round(last_close, 2),
        "rsi14": round(rsi14, 1) if rsi14 is not None else None,
        "ma20": round(ma20, 2) if ma20 else None,
        "ma50": round(ma50, 2) if ma50 else None,
        "ma200": round(ma200, 2) if ma200 else None,
        "atr14": round(atr14, 2) if atr14 else None,
        "dist_from_ma20_pct": round((last_close - ma20) / ma20 * 100, 2) if ma20 else None,
        "pct_from_52w_high": round((last_close - hi_52w) / hi_52w * 100, 2),
        "high_52w": round(hi_52w, 2),
    }
    cache_put(cache_key, result)
    return result


def _next_earnings(ticker: str) -> tuple[str | None, int | None]:
    """Returns (next_er_iso, days_to_er). Reuses the existing _earnings_<TKR> cache."""
    cache_key = f"earnings_{ticker.upper()}"
    cached = cache_get(cache_key, ttl_seconds=12 * 3600)
    if cached is not None:
        next_er = cached.get("next_er")
    else:
        try:
            tk = yf.Ticker(ticker)
            dates = tk.get_earnings_dates(limit=4)
            today = date.today()
            if dates is None or dates.empty:
                cache_put(cache_key, {"next_er": None})
                return None, None
            future = dates[dates.index.date > today] if hasattr(dates.index, "date") else dates
            if future.empty:
                cache_put(cache_key, {"next_er": None})
                return None, None
            next_dt = future.index[0].date() if hasattr(future.index[0], "date") else None
            next_er = next_dt.isoformat() if next_dt else None
            cache_put(cache_key, {"next_er": next_er})
        except Exception:
            return None, None
    if not next_er:
        return None, None
    try:
        days_out = (date.fromisoformat(next_er) - date.today()).days
    except Exception:
        return next_er, None
    return next_er, days_out


_OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def _parse_occ(symbol: str) -> dict | None:
    """OCC option symbol → {underlying, expiry, right, strike}.

    Format: TICKER + YYMMDD + C|P + 8-digit strike (thousandths of a dollar).
    """
    m = _OCC_RE.match((symbol or "").strip().upper())
    if not m:
        return None
    underlying, ymd, right, strike_raw = m.groups()
    yy, mm, dd = ymd[:2], ymd[2:4], ymd[4:6]
    return {
        "underlying": underlying,
        "expiry": f"20{yy}-{mm}-{dd}",
        "right": right,
        "strike": int(strike_raw) / 1000.0,
    }


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-x * x / 2.0)


def _bs_greeks(S: float, K: float, T: float, r: float, sigma: float, right: str) -> dict | None:
    """Black-Scholes Greeks. T in years, r/sigma annualized. theta is per-day; vega is per-1%-IV."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    if right == "C":
        delta = _norm_cdf(d1)
        theta = (-S * _norm_pdf(d1) * sigma / (2 * sqrt_T)
                 - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (-S * _norm_pdf(d1) * sigma / (2 * sqrt_T)
                 + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365.0
    gamma = _norm_pdf(d1) / (S * sigma * sqrt_T)
    vega = S * _norm_pdf(d1) * sqrt_T / 100.0
    return {
        "delta": round(delta, 3),
        "gamma": round(gamma, 4),
        "vega": round(vega, 3),
        "theta": round(theta, 3),
    }


def _option_meta(option_symbol: str, underlying_close: float | None) -> dict | None:
    """Pull bid/ask/IV/OI from yfinance options chain + compute Greeks via Black-Scholes."""
    parsed = _parse_occ(option_symbol)
    if not parsed:
        return None
    cache_key = f"opt_meta_{option_symbol}"
    cached = cache_get(cache_key, ttl_seconds=600)
    if cached is not None:
        chain = cached
    else:
        try:
            tk = yf.Ticker(parsed["underlying"])
            ch = tk.option_chain(parsed["expiry"])
            df = ch.calls if parsed["right"] == "C" else ch.puts
            match = df[df["strike"] == parsed["strike"]]
            if match.empty:
                cache_put(cache_key, {})
                return None
            row = match.iloc[0]
            bid = float(row.get("bid", 0) or 0)
            ask = float(row.get("ask", 0) or 0)
            last = float(row.get("lastPrice", 0) or 0)
            mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else last
            iv = float(row.get("impliedVolatility", 0) or 0)
            oi = int(row.get("openInterest", 0) or 0)
            chain = {
                "bid": round(bid, 2),
                "ask": round(ask, 2),
                "mid": round(mid, 2),
                "last": round(last, 2),
                "iv": round(iv, 4),
                "open_interest": oi,
            }
            cache_put(cache_key, chain)
        except Exception:
            return None
    if not chain:
        return None
    try:
        days_to_exp = (date.fromisoformat(parsed["expiry"]) - date.today()).days
    except Exception:
        days_to_exp = None
    greeks = None
    if days_to_exp is not None and days_to_exp > 0 and underlying_close and chain.get("iv"):
        T = days_to_exp / 365.0
        greeks = _bs_greeks(underlying_close, parsed["strike"], T, 0.04, chain["iv"], parsed["right"])
    return {
        "underlying": parsed["underlying"],
        "expiry": parsed["expiry"],
        "right": parsed["right"],
        "strike": parsed["strike"],
        "days_to_expiry": days_to_exp,
        **chain,
        "greeks": greeks,
    }


def _earnings_in_window(ticker: str, days: int) -> tuple[bool, str | None]:
    cache_key = f"earnings_{ticker.upper()}"
    cached = cache_get(cache_key, ttl_seconds=12 * 3600)
    if cached is not None:
        next_er = cached.get("next_er")
    else:
        try:
            tk = yf.Ticker(ticker)
            dates = tk.get_earnings_dates(limit=4)
            today = date.today()
            if dates is None or dates.empty:
                cache_put(cache_key, {"next_er": None})
                return False, None
            future = dates[dates.index.date > today] if hasattr(dates.index, "date") else dates
            if future.empty:
                cache_put(cache_key, {"next_er": None})
                return False, None
            next_dt = future.index[0].date() if hasattr(future.index[0], "date") else None
            next_er = next_dt.isoformat() if next_dt else None
            cache_put(cache_key, {"next_er": next_er})
        except Exception:
            return False, None
    if not next_er:
        return False, None
    next_dt = date.fromisoformat(next_er)
    days_out = (next_dt - date.today()).days
    return (0 <= days_out <= days), next_er


def _mega_cap_earnings_in_window(days: int) -> list[str]:
    triggered = []
    for mc in MEGA_CAPS:
        has, _ = _earnings_in_window(mc, days)
        if has:
            triggered.append(mc)
    return triggered


def _fomc_in_window(days: int) -> str | None:
    today = date.today()
    cutoff = today + timedelta(days=days)
    for d in FOMC_2026:
        dt = date.fromisoformat(d)
        if today <= dt <= cutoff:
            return d
    return None


def review_position(pos: dict) -> dict:
    """Return a structured action dict for one position."""
    today = date.today()
    ticker = pos["ticker"]
    is_option = pos.get("kind") == "option"
    side = pos.get("side", "LONG")

    # Days remaining in horizon
    horizon_expires = pos.get("horizon_expires_at")
    days_remaining = None
    if horizon_expires:
        try:
            days_remaining = (date.fromisoformat(horizon_expires) - today).days
        except Exception:
            pass

    # For options, the underlying price drives stop/target proximity checks
    # (typical convention: "close the option if the underlying breaks $S"),
    # while option premium drives unrealized P&L. Stocks use the same price for both.
    tech = _technicals(ticker)
    underlying_last = tech["close"] if tech else _last_price(ticker)
    if is_option:
        mark = pos.get("last_mark")
        pnl_basis_last = float(mark) if mark is not None else float(pos["entry"])
        last = underlying_last if underlying_last is not None else float(pos["entry"])
    else:
        last = underlying_last if underlying_last is not None else float(pos["entry"])
        pnl_basis_last = last
    entry = float(pos["entry"])
    stop = float(pos["stop"]) if pos.get("stop") else None
    target = float(pos["target"]) if pos.get("target") else None

    # P&L (premium-to-premium for options; price-to-price for stocks)
    side_sign = 1 if side == "LONG" else -1
    pnl_pct = ((pnl_basis_last - entry) / entry * 100) * side_sign

    # Days in trade
    opened_at = pos.get("opened_at")
    days_in_trade = None
    if opened_at:
        try:
            days_in_trade = (today - date.fromisoformat(opened_at)).days
        except Exception:
            pass

    # Decision tree (first match wins)
    actions: list[str] = []
    reasons: list[str] = []

    if days_remaining is not None and days_remaining <= 0:
        actions.append("EXIT_HORIZON")
        reasons.append(f"horizon_expires_at={horizon_expires} reached")
    elif days_remaining is not None and days_remaining <= 2:
        actions.append("HORIZON_EXPIRING_SOON")
        reasons.append(f"horizon_expires_at={horizon_expires} in {days_remaining}d")

    if stop and side == "LONG" and last <= stop * 1.005:
        actions.append("STOP_NEAR")
        reasons.append(f"price {last:.2f} within 0.5% of stop {stop:.2f}")
    if stop and side == "SHORT" and last >= stop * 0.995:
        actions.append("STOP_NEAR")
        reasons.append(f"price {last:.2f} within 0.5% of stop {stop:.2f}")

    if target and side == "LONG" and last >= target * 0.995:
        actions.append("TARGET_NEAR")
        reasons.append(f"price {last:.2f} within 0.5% of target {target:.2f}")
    if target and side == "SHORT" and last <= target * 1.005:
        actions.append("TARGET_NEAR")
        reasons.append(f"price {last:.2f} within 0.5% of target {target:.2f}")

    # Earnings/FOMC blackout — only when the event is imminent (capped window),
    # not anywhere in a multi-month horizon.
    if days_remaining is not None and days_remaining > 0:
        check_days = min(days_remaining, BLACKOUT_WINDOW_DAYS)
        if ticker in MEGA_CAP_SENSITIVE:
            triggered = _mega_cap_earnings_in_window(check_days)
            if triggered and not is_option:
                actions.append("EXIT_EARNINGS_BLACKOUT")
                reasons.append(f"mega-cap earnings in {check_days}d: {','.join(triggered)}")
        else:
            has, when = _earnings_in_window(ticker, check_days)
            if has and not is_option:
                actions.append("EXIT_EARNINGS_BLACKOUT")
                reasons.append(f"earnings on {when} within {check_days}d horizon")

        # FOMC overlay for index-direction trades
        if ticker in MEGA_CAP_SENSITIVE:
            fomc_d = _fomc_in_window(check_days)
            if fomc_d:
                actions.append("EXIT_FOMC_BLACKOUT")
                reasons.append(f"FOMC on {fomc_d} within {check_days}d horizon")

    # Drawdown + time
    if pnl_pct <= -10 and (days_in_trade or 0) >= 5:
        actions.append("CONSIDER_REDUCE")
        reasons.append(f"unrealized -{abs(pnl_pct):.1f}% over {days_in_trade}d")

    # Per-position next-ER (always pulled, even outside the horizon window)
    next_er, days_to_er = _next_earnings(ticker) if not is_option else (None, None)
    if (days_to_er is not None and 0 <= days_to_er <= 2 and not is_option
            and "EXIT_EARNINGS_BLACKOUT" not in actions
            and "EARNINGS_BLACKOUT" not in actions):
        actions.append("EARNINGS_BLACKOUT")
        reasons.append(f"earnings on {next_er} in {days_to_er}d (auto-blackout)")

    # Option meta (Greeks/IV/OI) for option positions
    option_meta = None
    if is_option:
        opt_sym = pos.get("option_symbol")
        if opt_sym:
            option_meta = _option_meta(opt_sym, last)

    # Invalidation: thesis-level invalidation is text — surface to Claude, not auto-act
    invalidation = pos.get("invalidation")

    primary_action = actions[0] if actions else "HOLD"

    out = {
        "ticker": ticker,
        "side": side,
        "kind": pos.get("kind", "stock"),
        "entry": entry,
        "stop": stop,
        "target": target,
        "last": round(last, 2),
        "pnl_pct": round(pnl_pct, 2),
        "days_in_trade": days_in_trade,
        "horizon_expires_at": horizon_expires,
        "days_remaining": days_remaining,
        "primary_action": primary_action,
        "all_actions": actions,
        "reasons": reasons,
        "invalidation": invalidation,
        "strategy": pos.get("strategy"),
        "next_er_date": next_er,
        "days_to_er": days_to_er,
    }
    if tech:
        out["technicals"] = {
            "rsi14": tech.get("rsi14"),
            "ma20": tech.get("ma20"),
            "ma50": tech.get("ma50"),
            "ma200": tech.get("ma200"),
            "atr14": tech.get("atr14"),
            "dist_from_ma20_pct": tech.get("dist_from_ma20_pct"),
            "pct_from_52w_high": tech.get("pct_from_52w_high"),
            "high_52w": tech.get("high_52w"),
        }
    if option_meta:
        out["option_meta"] = option_meta
    try:
        from ticker_lessons import load_lessons
        lessons = load_lessons(ticker, n=3)
        if lessons:
            out["lessons"] = lessons
    except Exception:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--portfolio-id", default="primary",
                    help="Portfolio id to review. 'primary' (default) reads "
                         "state/portfolio.json. Other ids read "
                         "state/portfolios/<id>.json.")
    args = ap.parse_args()

    p = load_portfolio(args.portfolio_id)
    challenge_positions = p.get("positions", [])
    user_positions = p.get("user_positions", [])
    challenge_reviews = [review_position(pos) for pos in challenge_positions]
    user_reviews = [review_position(pos) for pos in user_positions]
    reviews = challenge_reviews + user_reviews

    # Tag each review with which book it came from (for downstream filtering).
    for r in challenge_reviews:
        r["book"] = "challenge"
    for r in user_reviews:
        r["book"] = "user"

    # Headline + flags
    actionable = [r for r in reviews if r["primary_action"] != "HOLD"]
    flags = []
    for r in reviews:
        for a in r["all_actions"]:
            flags.append(f"{r['ticker'].lower()}_{a.lower()}")

    headline = (f"{len(reviews)} open ({len(challenge_reviews)} challenge, "
                f"{len(user_reviews)} user); {len(actionable)} action(s) needed")
    if actionable:
        headline += "; " + ", ".join(f"{r['ticker']}={r['primary_action']}" for r in actionable)

    result = step_result("position_review", ok=True, headline=headline,
                         data={"reviews": reviews,
                               "challenge_reviews": challenge_reviews,
                               "user_reviews": user_reviews},
                         flags=flags)
    if args.json:
        emit(result)
    else:
        print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
