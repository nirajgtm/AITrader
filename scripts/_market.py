"""Bulk market data fetch via yfinance.

Replaces per-ticker `.history()` calls with one `yf.download(...)` call. Massive
speedup when many tickers are needed at once (regime, sectors, breadth).

Design:
  - One bulk fetch per (sorted-tickers, period) tuple.
  - Result cached as a dict[ticker -> compact DataFrame] in _cache.
  - Helper `with_indicators` adds MA/RSI/ATR locally (no extra network).

Public API:
  fetch_bulk(tickers, period="1y", interval="1d") -> dict[ticker, DataFrame]
  with_indicators(df) -> DataFrame                # adds MA20/50/200, RSI14, ATR14
  rsi(series, period=14) -> Series
  atr(df, period=14) -> Series

The DataFrames have columns: Open, High, Low, Close, Volume (no AdjClose).
"""
from __future__ import annotations

import hashlib
from typing import Iterable, Optional

import numpy as np
import pandas as pd
import yfinance as yf

from _cache import cache_get, cache_put

# TTL: 10min for short periods (intraday-ish), 30min for longer.
_TTL_BY_PERIOD = {
    "5d": 300, "1mo": 600, "3mo": 600, "6mo": 1200,
    "1y": 1200, "2y": 1800, "5y": 3600, "max": 3600,
}


def _cache_key(tickers: list[str], period: str, interval: str) -> str:
    sym_hash = hashlib.sha1(",".join(sorted(tickers)).encode()).hexdigest()[:10]
    return f"market_bulk_{sym_hash}_{period}_{interval}"


def _ttl(period: str) -> int:
    return _TTL_BY_PERIOD.get(period, 900)


# Map yfinance period strings to public.com history periods (daily aggregation).
_PERIOD_TO_PUBLIC = {
    "5d": "WEEK", "1mo": "MONTH", "3mo": "QUARTER", "6mo": "HALF_YEAR",
    "1y": "YEAR", "2y": "FIVE_YEARS", "5y": "FIVE_YEARS", "max": "FIVE_YEARS",
    "ytd": "YTD",
}


def _serialize_bulk(result: dict, keep_rows: int) -> dict:
    """DataFrames -> JSON-cacheable {ticker: {index, data}} (rehydrated on read)."""
    payload = {}
    for tk, df in result.items():
        slim = df.tail(keep_rows)
        payload[tk] = {
            "index": [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in slim.index],
            "data": {col: slim[col].tolist() for col in slim.columns},
        }
    return payload


def _fetch_bulk_public(tickers: list[str], period: str, interval: str,
                       keep_rows: int) -> dict[str, pd.DataFrame]:
    """Primary source: per-ticker daily OHLCV from public.com (real-time, globally
    throttled to 10 req/s inside the client). Daily interval only. Returns {} so the
    caller falls back to yfinance if public.com can't serve the request OR covers
    <90% of the tickers (so a scan is never silently skewed by missing names)."""
    if interval not in ("1d", "1day"):
        return {}
    pub_period = _PERIOD_TO_PUBLIC.get(period)
    if pub_period is None:
        return {}
    try:
        import publicdotcom_api as pub
    except Exception:
        return {}
    out: dict[str, pd.DataFrame] = {}
    for tk in tickers:
        try:
            bars = pub.get_daily_ohlcv(tk, pub_period)
        except Exception:
            bars = []
        if len(bars) < 2:
            continue
        df = pd.DataFrame(bars)
        df.index = pd.to_datetime(df["date"])
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                "close": "Close", "volume": "Volume"})
        out[tk] = df[["Open", "High", "Low", "Close", "Volume"]].tail(keep_rows)
    if out and len(out) >= 0.9 * len(tickers):
        return out
    return {}


