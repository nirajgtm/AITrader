#!/usr/bin/env python3
"""Benchmark — compare real-book P&L against SPY (or chosen) buy-and-hold
over the same window, so the question 'should I just be in SPY?' is on
the page every brief.

Method: take the date of the first OPEN entry in state/ledger.jsonl as the
start. Compare cumulative realized P&L from the real ledger against what
the same notional would have made buying SPY on that date and holding to
today.

Notional sizing: by default, uses the sum of (entry * qty) across all OPEN
entries as the comparable notional. This is rough — it treats every trade
as if its capital was deployed on day-1 in SPY, which overstates SPY's run
when the real book opened and closed positions over time. Pass --equal-cap
to instead use a single fixed notional (default $10k) for the SPY leg, which
is cleaner for a research feed where you don't have a fixed account.

Usage:
  benchmark.py spy [--ticker SPY] [--start YYYY-MM-DD] [--equal-cap 10000]
                   [--include-shadow] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime

from _common import fmt_usd, read_ledger


def _spy_return(ticker: str, start: date, end: date) -> tuple[float, float, float]:
    """Return (start_close, end_close, pct_return) for SPY-or-chosen between dates."""
    import yfinance as yf
    from datetime import timedelta
    df = yf.Ticker(ticker).history(
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
    )
    if df.empty:
        raise RuntimeError(f"no {ticker} data between {start} and {end}")
    start_close = float(df.iloc[0]["Close"])
    end_close = float(df.iloc[-1]["Close"])
    return start_close, end_close, (end_close - start_close) / start_close * 100


def _ledger_realized_pnl(book: str = "real") -> float:
    return sum(float(e.get("pnl", 0))
               for e in read_ledger(book) if e.get("kind") == "CLOSE")


def _first_open_date(book: str = "real") -> date | None:
    for e in read_ledger(book):
        if e.get("kind") == "OPEN":
            ts = e.get("ts") or e.get("opened_at")
            if ts:
                # ts may be ISO datetime or YYYY-MM-DD
                try:
                    return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
                except ValueError:
                    return date.fromisoformat(ts[:10])
    return None


def _notional_deployed(book: str = "real") -> float:
    total = 0.0
    for e in read_ledger(book):
        if e.get("kind") != "OPEN":
            continue
        # Options: premium per contract * 100 * qty
        if e.get("vehicle") in ("long_call", "long_put", "debit_spread", "calendar"):
            premium = float(e.get("premium") or e.get("entry") or 0)
            qty = float(e.get("qty") or 0)
            total += premium * qty * 100
        else:
            entry = float(e.get("entry") or e.get("fill") or 0)
            qty = float(e.get("qty") or 0)
            total += entry * qty
    return total


def cmd_spy(args: argparse.Namespace) -> int:
    if args.start:
        start = date.fromisoformat(args.start)
    else:
        start = _first_open_date("real")
        if start is None:
            print("[ERR] no OPEN entry in real ledger; pass --start YYYY-MM-DD", file=sys.stderr)
            return 1
    end = date.today()

    real_pnl = _ledger_realized_pnl("real")
    shadow_pnl = _ledger_realized_pnl("shadow") if args.include_shadow else 0.0

    try:
        spy_start, spy_end, spy_pct = _spy_return(args.ticker, start, end)
    except Exception as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        return 1

    if args.equal_cap:
        notional = float(args.equal_cap)
        notional_source = f"--equal-cap {notional:.0f}"
    else:
        notional = _notional_deployed("real")
        notional_source = "ledger sum(entry*qty)"
        if notional == 0:
            notional = 10000.0
            notional_source = "no positions, defaulted 10000"

    spy_dollars = notional * spy_pct / 100

    delta = real_pnl - spy_dollars

    headline = (f"real_pnl={real_pnl:+.2f} spy_buy_hold={spy_dollars:+.2f} "
                f"delta={delta:+.2f} ({args.ticker} {spy_pct:+.2f}% over {(end - start).days}d)")

    if args.json:
        print(json.dumps({
            "start": start.isoformat(), "end": end.isoformat(),
            "ticker": args.ticker,
            "spy_start_close": spy_start, "spy_end_close": spy_end,
            "spy_pct_return": round(spy_pct, 4),
            "notional": round(notional, 2),
            "notional_source": notional_source,
            "spy_buy_hold_pnl": round(spy_dollars, 2),
            "real_pnl": round(real_pnl, 2),
            "shadow_pnl": round(shadow_pnl, 2) if args.include_shadow else None,
            "delta": round(delta, 2),
        }))
        return 0

    print(f"=== Benchmark vs {args.ticker} buy/hold ({start} to {end}) ===")
    print(f"Notional: {fmt_usd(notional)}  ({notional_source})")
    print(f"{args.ticker} {spy_start:.2f} -> {spy_end:.2f}  ({spy_pct:+.2f}%)")
    print(f"{args.ticker} buy/hold P&L:  {fmt_usd(spy_dollars)}")
    print(f"Real book P&L:        {fmt_usd(real_pnl)}")
    if args.include_shadow:
        print(f"Shadow book P&L:      {fmt_usd(shadow_pnl)}")
    print(f"Delta (real - {args.ticker}): {fmt_usd(delta)}")
    if delta < 0:
        print(f"  -> {args.ticker} buy/hold beats real by {fmt_usd(-delta)}")
    else:
        print(f"  -> Real beats {args.ticker} buy/hold by {fmt_usd(delta)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("spy", help="compare real ledger to SPY buy/hold")
    sp.add_argument("--ticker", default="SPY",
                    help="benchmark ticker (default SPY; QQQ, etc work)")
    sp.add_argument("--start", default=None,
                    help="start date YYYY-MM-DD; default = first OPEN in ledger")
    sp.add_argument("--equal-cap", type=float, default=None,
                    help="fixed notional for SPY leg instead of summing ledger entries")
    sp.add_argument("--include-shadow", action="store_true")
    sp.add_argument("--json", action="store_true")
    args = ap.parse_args()
    return {"spy": cmd_spy}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
