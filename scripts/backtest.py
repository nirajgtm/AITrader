#!/usr/bin/env python3
"""Backtest — replay strategy decisions over a historical window with the
constitution gates ON vs OFF, so we can measure whether the gates cost or
saved P&L. Initial scope: PEAD long under the FOMO_INDEX gate, since the
2026-05-01 brief flagged the FOMO/PEAD conflict as the leading "obvious miss"
hypothesis.

Approach (intentionally narrow, expand once first results land):
  1. For each ticker in --universe, pull 13mo daily OHLCV via yfinance.
  2. Detect PEAD candidates: a daily bar with gap-open >= 5% over prior close.
     (No earnings calendar lookup yet; the gap heuristic catches most ER gaps
     for liquid names. False positives = pre-market news, secondaries, splits.
     Document in caveats.)
  3. Compute per-day SPY regime context: 20MA, ATR(14), is-FOMO-extended.
  4. For each candidate, simulate two trades:
       - PASS: enter regardless of FOMO (the "no gate" book)
       - GATED: enter only if SPY not in FOMO regime that day
  5. Outcome via _hit_target_or_stop (reused from regret.py): walk forward
     20 trading days, stop at gap fill, target at +1ATR * 3 above gap close.
  6. Aggregate: total trades, hit rate, total P&L per book. Delta = cost
     of the gate.

Output: state/backtest/<window>_<universe>.json + console summary.

Usage:
  backtest.py pead --start 2025-05-01 --end 2026-04-30 \\
                   --universe SPY,QQQ,IWM,XLE,XLF,XLV,XLK,XLY,XLI,XLP,XLU,XLB,XLRE,XLC \\
                   [--gap-pct 5.0] [--horizon 20]
  backtest.py pead --tickers GNRC,INTC,QCOM,PWR,MO --start 2025-05-01 --end 2026-04-30
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from _common import STATE_DIR, fmt_usd

BACKTEST_DIR = STATE_DIR / "backtest"
EARNINGS_CACHE = STATE_DIR / "cache" / "backtest_earnings_dates.json"
EARNINGS_CACHE_TTL_DAYS = 30

# Default candidate set: sector ETFs + indices. Real PEAD universe should
# include S&P 500 individual names; pass via --tickers for now.
DEFAULT_UNIVERSE = ["SPY", "QQQ", "IWM",
                    "XLE", "XLF", "XLV", "XLK", "XLY",
                    "XLI", "XLP", "XLU", "XLB", "XLRE", "XLC"]


@dataclass
class Trade:
    ticker: str
    entry_date: str
    entry: float
    stop: float
    target: float
    side: str
    gated_block: bool                # True if INDEX FOMO (SPY > 20MA+2ATR) would block
    ticker_fomo_block: bool = False  # True if TICKER itself > 20MA+2ATR on signal day
    outcome: str = "open"            # target / stop / open / data_unavailable
    days_to_hit: int | None = None
    hit_price: float | None = None
    pnl_pct: float = 0.0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0


def _ticker_fomo_map(df) -> dict:
    """Per-bar: True if Close > 20MA + 2*ATR(14). Keyed by date_iso."""
    ma20 = df["Close"].rolling(20).mean()
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    tr = high_low.combine(high_close, max).combine(low_close, max)
    atr14 = tr.rolling(14).mean()
    threshold = ma20 + 2 * atr14
    is_fomo = df["Close"] > threshold
    return {ts.date().isoformat(): bool(v) for ts, v in is_fomo.items()}


def _gate_compare(trades: list, label: str) -> dict:
    """Compare PASS / INDEX-FOMO / TICKER-FOMO / BOTH gates side by side."""
    def stats(book):
        n = len(book)
        if n == 0:
            return {"n": 0, "win_rate": 0, "total_pnl_pct": 0, "avg_pnl_pct": 0}
        wins = sum(1 for t in book if t.outcome == "target")
        losses = sum(1 for t in book if t.outcome == "stop")
        total = sum(t.pnl_pct for t in book)
        return {"n": n, "wins": wins, "losses": losses,
                "win_rate": round(wins / n * 100, 1),
                "total_pnl_pct": round(total, 2),
                "avg_pnl_pct": round(total / n, 2)}
    pass_book = trades
    no_index = [t for t in trades if not t.gated_block]
    no_ticker = [t for t in trades if not t.ticker_fomo_block]
    no_either = [t for t in trades if not t.gated_block and not t.ticker_fomo_block]
    out = {
        "PASS (no gate)": stats(pass_book),
        "INDEX-FOMO gated (current rule)": stats(no_index),
        "TICKER-FOMO gated (proposed)": stats(no_ticker),
        "BOTH gates": stats(no_either),
    }
    print(f"\n=== Gate comparison ({label}) ===")
    for k, s in out.items():
        print(f"  {k:<35} n={s['n']:<4} wr={s.get('win_rate', 0):>5.1f}%  "
              f"avg={s.get('avg_pnl_pct', 0):>+6.2f}%  total={s.get('total_pnl_pct', 0):>+8.2f}%")
    return out


def _resolve_universe(name: str) -> list[str]:
    """Resolve a universe name into a list of tickers via _universe.py.

    Names: sp500, ndx, sectors, all (everything _universe.py knows about),
    leveraged, crypto. Anything else is treated as a comma-separated literal.
    """
    name = (name or "").lower().strip()
    if not name:
        return DEFAULT_UNIVERSE
    if "," in name or name.isupper():
        return [t.strip().upper() for t in name.split(",") if t.strip()]
    try:
        from _universe import (
            get_universe, SECTOR_ETFS, INDEX_ETFS,
            LEVERAGED_INVERSE, CRYPTO_EQUITIES, _from_fmp_sp500, _from_fmp_nasdaq,
        )
    except Exception as exc:
        print(f"[WARN] _universe.py unavailable ({exc}); falling back to default",
              file=sys.stderr)
        return DEFAULT_UNIVERSE
    if name == "all":
        return sorted(get_universe())
    if name == "sp500":
        return sorted(_from_fmp_sp500())
    if name == "ndx":
        return sorted(_from_fmp_nasdaq())
    if name == "sectors":
        return sorted(SECTOR_ETFS)
    if name == "indices":
        return sorted(INDEX_ETFS)
    if name == "leveraged":
        return sorted(LEVERAGED_INVERSE)
    if name == "crypto":
        return sorted(CRYPTO_EQUITIES)
    print(f"[WARN] unknown universe '{name}', falling back to default", file=sys.stderr)
    return DEFAULT_UNIVERSE


def _load_earnings_cache() -> dict:
    if not EARNINGS_CACHE.exists():
        return {}
    try:
        with EARNINGS_CACHE.open() as f:
            return json.load(f)
    except Exception:
        return {}


def _save_earnings_cache(cache: dict) -> None:
    EARNINGS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with EARNINGS_CACHE.open("w") as f:
        json.dump(cache, f, indent=0)


def _earnings_dates_for(ticker: str, cache: dict) -> set[str]:
    """Return set of earnings date strings (YYYY-MM-DD) for a ticker.

    Uses yfinance Ticker.earnings_dates with a 30d cache. None entries marked
    in cache to avoid retrying tickers with no data.
    """
    entry = cache.get(ticker)
    cache_date = entry.get("cached_at") if entry else None
    if entry is not None and cache_date:
        cached = date.fromisoformat(cache_date)
        if (date.today() - cached).days < EARNINGS_CACHE_TTL_DAYS:
            return set(entry.get("dates", []))
    try:
        import yfinance as yf
        ed = yf.Ticker(ticker).earnings_dates
    except Exception:
        cache[ticker] = {"cached_at": date.today().isoformat(), "dates": []}
        return set()
    if ed is None or len(ed) == 0:
        cache[ticker] = {"cached_at": date.today().isoformat(), "dates": []}
        return set()
    dates = sorted(set(idx.date().isoformat() for idx in ed.index))
    cache[ticker] = {"cached_at": date.today().isoformat(), "dates": dates}
    return set(dates)


def _gap_near_earnings(gap_day: date, er_dates: set[str], window: int = 2) -> bool:
    """True if gap_day is within +/- window calendar days of any earnings date."""
    for ed_str in er_dates:
        try:
            ed = date.fromisoformat(ed_str)
        except Exception:
            continue
        if abs((gap_day - ed).days) <= window:
            return True
    return False


def _pull_daily(ticker: str, start: date, end: date):
    import yfinance as yf
    df = yf.Ticker(ticker).history(
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
    )
    if df.empty:
        return None
    return df


def _spy_fomo_series(start: date, end: date):
    """Return dict[date_iso] -> bool indicating SPY > 20MA + 2 * ATR(14) that day."""
    spy = _pull_daily("SPY", start - timedelta(days=60), end)  # warmup buffer
    if spy is None:
        return {}
    ma20 = spy["Close"].rolling(20).mean()
    high_low = spy["High"] - spy["Low"]
    high_close = (spy["High"] - spy["Close"].shift()).abs()
    low_close = (spy["Low"] - spy["Close"].shift()).abs()
    tr = high_low.combine(high_close, max).combine(low_close, max)
    atr14 = tr.rolling(14).mean()
    fomo_threshold = ma20 + 2 * atr14
    is_fomo = spy["Close"] > fomo_threshold
    return {ts.date().isoformat(): bool(v) for ts, v in is_fomo.items()}


def _detect_pead_candidates(ticker: str, df, gap_pct: float, ma50_required: bool):
    """Return list of dicts: {date, prior_close, gap_open, gap_close, atr20}.

    Heuristic: same-bar gap >= gap_pct vs prior close, with volume >= 2x ADV(20).
    """
    if df is None or len(df) < 30:
        return []
    ma50 = df["Close"].rolling(50).mean()
    adv20 = df["Volume"].rolling(20).mean()
    high_low = df["High"] - df["Low"]
    atr20 = high_low.rolling(20).mean()

    candidates = []
    closes = df["Close"]
    opens = df["Open"]
    for i in range(50, len(df) - 1):  # need warmup; need at least one fwd day
        prior_close = float(closes.iloc[i - 1])
        today_open = float(opens.iloc[i])
        if prior_close <= 0:
            continue
        gap_pct_actual = (today_open - prior_close) / prior_close * 100
        if gap_pct_actual < gap_pct:
            continue
        if df["Volume"].iloc[i] < 2 * adv20.iloc[i]:
            continue
        if ma50_required and float(closes.iloc[i]) < float(ma50.iloc[i]):
            continue
        candidates.append({
            "date": df.index[i].date(),
            "prior_close": prior_close,
            "gap_open": today_open,
            "gap_close": float(closes.iloc[i]),
            "atr20": float(atr20.iloc[i]),
            "next_open": float(opens.iloc[i + 1]) if i + 1 < len(df) else None,
        })
    return candidates


def _simulate_trade(df, entry_idx_date: date, entry: float, stop: float,
                   target: float, horizon: int) -> dict:
    """Walk forward `horizon` bars, return outcome dict."""
    # Find entry_idx_date position
    try:
        i_start = df.index.get_loc(df.index[df.index.date == entry_idx_date][0])
    except (IndexError, KeyError):
        return {"outcome": "data_unavailable"}
    favorable = adverse = entry
    end_i = min(i_start + horizon, len(df))
    for i in range(i_start, end_i):
        hi = float(df["High"].iloc[i])
        lo = float(df["Low"].iloc[i])
        favorable = max(favorable, hi)
        adverse = min(adverse, lo)
        if lo <= stop:
            return {"outcome": "stop", "hit_price": stop,
                    "days_to_hit": i - i_start,
                    "pnl_pct": (stop - entry) / entry * 100,
                    "mfe_pct": (favorable - entry) / entry * 100,
                    "mae_pct": (adverse - entry) / entry * 100}
        if hi >= target:
            return {"outcome": "target", "hit_price": target,
                    "days_to_hit": i - i_start,
                    "pnl_pct": (target - entry) / entry * 100,
                    "mfe_pct": (favorable - entry) / entry * 100,
                    "mae_pct": (adverse - entry) / entry * 100}
    last_close = float(df["Close"].iloc[end_i - 1])
    return {"outcome": "open", "hit_price": last_close,
            "days_to_hit": end_i - 1 - i_start,
            "pnl_pct": (last_close - entry) / entry * 100,
            "mfe_pct": (favorable - entry) / entry * 100,
            "mae_pct": (adverse - entry) / entry * 100}


def _detect_breakout_candidates(ticker: str, df, vol_x_min: float = 1.5,
                                lookback: int = 20):
    """Daily close > prior `lookback`-bar high, with volume >= vol_x_min * ADV(20).

    Returns list of dicts: {date, close, breakout_high, atr20, next_open}.
    """
    if df is None or len(df) < lookback + 25:
        return []
    closes = df["Close"]
    highs = df["High"]
    opens = df["Open"]
    adv20 = df["Volume"].rolling(20).mean()
    atr20 = (df["High"] - df["Low"]).rolling(20).mean()

    candidates = []
    for i in range(lookback + 21, len(df) - 1):
        prior_high = float(highs.iloc[i - lookback:i].max())
        close = float(closes.iloc[i])
        if close <= prior_high:
            continue
        vol = float(df["Volume"].iloc[i])
        if vol < vol_x_min * float(adv20.iloc[i]):
            continue
        candidates.append({
            "date": df.index[i].date(),
            "close": close,
            "breakout_high": prior_high,
            "atr20": float(atr20.iloc[i]),
            "next_open": float(opens.iloc[i + 1]),
        })
    return candidates


def cmd_breakout(args: argparse.Namespace) -> int:
    """20-day high breakout long with volume confirmation.

    Stop = breakout_high - 0.5 * ATR (failed-breakout invalidation).
    Target = entry + ATR * 3.
    Same FOMO gate test as PEAD.
    """
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if args.tickers:
        universe = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        universe = _resolve_universe(args.universe)

    print(f"Backtest breakout long: {start} -> {end}, universe N={len(universe)}, "
          f"vol_x_min={args.vol_x_min}, lookback={args.lookback}, horizon={args.horizon}")
    fomo_map = _spy_fomo_series(start, end)
    print(f"SPY FOMO days in window: "
          f"{sum(1 for v in fomo_map.values() if v)}/{len(fomo_map)}")

    trades: list[Trade] = []
    for ticker in universe:
        df = _pull_daily(ticker, start - timedelta(days=80), end)
        if df is None:
            continue
        ticker_fomo = _ticker_fomo_map(df)
        candidates = _detect_breakout_candidates(ticker, df, args.vol_x_min, args.lookback)
        for c in candidates:
            entry = c["next_open"]
            stop = c["breakout_high"] - 0.5 * c["atr20"]
            target = entry + c["atr20"] * 3
            entry_date_iter = df.index[df.index.date > c["date"]]
            if len(entry_date_iter) == 0:
                continue
            entry_date = entry_date_iter[0].date()
            outcome = _simulate_trade(df, entry_date, entry, stop, target, args.horizon)
            index_block = bool(fomo_map.get(c["date"].isoformat(), False))
            ticker_block = ticker_fomo.get(c["date"].isoformat(), False)
            t = Trade(ticker=ticker, entry_date=entry_date.isoformat(),
                     entry=entry, stop=stop, target=target,
                     side="LONG", gated_block=index_block,
                     ticker_fomo_block=ticker_block,
                     **{k: v for k, v in outcome.items() if k in
                        ("outcome", "days_to_hit", "hit_price", "pnl_pct",
                         "mfe_pct", "mae_pct")})
            trades.append(t)

    gate_compare = _gate_compare(trades, label="breakout")

    blocked = [t for t in trades if t.gated_block]
    blocked_winners = [t for t in blocked if t.outcome == "target"]

    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    out_path = BACKTEST_DIR / f"breakout_{args.start}_{args.end}.json"
    with out_path.open("w") as f:
        json.dump({
            "params": {"start": args.start, "end": args.end,
                       "universe": universe, "vol_x_min": args.vol_x_min,
                       "lookback": args.lookback, "horizon": args.horizon},
            "gate_compare": gate_compare,
            "blocked_winners": [asdict(t) for t in blocked_winners],
            "trades": [asdict(t) for t in trades],
        }, f, indent=2)
    print(f"\nWrote {out_path}")
    return 0


def cmd_pead(args: argparse.Namespace) -> int:
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if args.tickers:
        universe = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        universe = _resolve_universe(args.universe)

    print(f"Backtest PEAD long: {start} -> {end}, universe N={len(universe)}, "
          f"gap_pct={args.gap_pct}, horizon={args.horizon}, "
          f"earnings_filter={args.earnings_filter}")
    fomo_map = _spy_fomo_series(start, end)
    print(f"SPY FOMO days in window: "
          f"{sum(1 for v in fomo_map.values() if v)}/{len(fomo_map)}")

    earnings_cache = _load_earnings_cache() if args.earnings_filter else {}

    trades: list[Trade] = []
    skipped = 0
    earnings_filtered = 0
    for ticker in universe:
        df = _pull_daily(ticker, start - timedelta(days=80), end)
        if df is None:
            skipped += 1
            continue
        ticker_fomo = _ticker_fomo_map(df)
        candidates = _detect_pead_candidates(ticker, df, args.gap_pct, args.require_ma50)
        if args.earnings_filter and candidates:
            er_dates = _earnings_dates_for(ticker, earnings_cache)
            kept = [c for c in candidates
                    if _gap_near_earnings(c["date"], er_dates, args.earnings_window)]
            earnings_filtered += len(candidates) - len(kept)
            candidates = kept
        for c in candidates:
            if c["next_open"] is None:
                continue
            entry = c["next_open"]
            stop = c["prior_close"]
            target = c["gap_close"] + c["atr20"] * 3
            entry_date_iter = df.index[df.index.date > c["date"]]
            if len(entry_date_iter) == 0:
                continue
            entry_date = entry_date_iter[0].date()
            outcome = _simulate_trade(df, entry_date, entry, stop, target, args.horizon)
            index_block = bool(fomo_map.get(c["date"].isoformat(), False))
            ticker_block = ticker_fomo.get(c["date"].isoformat(), False)
            t = Trade(ticker=ticker, entry_date=entry_date.isoformat(),
                     entry=entry, stop=stop, target=target,
                     side="LONG", gated_block=index_block,
                     ticker_fomo_block=ticker_block,
                     **{k: v for k, v in outcome.items() if k in
                        ("outcome", "days_to_hit", "hit_price", "pnl_pct",
                         "mfe_pct", "mae_pct")})
            trades.append(t)

    # Aggregate
    pass_book = trades  # all trades, no gate
    gated_book = [t for t in trades if not t.gated_block]  # only those NOT blocked

    def stats(book):
        n = len(book)
        if n == 0:
            return {"n": 0}
        wins = sum(1 for t in book if t.outcome == "target")
        losses = sum(1 for t in book if t.outcome == "stop")
        opens = sum(1 for t in book if t.outcome == "open")
        avg_pnl = sum(t.pnl_pct for t in book) / n
        return {"n": n, "wins": wins, "losses": losses, "open": opens,
                "win_rate": wins / n * 100,
                "avg_pnl_pct": round(avg_pnl, 2),
                "total_pnl_pct": round(sum(t.pnl_pct for t in book), 2)}

    pass_stats = stats(pass_book)
    gated_stats = stats(gated_book)

    blocked_trades = [t for t in trades if t.gated_block]
    blocked_winners = [t for t in blocked_trades if t.outcome == "target"]

    print("\n=== Results ===")
    print(f"PASS book (no gate):   {pass_stats}")
    print(f"GATED book (FOMO on):  {gated_stats}")
    print(f"\nFOMO blocked: {len(blocked_trades)} trades")
    print(f"  of which winners: {len(blocked_winners)} "
          f"({len(blocked_winners) / max(len(blocked_trades), 1) * 100:.0f}%)")
    print(f"  blocked-winner total pnl_pct: "
          f"{sum(t.pnl_pct for t in blocked_winners):+.2f}%")
    print(f"  cost of gate (PASS - GATED total_pnl_pct): "
          f"{pass_stats.get('total_pnl_pct', 0) - gated_stats.get('total_pnl_pct', 0):+.2f}%")

    gate_compare = _gate_compare(trades, label="PEAD")

    if blocked_winners and not args.quiet:
        print("\nTop blocked winners:")
        for t in sorted(blocked_winners, key=lambda x: -x.pnl_pct)[:10]:
            print(f"  {t.ticker:<6} entry={t.entry_date} pnl={t.pnl_pct:+.2f}% "
                  f"hit_in={t.days_to_hit}d")

    if args.earnings_filter:
        _save_earnings_cache(earnings_cache)
        print(f"\nEarnings filter dropped: {earnings_filtered} non-ER gaps")

    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "_erfilter" if args.earnings_filter else ""
    universe_tag = (args.universe if not args.tickers else "custom")
    out_path = BACKTEST_DIR / f"pead_{universe_tag}_{args.start}_{args.end}{suffix}.json"
    with out_path.open("w") as f:
        json.dump({
            "params": {"start": args.start, "end": args.end,
                       "gap_pct": args.gap_pct, "horizon": args.horizon,
                       "universe": universe, "require_ma50": args.require_ma50},
            "fomo_days": sum(1 for v in fomo_map.values() if v),
            "total_days": len(fomo_map),
            "pass_stats": pass_stats,
            "gated_stats": gated_stats,
            "gate_compare": gate_compare,
            "blocked_winners": [asdict(t) for t in blocked_winners],
            "trades": [asdict(t) for t in trades],
        }, f, indent=2)
    print(f"\nWrote {out_path}")
    return 0


def cmd_portfolio(args: argparse.Namespace) -> int:
    """Aggregate saved per-strategy backtest JSONs into a portfolio sim and
    compare to SPY buy/hold over the same window.

    Sizing: equal-weight `--per-trade-pct` of equity per entry, up to
    `--max-concurrent` open at once. Cash earns 0. New signals while at the
    concurrency cap are skipped (the realistic constraint that drives most
    active strategies' performance).

    Gate: applies the v2.3 carve-out --
      ALLOW = (ticker in leadership tier) OR (NOT index-FOMO blocked)
    Trades that fail both are dropped (would have been blocked).
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from leadership import is_leadership_tier
    except Exception:
        def is_leadership_tier(_t): return False  # noqa: E731

    files = [Path(p) for p in args.inputs.split(",") if p.strip()]
    all_trades = []
    for f in files:
        if not f.exists():
            print(f"[WARN] missing: {f}", file=sys.stderr)
            continue
        d = json.loads(f.read_text())
        all_trades.extend(d.get("trades", []))

    if not all_trades:
        print("[ERR] no trades loaded", file=sys.stderr)
        return 1

    # Apply v2.3 carve-out
    def passes_gate(t: dict) -> bool:
        if is_leadership_tier(t["ticker"]):
            return True
        return not t.get("gated_block", False)

    eligible = [t for t in all_trades if passes_gate(t)]
    print(f"Loaded {len(all_trades)} trades; {len(eligible)} pass v2.3 carve-out gate "
          f"({len(all_trades) - len(eligible)} dropped).")

    # Sort by entry_date for chronological replay
    eligible.sort(key=lambda t: t["entry_date"])

    # Determine window
    first_entry = date.fromisoformat(eligible[0]["entry_date"])
    last_entry = date.fromisoformat(eligible[-1]["entry_date"])
    window_start = first_entry
    window_end = max(last_entry, date.today())

    # Portfolio sim: walk trades chronologically, track open slots and equity.
    equity = float(args.start_equity)
    open_slots: list[dict] = []  # each = {trade, exit_date, exit_pnl_dollars}
    skipped_concurrency = 0
    taken = 0
    realized_curve = []  # list of (date_iso, equity_after_close)

    def close_due(today: date):
        nonlocal equity
        still_open = []
        for slot in open_slots:
            if slot["exit_date"] <= today:
                equity += slot["exit_pnl_dollars"]
                realized_curve.append((today.isoformat(), round(equity, 2)))
            else:
                still_open.append(slot)
        return still_open

    # Walk trades chronologically
    from datetime import timedelta as _td
    for t in eligible:
        entry_date = date.fromisoformat(t["entry_date"])
        # Close any positions whose exit fell on or before this entry date
        open_slots[:] = close_due(entry_date)
        if len(open_slots) >= args.max_concurrent:
            skipped_concurrency += 1
            continue
        size_dollars = equity * (args.per_trade_pct / 100)
        # Outcome already simulated per-trade. Use pnl_pct on the sized notional.
        pnl_dollars = size_dollars * (t.get("pnl_pct", 0) / 100)
        exit_date = entry_date + _td(days=int(t.get("days_to_hit") or 0))
        open_slots.append({
            "trade": t, "exit_date": exit_date,
            "exit_pnl_dollars": pnl_dollars,
        })
        taken += 1

    # Close any remaining open positions at window end (or today)
    open_slots[:] = close_due(window_end)
    final_equity = equity
    strategy_return_pct = (final_equity - args.start_equity) / args.start_equity * 100

    # SPY buy/hold over the same window
    import yfinance as yf
    spy = yf.Ticker("SPY").history(
        start=window_start.isoformat(),
        end=(window_end + _td(days=1)).isoformat(),
    )
    spy_start_close = float(spy.iloc[0]["Close"])
    spy_end_close = float(spy.iloc[-1]["Close"])
    spy_return_pct = (spy_end_close - spy_start_close) / spy_start_close * 100
    spy_final_equity = args.start_equity * (1 + spy_return_pct / 100)

    print(f"\n=== Portfolio sim ({window_start} -> {window_end}) ===")
    print(f"Sizing: {args.per_trade_pct}% per trade, max {args.max_concurrent} concurrent, "
          f"start equity ${args.start_equity:,.0f}")
    print(f"Trades taken:        {taken}")
    print(f"Skipped (concurrency): {skipped_concurrency}")
    print(f"\nStrategy final equity:  ${final_equity:,.2f}  ({strategy_return_pct:+.2f}%)")
    print(f"SPY final equity:       ${spy_final_equity:,.2f}  ({spy_return_pct:+.2f}%)")
    delta_pct = strategy_return_pct - spy_return_pct
    delta_dollars = final_equity - spy_final_equity
    print(f"Alpha vs SPY:           {delta_pct:+.2f} pp  (${delta_dollars:+,.2f})")
    if delta_pct < 0:
        print(f"  -> SPY beats strategy by {abs(delta_pct):.2f} pp")
    else:
        print(f"  -> Strategy beats SPY by {delta_pct:.2f} pp")

    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    out_path = BACKTEST_DIR / f"portfolio_compare_{window_start}_{window_end}.json"
    with out_path.open("w") as f:
        json.dump({
            "params": {"start_equity": args.start_equity,
                       "per_trade_pct": args.per_trade_pct,
                       "max_concurrent": args.max_concurrent,
                       "inputs": [str(p) for p in files]},
            "window": {"start": window_start.isoformat(),
                       "end": window_end.isoformat()},
            "totals": {
                "trades_eligible": len(eligible),
                "trades_taken": taken,
                "skipped_concurrency": skipped_concurrency,
                "final_equity": round(final_equity, 2),
                "strategy_return_pct": round(strategy_return_pct, 2),
                "spy_final_equity": round(spy_final_equity, 2),
                "spy_return_pct": round(spy_return_pct, 2),
                "alpha_pp": round(delta_pct, 2),
                "alpha_dollars": round(delta_dollars, 2),
            },
            "equity_curve": realized_curve,
        }, f, indent=2)
    print(f"\nWrote {out_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("pead", help="PEAD long FOMO-gate backtest")
    pe.add_argument("--start", required=True, help="YYYY-MM-DD")
    pe.add_argument("--end", required=True, help="YYYY-MM-DD")
    pe.add_argument("--universe", default="default",
                    help="sp500, ndx, sectors, indices, leveraged, crypto, "
                         "all, default, or comma-separated tickers")
    pe.add_argument("--tickers", default=None,
                    help="comma-separated; overrides --universe")
    pe.add_argument("--gap-pct", type=float, default=5.0,
                    help="min gap-up percent to flag PEAD candidate")
    pe.add_argument("--horizon", type=int, default=20)
    pe.add_argument("--require-ma50", action="store_true",
                    help="enforce close > 50MA at gap (PEAD spec rule 5)")
    pe.add_argument("--earnings-filter", action="store_true",
                    help="only keep gaps within +/- earnings-window days of "
                         "an actual earnings date (yfinance, cached 30d)")
    pe.add_argument("--earnings-window", type=int, default=2,
                    help="calendar-day window around earnings dates")
    pe.add_argument("--quiet", action="store_true")

    bo = sub.add_parser("breakout", help="20-day high breakout long FOMO-gate backtest")
    bo.add_argument("--start", required=True)
    bo.add_argument("--end", required=True)
    bo.add_argument("--universe", default="default")
    bo.add_argument("--tickers", default=None)
    bo.add_argument("--vol-x-min", type=float, default=1.5)
    bo.add_argument("--lookback", type=int, default=20)
    bo.add_argument("--horizon", type=int, default=20)
    bo.add_argument("--quiet", action="store_true")

    pc = sub.add_parser("portfolio",
                        help="aggregate per-strategy backtest JSONs into "
                             "portfolio sim with v2.3 carve-out, vs SPY")
    pc.add_argument("--inputs", required=True,
                    help="comma-separated paths to per-strategy backtest JSONs")
    pc.add_argument("--start-equity", type=float, default=10000)
    pc.add_argument("--per-trade-pct", type=float, default=5.0,
                    help="equity %% allocated per entry")
    pc.add_argument("--max-concurrent", type=int, default=8,
                    help="max open positions at once; new signals at cap are skipped")

    args = ap.parse_args()
    return {"pead": cmd_pead, "breakout": cmd_breakout,
            "portfolio": cmd_portfolio}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
