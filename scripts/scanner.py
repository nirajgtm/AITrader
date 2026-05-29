#!/usr/bin/env python3
"""Universe-wide scanner: breakouts, breakdowns, 52w hi/lo, vol expansion.

Strategy:
  - One Polygon `grouped_daily` call returns ~12k US tickers' OHLCV per date.
  - We pull ~21 trading days for a 20-day window, ~252 for 52w. Each past-date
    call is cached 12h+ (past dates don't change), so warm-cache is free.
  - Filter results by `_universe.is_in_universe()` to surface tradable names only.
  - All metrics computed locally; zero per-ticker network calls.

Outputs (JSON, per --json):
  - breakouts        — close > 20-day high AND volume > 1.5× ADV
  - breakdowns       — close < 20-day low  AND volume > 1.5× ADV
  - new_52w_highs    — close >= 252-day high
  - new_52w_lows     — close <= 252-day low
  - vol_expansion    — current ATR(14) > 1.5× ATR(60)
  - oversold         — RSI(14) < 30 (quietly washed-out names)

Quality filter applied to all candidates:
  - price > $5
  - in universe (so penny moonshots and obscure foreign listings are excluded)

Usage:
  scanner.py
  scanner.py --json
  scanner.py --kind breakouts --json
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from _cache import cache_get, cache_put  # noqa: E402
from _terse import emit, step_result  # noqa: E402
from _universe import get_universe, LEVERAGED_INVERSE  # noqa: E402

PRICE_FLOOR = 5.0


def _trading_days_back(n: int) -> list[str]:
    """Return last N trading days (approx — skips weekends; doesn't account for holidays)."""
    out: list[str] = []
    d = date.today()
    # Step back; skip Sat/Sun
    while len(out) < n:
        if d.weekday() < 5:  # Mon-Fri
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out


def _build_history(days: int = 252) -> dict[str, list[dict]]:
    """Per-ticker daily-bar history for the universe, sourced from public.com
    (10 req/s, full 52-week history), replacing the rate-limited Polygon
    grouped_daily. Each ticker's bars are cached ~4h; a cold full-universe fetch
    runs in parallel (capped by public.com's shared rate gate) in ~60-90s. The
    aggregate is still cached under the same key so deep_scan.py keeps working."""
    today = date.today().isoformat()
    cache_key = f"scanner_history_{days}d_{today}"
    cached = cache_get(cache_key, ttl_seconds=4 * 3600)
    if cached is not None and len(cached) >= 400:
        return cached

    import concurrent.futures
    from publicdotcom_api import get_daily_ohlcv

    universe = sorted(get_universe())

    def _fetch(tk: str):
        pc = cache_get(f"pubhist_{tk}", ttl_seconds=4 * 3600)
        if pc is None:
            try:
                raw = get_daily_ohlcv(tk)
                pc = [{"date": b["date"], "o": b["open"], "h": b["high"],
                       "l": b["low"], "c": b["close"], "v": b["volume"]}
                      for b in (raw or [])]
                import datetime as _dt
                _today = _dt.date.today().isoformat()
                if pc and pc[-1].get("date") == _today:
                    pc = pc[:-1]  # drop the in-progress session; scan completed daily bars (movers.py covers intraday)
                if pc:
                    cache_put(f"pubhist_{tk}", pc)
            except Exception:
                pc = []
        return tk, (pc[-days:] if (days and pc) else pc)

    by_tk: dict[str, list[dict]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for tk, bars in ex.map(_fetch, universe):
            if bars:
                by_tk[tk] = bars

    if len(by_tk) >= 400:
        cache_put(cache_key, by_tk)
    return by_tk


def _atr(bars: list[dict], period: int = 14) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l = bars[i]["h"], bars[i]["l"]
        pc = bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    return statistics.mean(trs[-period:])


def _scan(history: dict[str, list[dict]]) -> dict:
    import pandas as pd
    from price import rsi as _rsi

    breakouts: list[dict] = []
    breakdowns: list[dict] = []
    new_52w_highs: list[dict] = []
    new_52w_lows: list[dict] = []
    vol_expansion: list[dict] = []
    oversold: list[dict] = []

    for tk, bars in history.items():
        if len(bars) < 11:
            continue
        last = bars[-1]
        last_close = last["c"]
        last_vol = last["v"] or 0
        if last_close < PRICE_FLOOR:
            continue

        # 20-day window (excluding today). Use what we have if < 20.
        n_lookback = min(20, len(bars) - 1)
        window20 = bars[-1 - n_lookback : -1]
        if len(window20) < 10:
            continue
        high20 = max(b["h"] for b in window20)
        low20 = min(b["l"] for b in window20)
        avg_vol = statistics.mean(b["v"] for b in window20 if b["v"]) or 1
        vol_x_avg = last_vol / avg_vol if avg_vol else 0

        if last_close > high20 and vol_x_avg > 1.5:
            breakouts.append({
                "tk": tk, "close": round(last_close, 2),
                "high20d": round(high20, 2),
                "vol_x_avg": round(vol_x_avg, 2),
            })
        if last_close < low20 and vol_x_avg > 1.5:
            breakdowns.append({
                "tk": tk, "close": round(last_close, 2),
                "low20d": round(low20, 2),
                "vol_x_avg": round(vol_x_avg, 2),
            })

        # 52-week (252-day) extremes
        if len(bars) >= 240:
            window252 = bars[-252:]
            high52 = max(b["h"] for b in window252)
            low52 = min(b["l"] for b in window252)
            if last_close >= high52 * 0.999:
                new_52w_highs.append({"tk": tk, "close": round(last_close, 2),
                                      "high52w": round(high52, 2)})
            if last_close <= low52 * 1.001:
                new_52w_lows.append({"tk": tk, "close": round(last_close, 2),
                                     "low52w": round(low52, 2)})

        # ATR(14) vs ATR(60) — vol expansion
        if len(bars) >= 61:
            atr14 = _atr(bars, 14)
            atr60 = _atr(bars, 60)
            if atr14 and atr60 and atr14 > 1.5 * atr60:
                vol_expansion.append({
                    "tk": tk, "close": round(last_close, 2),
                    "atr14": round(atr14, 2), "atr60": round(atr60, 2),
                    "ratio": round(atr14 / atr60, 2),
                })

        # RSI(14) oversold — quietly washed-out names
        if len(bars) >= 15:
            try:
                r = float(_rsi(pd.Series([b["c"] for b in bars])).iloc[-1])
                if r < 30 and tk not in LEVERAGED_INVERSE:
                    oversold.append({"tk": tk, "close": round(last_close, 2),
                                     "rsi14": round(r, 1)})
            except Exception:
                pass

    # Sort by signal strength
    breakouts.sort(key=lambda x: -x["vol_x_avg"])
    breakdowns.sort(key=lambda x: -x["vol_x_avg"])
    new_52w_highs.sort(key=lambda x: -x["close"])
    new_52w_lows.sort(key=lambda x: x["close"])
    vol_expansion.sort(key=lambda x: -x["ratio"])
    oversold.sort(key=lambda x: x["rsi14"])

    return {
        "breakouts": breakouts[:25],
        "breakdowns": breakdowns[:25],
        "new_52w_highs": new_52w_highs[:25],
        "new_52w_lows": new_52w_lows[:25],
        "vol_expansion": vol_expansion[:25],
        "oversold": oversold[:25],
    }


def _scan_pead(history: dict[str, list[dict]]) -> list[dict]:
    """PEAD candidates: gap-up day with vol spike, close > 50MA.

    Looks at the most recent 5 trading days. For each day, finds tickers with:
      - gap-day open / prior close >= 1.05 (5% gap up)
      - volume >= 2× 20-day average volume
      - close > 50-day MA (uptrend confirmation)

    Returns list of {tk, gap_day, gap_pct, vol_x_avg, close, ma50}.

    Signals only — actual PEAD trade requires manual guidance-raise verification
    (read the press release / news).
    """
    candidates = []
    for tk, bars in history.items():
        if len(bars) < 51:
            continue
        # Need MA50, prior close, current bar
        for offset in range(-5, 0):
            i = len(bars) + offset
            if i < 51:
                continue
            bar = bars[i]
            prev_bar = bars[i - 1]
            close = bar["c"]
            opn = bar["o"]
            vol = bar["v"] or 0
            prev_close = prev_bar["c"]
            if not all([close, opn, prev_close, vol]):
                continue
            if close < PRICE_FLOOR:
                continue
            gap_pct = (opn / prev_close - 1) * 100
            if gap_pct < 5:
                continue
            # Volume avg over prior 20 days
            window = bars[max(0, i - 20):i]
            avg_vol = statistics.mean(b["v"] for b in window if b["v"]) or 1
            vol_x = vol / avg_vol
            if vol_x < 2.0:
                continue
            # MA50 (close-based)
            ma_window = bars[max(0, i - 50):i]
            if len(ma_window) < 30:
                continue
            ma50 = statistics.mean(b["c"] for b in ma_window if b["c"])
            if close <= ma50:
                continue

            candidates.append({
                "tk": tk,
                "gap_day": bar["date"],
                "gap_pct": round(gap_pct, 2),
                "vol_x_avg": round(vol_x, 2),
                "close": round(close, 2),
                "ma50": round(ma50, 2),
            })

    # Dedupe by ticker — keep most recent
    by_tk = {}
    for c in candidates:
        if c["tk"] not in by_tk or c["gap_day"] > by_tk[c["tk"]]["gap_day"]:
            by_tk[c["tk"]] = c
    out = sorted(by_tk.values(), key=lambda x: -x["gap_pct"])
    return out


def _scan_pre_earnings_runup(history: dict[str, list[dict]],
                              earnings_by_tk: dict[str, str],
                              within_days: int = 7) -> list[dict]:
    """Pre-earnings run-up: name up 5-15% in 5 days before scheduled earnings.

    Two interpretations (caller decides):
      - Continuation: smart money front-running a beat → ride into print
      - Fade: premium priced in → short-volatility play (debit put spread)

    `earnings_by_tk` is dict[ticker -> next_earnings_iso_date].
    Returns list with both 5d_return and days_to_earnings.
    """
    candidates = []
    today = date.today()
    for tk, bars in history.items():
        if len(bars) < 6:
            continue
        next_er = earnings_by_tk.get(tk)
        if not next_er:
            continue
        try:
            er_date = date.fromisoformat(next_er)
        except Exception:
            continue
        days_to_er = (er_date - today).days
        if not (0 < days_to_er <= within_days):
            continue
        # 5-day return
        last_close = bars[-1]["c"]
        ago_close = bars[-6]["c"]
        if not last_close or not ago_close:
            continue
        ret5 = (last_close / ago_close - 1) * 100
        if not (5.0 <= ret5 <= 15.0):
            continue
        if last_close < PRICE_FLOOR:
            continue
        candidates.append({
            "tk": tk,
            "next_earnings": next_er,
            "days_to_er": days_to_er,
            "ret5d_pct": round(ret5, 2),
            "close": round(last_close, 2),
        })
    return sorted(candidates, key=lambda x: -x["ret5d_pct"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["breakouts", "breakdowns", "new_52w_highs",
                                       "new_52w_lows", "vol_expansion", "oversold",
                                       "pead", "pre_earnings_runup"],
                    help="Print only one signal type")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--days", type=int, default=252,
                    help="Trading-days history to load (default 252 = 1y)")
    ap.add_argument("--pead", action="store_true",
                    help="Run PEAD scan (post-earnings drift candidates)")
    ap.add_argument("--pre-earnings", action="store_true",
                    help="Run pre-earnings run-up scan")
    args = ap.parse_args()

    history = _build_history(args.days)

    # PEAD-only mode
    if args.pead and not args.pre_earnings:
        pead = _scan_pead(history)
        flags = [f"pead_{c['tk'].lower()}" for c in pead[:10]]
        headline = f"PEAD candidates: {len(pead)}"
        if args.json:
            emit(step_result("scanner", ok=True, headline=headline,
                             data={"pead": pead}, flags=flags))
        else:
            for c in pead[:25]:
                print(f"  {c}")
        return 0

    # Pre-earnings run-up mode
    if args.pre_earnings:
        # Pull universe upcoming earnings
        from _apikeys import has_key
        ek_map: dict[str, str] = {}
        if has_key("FINNHUB_API_KEY"):
            try:
                from _providers import finnhub
                fh = finnhub()
                cal = fh.earnings_calendar(days_ahead=14) or []
                for entry in cal:
                    sym = (entry.get("symbol") or "").upper()
                    d_str = entry.get("date")
                    if sym and d_str and sym not in ek_map:
                        ek_map[sym] = d_str
            except Exception:
                pass
        if not ek_map:
            if args.json:
                emit(step_result("scanner", ok=False,
                                 headline="no earnings calendar available",
                                 errors=["set FINNHUB_API_KEY"]))
            return 1
        runups = _scan_pre_earnings_runup(history, ek_map, within_days=7)
        flags = [f"pre_er_runup_{c['tk'].lower()}" for c in runups[:10]]
        headline = f"Pre-earnings run-ups (5d>5%, ER≤7d): {len(runups)}"
        if args.json:
            emit(step_result("scanner", ok=True, headline=headline,
                             data={"pre_earnings_runup": runups}, flags=flags))
        else:
            for c in runups[:25]:
                print(f"  {c}")
        return 0

    sig = _scan(history)

    flags: list[str] = []
    for tk_d in sig["breakouts"][:10]:
        flags.append(f"breakout_{tk_d['tk'].lower()}")
    for tk_d in sig["new_52w_highs"][:10]:
        flags.append(f"new52w_high_{tk_d['tk'].lower()}")
    for tk_d in sig["vol_expansion"][:10]:
        flags.append(f"vol_expansion_{tk_d['tk'].lower()}")

    headline = (f"BO={len(sig['breakouts'])} BD={len(sig['breakdowns'])} "
                f"52w_hi={len(sig['new_52w_highs'])} "
                f"vol_exp={len(sig['vol_expansion'])} "
                f"OS={len(sig['oversold'])}; "
                f"universe sample={len(history)}")

    if args.json:
        if args.kind:
            emit(step_result("scanner", ok=True, headline=headline,
                             data={args.kind: sig[args.kind]}, flags=flags))
        else:
            emit(step_result("scanner", ok=True, headline=headline,
                             data=sig, flags=flags))
        return 0

    # Human dashboard
    def _print(label: str, items: list[dict]) -> None:
        print(f"\n=== {label} ({len(items)}) ===")
        for it in items[:10]:
            print(f"  {it}")
    if args.kind:
        _print(args.kind, sig[args.kind])
    else:
        _print("BREAKOUTS", sig["breakouts"])
        _print("BREAKDOWNS", sig["breakdowns"])
        _print("NEW 52W HIGHS", sig["new_52w_highs"])
        _print("NEW 52W LOWS", sig["new_52w_lows"])
        _print("VOL EXPANSION", sig["vol_expansion"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
