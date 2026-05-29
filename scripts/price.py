#!/usr/bin/env python3
"""Quote + OHLC + moving averages + RSI(14) + ATR(14) for a ticker.

PRIMARY data source is public.com (real-time exchange quotes + daily bars). If
public.com is unavailable for the ticker, we fall back to yfinance (~15-min
delayed) so nothing breaks. The reported `close` is the real-time last price when
available; the moving averages / RSI / ATR are computed from the daily close
series. Every result is tagged with `source` so consumers know what they got.

Usage:
  price.py TICKER [--days 60] [--json]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

import numpy as np
import pandas as pd
import yfinance as yf

OHLCV = ["Open", "High", "Low", "Close", "Volume"]


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0).rolling(period).mean()
    down = (-delta.clip(upper=0)).rolling(period).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def _load_history(ticker: str):
    """Return (df[OHLCV], realtime_quote_dict, source) or (None, None, None).

    Tries public.com first (real-time), then yfinance. Never raises -- a failure
    in the primary source silently falls through to the backup.
    """
    # Primary: public.com
    try:
        import publicdotcom_api as pub
        bars = pub.get_daily_ohlcv(ticker, "YEAR")
        if len(bars) >= 30:
            df = pd.DataFrame(bars)
            df.index = pd.to_datetime(df["date"])
            df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                    "close": "Close", "volume": "Volume"})
            quote = {}
            try:
                quote = pub.get_quote(ticker) or {}
            except Exception:
                quote = {}
            return df[OHLCV], quote, "public.com"
    except Exception:
        pass
    # Fallback: yfinance
    try:
        hist = yf.Ticker(ticker).history(period="1y", auto_adjust=False)
        if not hist.empty:
            return hist[OHLCV], {}, "yfinance"
    except Exception:
        pass
    return None, None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    from _terse import emit, step_result

    t = args.ticker.upper()
    hist, quote, source = _load_history(t)
    if hist is None or len(hist) < 2:
        print(f"No data for {t}", file=sys.stderr)
        return 1

    hist["MA20"] = hist["Close"].rolling(20).mean()
    hist["MA50"] = hist["Close"].rolling(50).mean()
    hist["MA200"] = hist["Close"].rolling(200).mean()
    hist["RSI14"] = rsi(hist["Close"], 14)
    hist["ATR14"] = atr(hist, 14)

    last = hist.iloc[-1]
    daily_close = float(last["Close"])

    # Reported price = real-time last when public.com gave us one; else daily close.
    rt_last = None
    last_ts = None
    if quote and quote.get("last") is not None:
        try:
            rt_last = float(quote["last"])
        except (TypeError, ValueError):
            rt_last = None
        last_ts = quote.get("lastTimestamp")
    close = rt_last if rt_last is not None else daily_close

    # Day change vs the prior session's close.
    last_date = hist.index[-1].date().isoformat()
    if last_date == date.today().isoformat() and len(hist) >= 2:
        prev_close = float(hist.iloc[-2]["Close"])
    else:
        prev_close = daily_close
    chg = close - prev_close
    chg_pct = (chg / prev_close * 100) if prev_close else 0.0

    volume = int(quote["volume"]) if quote.get("volume") is not None else int(last["Volume"])

    if args.json:
        atr_val = float(last["ATR14"]) if not pd.isna(last["ATR14"]) else None
        ma20 = float(last["MA20"]) if not pd.isna(last["MA20"]) else None
        fomo_ceiling = (ma20 + 2 * atr_val) if (ma20 and atr_val) else None
        rsi_val = float(last["RSI14"]) if not pd.isna(last["RSI14"]) else None
        flags = []
        if fomo_ceiling and close > fomo_ceiling:
            flags.append("fomo_above_2atr")
        if rsi_val is not None and rsi_val >= 75:
            flags.append("rsi_overbought")
        if rsi_val is not None and rsi_val <= 30:
            flags.append("rsi_oversold")
        emit(step_result("price", ok=True,
                         headline=f"{t} {close:.2f} ({chg_pct:+.2f}%) RSI={rsi_val:.1f} [{source}]"
                                  if rsi_val is not None else f"{t} {close:.2f} ({chg_pct:+.2f}%) [{source}]",
                         data={
                             "ticker": t,
                             "close": round(close, 2),
                             "chg_pct": round(chg_pct, 2),
                             "high": round(float(last["High"]), 2),
                             "low": round(float(last["Low"]), 2),
                             "volume": volume,
                             "ma20": round(ma20, 2) if ma20 else None,
                             "ma50": round(float(last["MA50"]), 2) if not pd.isna(last["MA50"]) else None,
                             "ma200": round(float(last["MA200"]), 2) if not pd.isna(last["MA200"]) else None,
                             "rsi14": round(rsi_val, 1) if rsi_val is not None else None,
                             "atr14": round(atr_val, 2) if atr_val else None,
                             "fomo_ceiling": round(fomo_ceiling, 2) if fomo_ceiling else None,
                             "source": source,
                             "last_timestamp": last_ts,
                         }, flags=flags))
        return 0

    print(f"=== {t} === ({last_date})  [source: {source}]")
    print(f"  Last:    ${close:.2f}  ({chg:+.2f}, {chg_pct:+.2f}%)" + (f"  rt@{last_ts}" if last_ts else ""))
    print(f"  Range:   ${float(last['Low']):.2f} - ${float(last['High']):.2f}  vol {volume:,}")
    print(f"  MAs:     20={last['MA20']:.2f}  50={last['MA50']:.2f}  200={last['MA200']:.2f}")

    trend = [">20MA" if close > last["MA20"] else "<20MA"]
    if close > last["MA50"]:
        trend.append(">50MA")
    if close > last["MA200"]:
        trend.append(">200MA")
    print(f"  Trend:   {' '.join(trend)}")
    print(f"  RSI14:   {last['RSI14']:.1f}")
    print(f"  ATR14:   {last['ATR14']:.2f}  ({last['ATR14'] / close * 100:.2f}% of price)")

    fomo_ceiling = last["MA20"] + 2 * last["ATR14"]
    distance_to_fomo = fomo_ceiling - close
    fomo_flag = "OK" if close <= fomo_ceiling else "FOMO (>2 ATR above 20MA)"
    print(f"  FOMO:    ceiling=${fomo_ceiling:.2f}  distance={distance_to_fomo:+.2f}  [{fomo_flag}]")

    print()
    print(hist[OHLCV].tail(args.days).round(2).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
