#!/usr/bin/env python3
"""Earnings — calendar + historical reactions.

Modes:
  earnings.py NVDA AAPL ...               # next earnings + ATM straddle expected move
  earnings.py --from-watchlist
  earnings.py --json
  earnings.py --no-em                     # skip expected-move (faster path for runbook)
  earnings.py --history NVDA              # last 4-8 qtr beat/miss + 1d reaction profile

Sources:
  - PRIMARY upcoming dates: Finnhub /calendar/earnings (chunked weekly, full universe)
  - FALLBACK upcoming dates: yfinance .get_earnings_dates() per ticker
  - Expected move: yfinance nearest-expiry ATM straddle / spot
  - Historical reactions: Finnhub /stock/earnings (actual/estimate/surprise%)
                          + yfinance OHLC for next-day return
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from _apikeys import has_key  # noqa: E402
import watchlist_store  # noqa: E402


def watchlist_tickers() -> list[str]:
    return watchlist_store.active_tickers()


def expected_move_pct(tk: yf.Ticker, spot: float) -> tuple[float | None, str]:
    """Nearest-expiry ATM straddle mid → implied % move."""
    exps = tk.options or []
    if not exps:
        return None, ""
    # pick first expiry
    exp = exps[0]
    try:
        ch = tk.option_chain(exp)
    except Exception:
        return None, exp
    calls = ch.calls.copy()
    puts = ch.puts.copy()
    if calls.empty or puts.empty:
        return None, exp
    for df in (calls, puts):
        df["d"] = (df["strike"] - spot).abs()
    c = calls.nsmallest(1, "d").iloc[0]
    p = puts.nsmallest(1, "d").iloc[0]
    c_mid = (c.get("bid", 0) + c.get("ask", 0)) / 2 or c.get("lastPrice", 0)
    p_mid = (p.get("bid", 0) + p.get("ask", 0)) / 2 or p.get("lastPrice", 0)
    straddle = c_mid + p_mid
    if spot > 0 and straddle > 0:
        return round(straddle / spot * 100, 2), exp
    return None, exp


def _cmd_universe_calendar(days: int = 14, json_mode: bool = False) -> int:
    """Universe-wide earnings calendar.

    One Finnhub bulk call (chunked weekly) returns the full universe of
    upcoming earnings. We filter to the working universe and return a
    by-date dict for quick reference.

    Goal: surface non-mainstream names with earnings in horizon — not just
    the hardcoded 10 mega-caps.
    """
    from _terse import emit, step_result
    from _apikeys import has_key
    sys.path.insert(0, str(ROOT / "scripts"))
    from _universe import get_universe

    if not has_key("FINNHUB_API_KEY"):
        if json_mode:
            emit(step_result("earnings_universe", ok=False,
                             headline="FINNHUB_API_KEY required",
                             errors=["set FINNHUB_API_KEY"]))
        else:
            print("FINNHUB_API_KEY required for --universe", file=sys.stderr)
        return 1

    from _providers import finnhub
    fh = finnhub()
    cal = fh.earnings_calendar(days_ahead=days) or []
    universe = get_universe()

    # Filter to in-universe and group by date
    by_date: dict[str, list[dict]] = {}
    in_u_count = 0
    today = date.today()
    for entry in cal:
        sym = (entry.get("symbol") or "").upper()
        d_str = entry.get("date")
        if not sym or sym not in universe:
            continue
        if not d_str:
            continue
        try:
            d_obj = date.fromisoformat(d_str)
        except Exception:
            continue
        days_out = (d_obj - today).days
        if days_out < 0 or days_out > days:
            continue
        in_u_count += 1
        by_date.setdefault(d_str, []).append({
            "tk": sym,
            "hour": entry.get("hour"),  # bmo / amc / dmh
            "eps_est": entry.get("epsEstimate"),
            "rev_est": entry.get("revenueEstimate"),
            "days_out": days_out,
        })

    # Sort each day's list and the dict
    for d_str in by_date:
        by_date[d_str].sort(key=lambda x: x["tk"])
    sorted_dates = sorted(by_date.keys())

    flags = []
    if in_u_count > 0:
        flags.append("earnings_in_universe")
    # Heavy days
    heavy = [(d, len(items)) for d, items in by_date.items() if len(items) >= 5]
    if heavy:
        flags.append("earnings_heavy_day")

    next_day = sorted_dates[0] if sorted_dates else None
    next_day_tickers = [it["tk"] for it in by_date.get(next_day, [])][:8] if next_day else []
    headline = f"{in_u_count} in-universe earnings in {days}d across {len(sorted_dates)} days"
    if next_day:
        headline += f"; next: {next_day} = {','.join(next_day_tickers)}"

    if json_mode:
        emit(step_result("earnings_universe", ok=True, headline=headline,
                         data={"by_date": {d: by_date[d] for d in sorted_dates},
                               "total_in_universe": in_u_count,
                               "heavy_days": [d for d, _ in heavy]},
                         flags=flags))
    else:
        print(f"=== Universe earnings ({days}d) ===")
        print(f"  {in_u_count} in-universe across {len(sorted_dates)} days")
        for d_str in sorted_dates:
            items = by_date[d_str]
            print(f"\n  {d_str} (T+{items[0]['days_out']}, {len(items)} names):")
            for it in items[:20]:
                hour = it.get("hour", "")
                hour_s = f"[{hour}]" if hour else ""
                est = f"est={it['eps_est']}" if it.get("eps_est") else ""
                print(f"    {it['tk']:<6}{hour_s:<7}{est}")
    return 0


def _cmd_history(ticker: str, json_mode: bool = False) -> int:
    """Pull last 4-8 quarter earnings + compute next-day price reaction.

    The "period" field is fiscal quarter-end, not the report date. As a proxy
    for report date, we use period + ~6 weeks (most companies report 4-6 weeks
    after quarter-end). For the close-day return, we look at the first trading
    day after that proxy.
    """
    from _terse import emit, step_result
    from _apikeys import has_key
    from datetime import timedelta as _td

    if not has_key("FINNHUB_API_KEY"):
        if json_mode:
            emit(step_result("earnings_history", ok=False,
                             headline="FINNHUB_API_KEY required",
                             errors=["set FINNHUB_API_KEY"]))
        else:
            print("FINNHUB_API_KEY required for --history", file=sys.stderr)
        return 1

    from _providers import finnhub
    fh = finnhub()
    h = fh.earnings_history(ticker)
    if not h:
        if json_mode:
            emit(step_result("earnings_history", ok=False,
                             headline=f"{ticker}: no earnings history",
                             errors=["no data"]))
        else:
            print(f"{ticker}: no earnings history")
        return 1

    # Pull ~2 years of OHLC for next-day-reaction lookup
    tk = yf.Ticker(ticker)
    hist = tk.history(period="2y")
    if hist.empty:
        hist = None
    else:
        hist.index = hist.index.tz_localize(None) if hist.index.tz else hist.index

    items = []
    for r in h:
        period = r.get("period")
        if not period:
            continue
        try:
            period_dt = date.fromisoformat(period)
        except Exception:
            continue
        # Approximate report date: period_end + 5 weeks (typical reporting lag)
        approx_report = period_dt + _td(days=35)
        # 1-day reaction: open of next trading day vs close of prior trading day
        one_d_pct = None
        report_close = None
        prev_close = None
        if hist is not None and not hist.empty:
            # Find first trading day on or after approx_report
            after = hist[hist.index.date >= approx_report]
            if not after.empty:
                report_idx = hist.index.get_loc(after.index[0])
                if report_idx > 0 and report_idx < len(hist):
                    prev_close = float(hist.iloc[report_idx - 1]["Close"])
                    report_close = float(hist.iloc[report_idx]["Close"])
                    one_d_pct = round((report_close / prev_close - 1) * 100, 2) if prev_close else None
        items.append({
            "period": period,
            "approx_report": approx_report.isoformat(),
            "actual": r.get("actual"),
            "estimate": r.get("estimate"),
            "surprise_pct": r.get("surprisePercent"),
            "beat": (r.get("actual") or 0) > (r.get("estimate") or 0),
            "1d_reaction_pct": one_d_pct,
            "prev_close": prev_close,
            "report_close": report_close,
        })

    # Profile stats
    surprises = [it["surprise_pct"] for it in items if it.get("surprise_pct") is not None]
    reactions = [it["1d_reaction_pct"] for it in items if it.get("1d_reaction_pct") is not None]
    beats = sum(1 for it in items if it.get("beat"))
    profile = {
        "beat_rate": round(beats / len(items), 2) if items else None,
        "avg_surprise_pct": round(sum(surprises) / len(surprises), 2) if surprises else None,
        "avg_1d_reaction_pct": round(sum(reactions) / len(reactions), 2) if reactions else None,
        "max_1d_reaction_pct": max(reactions, key=abs) if reactions else None,
        "n_quarters": len(items),
    }

    headline = (f"{ticker.upper()}: beat_rate={profile['beat_rate']} "
                f"avg_1d_reaction={profile['avg_1d_reaction_pct']}% "
                f"max_1d={profile['max_1d_reaction_pct']}% (n={profile['n_quarters']})")

    if json_mode:
        emit(step_result("earnings_history", ok=True, headline=headline,
                         data={"ticker": ticker.upper(), "profile": profile, "history": items}))
    else:
        print(f"\n=== {ticker.upper()} earnings history ===")
        print(f"  Beat rate:           {profile['beat_rate']}")
        print(f"  Avg surprise %:      {profile['avg_surprise_pct']}")
        print(f"  Avg 1d reaction %:   {profile['avg_1d_reaction_pct']}")
        print(f"  Max 1d reaction %:   {profile['max_1d_reaction_pct']}")
        print()
        print(f"{'Period':<12}{'Approx Rpt':<14}{'Actual':>8}{'Est':>8}{'Srp%':>8}"
              f"{'Beat':>6}{'1d %':>8}")
        for it in items:
            beat_s = "✓" if it.get("beat") else "✗"
            print(f"{it['period']:<12}{it['approx_report']:<14}"
                  f"{(it.get('actual') or 0):>8.2f}"
                  f"{(it.get('estimate') or 0):>8.2f}"
                  f"{(it.get('surprise_pct') or 0):>8.2f}"
                  f"{beat_s:>6}"
                  f"{(it.get('1d_reaction_pct') or 0):>8.2f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--from-watchlist", action="store_true")
    ap.add_argument("--days", type=int, default=30, help="Show earnings within next N days")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-em", action="store_true",
                    help="Skip expected-move computation (faster; for runbook)")
    ap.add_argument("--history", help="Show historical earnings reactions for ticker")
    ap.add_argument("--universe", action="store_true",
                    help="Scan entire working universe (not just hardcoded list)")
    args = ap.parse_args()
    from _terse import emit, step_result

    # ---------- HISTORY MODE ----------
    if args.history:
        return _cmd_history(args.history, json_mode=args.json)

    # ---------- UNIVERSE MODE ----------
    if args.universe:
        return _cmd_universe_calendar(args.days, json_mode=args.json)

    tickers = [t.upper() for t in args.tickers] or (watchlist_tickers() if args.from_watchlist else [])
    if not tickers:
        print("No tickers provided. Pass names or use --from-watchlist.", file=sys.stderr)
        return 1

    today = date.today()
    rows = []

    # PRIMARY: Finnhub bulk earnings calendar. ONE call gets the full universe;
    # filter locally to requested tickers.
    fh_dates: dict[str, str] = {}  # ticker -> next ER date iso
    if has_key("FINNHUB_API_KEY"):
        try:
            from _providers import finnhub as _fh_factory
            fh = _fh_factory()
            cal = fh.earnings_calendar(days_ahead=args.days)
            if cal:
                # Take the earliest future date per symbol
                for entry in cal:
                    sym = entry.get("symbol", "").upper()
                    d_str = entry.get("date")
                    if not sym or not d_str:
                        continue
                    if sym in fh_dates:
                        continue  # earliest already taken (cal is sorted asc)
                    fh_dates[sym] = d_str
        except Exception:
            pass

    for t in tickers:
        next_dt = None
        days_out = None
        # Try Finnhub first
        if t in fh_dates:
            try:
                next_dt = date.fromisoformat(fh_dates[t])
                days_out = (next_dt - today).days
            except Exception:
                next_dt = None
        # Fallback: yfinance per-ticker. Skip when --no-em and Finnhub-bulk
        # is the canonical source (the runbook's fast path).
        if next_dt is None:
            if args.no_em and fh_dates:
                # Finnhub didn't have it → no upcoming earnings in window. Skip.
                continue
            tk = yf.Ticker(t)
            try:
                dates = tk.get_earnings_dates(limit=4)
            except Exception as e:
                rows.append({"Ticker": t, "Next ER": "err", "Days": "", "ExpMove%": "", "NextExp": str(e)[:40]})
                continue
            if dates is None or dates.empty:
                rows.append({"Ticker": t, "Next ER": "n/a", "Days": "", "ExpMove%": "", "NextExp": ""})
                continue
            future = dates[dates.index.date > today] if hasattr(dates.index, "date") else dates
            if future.empty:
                rows.append({"Ticker": t, "Next ER": "past", "Days": "", "ExpMove%": "", "NextExp": ""})
                continue
            next_dt = future.index[0].date() if hasattr(future.index[0], "date") else None
            days_out = (next_dt - today).days if next_dt else None

        if days_out is None or days_out > args.days or days_out < 0:
            continue

        # Expected move from yfinance options chain (skip if --no-em)
        em, nearest_exp = (None, "")
        spot = 0
        if not args.no_em:
            tk = yf.Ticker(t)
            try:
                hist = tk.history(period="5d")
                spot = float(hist["Close"].iloc[-1]) if not hist.empty else 0
            except Exception:
                spot = 0
            if spot:
                em, nearest_exp = expected_move_pct(tk, spot)
        rows.append({
            "Ticker": t,
            "Next ER": str(next_dt),
            "Days": days_out,
            "Spot": round(spot, 2),
            "ExpMove%": em if em is not None else "",
            "NextExp": nearest_exp,
        })

    if args.json:
        compact = []
        for r in rows:
            d = r.get("Days")
            compact.append({
                "tk": r.get("Ticker"),
                "next_er": r.get("Next ER"),
                "days_out": d if isinstance(d, int) else None,
                "spot": r.get("Spot"),
                "exp_move_pct": r.get("ExpMove%") or None,
            })
        compact.sort(key=lambda x: (x["days_out"] is None, x["days_out"] or 9999))
        # Flags: any earnings within 7d
        flags = []
        within_7 = [r["tk"] for r in compact if isinstance(r["days_out"], int) and 0 <= r["days_out"] <= 7]
        if within_7:
            flags.append("earnings_within_7d")
        headline = f"{len(within_7)} earnings within 7d ({','.join(within_7[:5])})" if within_7 else f"none within 7d of {len(tickers)} tickers"
        emit(step_result("earnings", ok=True, headline=headline,
                         data={"items": compact, "within_7d": within_7}, flags=flags))
        return 0

    if not rows:
        print(f"No earnings in the next {args.days} days for {len(tickers)} tickers.")
        return 0

    df = pd.DataFrame(rows).sort_values("Days", na_position="last")
    print(f"=== Earnings within {args.days} days ===\n")
    print(df.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