def fetch_bulk(tickers: Iterable[str], period: str = "1y",
               interval: str = "1d", auto_adjust: bool = False,
               keep_rows: int = 260) -> dict[str, pd.DataFrame]:
    """Fetch OHLCV for many tickers in a single yfinance call.

    Returns dict[ticker, DataFrame] where each DataFrame has columns
    Open, High, Low, Close, Volume.

    Cached for `_ttl(period)` seconds. `keep_rows` limits cached data per
    ticker (260 ≈ 1 trading year — plenty for 200MA).
    """
    tickers = sorted({t.upper() for t in tickers})
    if not tickers:
        return {}

    ck = _cache_key(tickers, period, interval)
    cached = cache_get(ck, ttl_seconds=_ttl(period))
    if cached is not None:
        out: dict[str, pd.DataFrame] = {}
        for tk, payload in cached.items():
            try:
                if isinstance(payload, dict) and "index" in payload and "data" in payload:
                    df = pd.DataFrame(payload["data"])
                    df.index = pd.to_datetime(payload["index"])
                else:
                    # legacy shape
                    df = pd.DataFrame(payload)
                    df.index = pd.to_datetime(df.index)
                out[tk] = df
            except Exception:
                continue
        return out

    # Primary source: public.com (real-time per-ticker daily bars, throttled to
    # 10 req/s globally). Falls back to the yfinance batch below if it can't serve
    # this request or covers <90% of the tickers.
    pub_result = _fetch_bulk_public(tickers, period, interval, keep_rows)
    if pub_result:
        cache_put(ck, _serialize_bulk(pub_result, keep_rows))
        return pub_result

    # yfinance batch download (fallback)
    try:
        data = yf.download(
            tickers=tickers,
            period=period,
            interval=interval,
            auto_adjust=auto_adjust,
            group_by="ticker",
            progress=False,
            threads=True,
        )
    except Exception:
        # Last-resort: per-ticker fallback (preserves correctness)
        result = {}
        for tk in tickers:
            try:
                df = yf.Ticker(tk).history(period=period, interval=interval, auto_adjust=auto_adjust)
                if not df.empty:
                    result[tk] = df[["Open", "High", "Low", "Close", "Volume"]]
            except Exception:
                continue
        return result

    # Multi-ticker yfinance returns columns: MultiIndex (ticker, OHLCV)
    # OR (OHLCV, ticker). Handle both.
    result: dict[str, pd.DataFrame] = {}
    if isinstance(data.columns, pd.MultiIndex):
        # Determine level order
        if data.columns.nlevels >= 2:
            # Try (ticker, field) first
            level0_vals = set(data.columns.get_level_values(0).unique())
            level1_vals = set(data.columns.get_level_values(1).unique())
            ticker_set = set(tickers)
            ohlcv = {"Open", "High", "Low", "Close", "Volume", "Adj Close"}

            if ticker_set.issubset(level0_vals):
                # group_by="ticker" → (ticker, field)
                for tk in tickers:
                    if tk in level0_vals:
                        sub = data[tk].dropna(how="all")
                        if not sub.empty and "Close" in sub.columns:
                            cols = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in sub.columns]
                            result[tk] = sub[cols].copy()
            elif level0_vals.issubset(ohlcv):
                # (field, ticker)
                for tk in tickers:
                    try:
                        sub = pd.DataFrame({
                            "Open": data["Open"][tk],
                            "High": data["High"][tk],
                            "Low": data["Low"][tk],
                            "Close": data["Close"][tk],
                            "Volume": data["Volume"][tk],
                        }).dropna(how="all")
                        if not sub.empty:
                            result[tk] = sub
                    except Exception:
                        continue
    else:
        # Single ticker: data.columns are OHLCV directly
        if len(tickers) == 1 and not data.empty:
            tk = tickers[0]
            cols = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in data.columns]
            result[tk] = data[cols].copy()

    # Trim to keep_rows + cache. JSON dict keys must be strings, so we serialize
    # each DataFrame as {"index": [iso_dates], "data": {col: [vals]}} and
    # rehydrate on read.
    payload = {}
    for tk, df in result.items():
        slim = df.tail(keep_rows)
        payload[tk] = {
            "index": [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in slim.index],
            "data": {col: slim[col].tolist() for col in slim.columns},
        }
    cache_put(ck, payload)
    return result


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0).rolling(period).mean()
    down = (-delta.clip(upper=0)).rolling(period).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def with_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add MA20/50/200, RSI14, ATR14 to a single-ticker DataFrame in-place-style.

    Returns a new DataFrame to avoid SettingWithCopyWarning.
    """
    out = df.copy()
    out["MA20"] = out["Close"].rolling(20).mean()
    out["MA50"] = out["Close"].rolling(50).mean()
    out["MA200"] = out["Close"].rolling(200).mean()
    out["RSI14"] = rsi(out["Close"], 14)
    out["ATR14"] = atr(out, 14)
    return out


def ret_pct(df: pd.DataFrame, n: int) -> float:
    """N-day return % using Close. NaN if insufficient history."""
    if len(df) < n + 1:
        return float("nan")
    return float((df["Close"].iloc[-1] / df["Close"].iloc[-n - 1] - 1) * 100)


def vs_ma(close: float, ma: float) -> str:
    if pd.isna(ma):
        return "?"
    return "above" if close > ma else "below"
