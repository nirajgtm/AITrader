#!/usr/bin/env python3
"""LEAP call strategy backtest using Black-Scholes synthetic option prices.

Strategy: "Anchor LEAP DCA" -- synthesized from research consensus
(Bogleheads HFEA threads, Ayres/Nalebuff Lifecycle Investing, Spintwig PMCC
backtests, Lorintine Anchor Strategy, r/options/r/thetagang threads).

Default rules (configurable via CLI):
  - Underlying: SPY (default), QQQ, TQQQ supported
  - Entry: monthly DCA on 1st trading day, plus opportunistic on VIX < 18
    OR underlying RSI < 40. Skip if VIX > 30.
  - Strike: 0.80 delta (deep ITM)
  - DTE at entry: 540 calendar days (~18 months)
  - Sizing: 5% of equity per LEAP, max 6 concurrent
  - Roll: when DTE drops to 90, close + buy fresh 18mo at 0.80 delta
  - Stop: close if underlying drops 25% from LEAP entry spot
  - No profit target (let winners ride to roll)

Pricing assumptions:
  - IV from VIX scaled per-underlying:
      SPY ~ VIX * 0.95
      QQQ ~ VIX * 1.15
      TQQQ ~ VIX * 3.0  (per Leung/Sircar LETF IV scaling, leverage^2 effect)
  - Risk-free rate: 4.5% (rough proxy for 1Y treasury median in window)
  - No skew adjustment, no dividend (rough; SPY dividends ~1.3% would help LEAP)
  - Bid/ask: assume 2% transaction cost on each open + close

Comparison: vs underlying buy/hold over the same window with the same
starting equity, assuming the cash NOT in LEAPs sits in cash earning 0.

Usage:
  backtest_leap.py --underlying SPY --start 2019-01-01 --end 2024-12-31
  backtest_leap.py --underlying QQQ --start 2019-01-01 --end 2024-12-31 --iv-mult 1.15
  backtest_leap.py --underlying TQQQ --start 2019-01-01 --end 2024-12-31 \\
                   --iv-mult 3.0 --stop-pct 0.40
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from pathlib import Path

from _bs import bs_call_price, strike_for_delta
from _common import STATE_DIR

BACKTEST_DIR = STATE_DIR / "backtest"

IV_SCALE_BY_UNDERLYING = {
    "SPY": 0.95, "QQQ": 1.15, "IWM": 1.30,
    "TQQQ": 3.0, "SQQQ": 3.0, "UPRO": 3.0, "SOXL": 3.5,
}


@dataclass
class LeapPosition:
    open_date: date
    open_spot: float
    strike: float
    expiry: date
    open_premium: float
    contracts: float                # fractional allowed for sizing math
    last_premium: float = 0.0
    closed: bool = False
    close_date: date | None = None
    close_premium: float = 0.0
    close_reason: str = ""
    pnl_dollars: float = 0.0
    history: list = field(default_factory=list)  # [(date_iso, premium), ...]


def _pull_daily(ticker: str, start: date, end: date):
    import yfinance as yf
    df = yf.Ticker(ticker).history(
        start=(start - timedelta(days=60)).isoformat(),
        end=(end + timedelta(days=2)).isoformat(),
    )
    if df.empty:
        return None
    return df


def _rsi(closes, period: int = 14):
    """Simple RSI(14)."""
    import pandas as pd
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))


def _is_first_trading_day_of_month(idx, dates_list) -> bool:
    """idx is the position in dates_list."""
    if idx == 0:
        return True
    cur = dates_list[idx]
    prev = dates_list[idx - 1]
    return cur.month != prev.month


def _close_position(pos: LeapPosition, today: date, spot: float,
                   iv: float, rate: float, reason: str) -> None:
    ttm = max(0.0, (pos.expiry - today).days / 365.0)
    premium = bs_call_price(spot, pos.strike, ttm, iv, rate)
    # Apply 1% slippage on close (half of round-trip 2% cost)
    premium_after_slip = premium * 0.99
    pos.closed = True
    pos.close_date = today
    pos.close_premium = round(premium_after_slip, 4)
    pos.close_reason = reason
    pos.last_premium = premium_after_slip
    pos.pnl_dollars = round((premium_after_slip - pos.open_premium) * pos.contracts * 100, 2)


def _open_position(today: date, spot: float, iv: float, rate: float,
                  dte_days: int, target_delta: float, size_dollars: float
                  ) -> LeapPosition | None:
    ttm = dte_days / 365.0
    strike = strike_for_delta(spot, ttm, target_delta, iv, rate)
    premium = bs_call_price(spot, strike, ttm, iv, rate)
    if premium <= 0:
        return None
    # Apply 1% slippage on open (other half of round-trip)
    premium_after_slip = premium * 1.01
    contract_cost_dollars = premium_after_slip * 100  # 1 contract = 100 shares
    if contract_cost_dollars <= 0:
        return None
    contracts = size_dollars / contract_cost_dollars
    return LeapPosition(
        open_date=today,
        open_spot=spot,
        strike=round(strike, 2),
        expiry=today + timedelta(days=dte_days),
        open_premium=round(premium_after_slip, 4),
        contracts=round(contracts, 4),
        last_premium=premium_after_slip,
    )


def run_backtest(args: argparse.Namespace) -> dict:
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    iv_mult = args.iv_mult or IV_SCALE_BY_UNDERLYING.get(args.underlying.upper(), 1.0)

    df_under = _pull_daily(args.underlying, start, end)
    df_vix = _pull_daily("^VIX", start, end)
    if df_under is None or df_vix is None:
        print("[ERR] failed to pull underlying or VIX history", file=sys.stderr)
        sys.exit(1)

    # Align both on dates within window
    import pandas as pd
    closes = df_under["Close"]
    rsi14 = _rsi(closes)
    vix_close = df_vix["Close"].reindex(closes.index, method="ffill")

    dates = [ts.date() for ts in closes.index if start <= ts.date() <= end]
    if not dates:
        print(f"[ERR] no trading days in {start}..{end}", file=sys.stderr)
        sys.exit(1)

    spot_by_date = {ts.date(): float(c) for ts, c in zip(closes.index, closes.values)}
    rsi_by_date = {ts.date(): float(r) for ts, r in zip(rsi14.index, rsi14.values)}
    vix_by_date = {ts.date(): float(v) for ts, v in zip(vix_close.index, vix_close.values)}

    equity = float(args.start_equity)
    # Initial cash placement
    if args.cash_in_underlying:
        first_spot = spot_by_date[dates[0]]
        cash_shares = equity / first_spot
        equity = 0.0
    else:
        cash_shares = 0.0
    open_positions: list[LeapPosition] = []
    closed_positions: list[LeapPosition] = []
    equity_curve = []  # (date_iso, equity, leap_value, cash_value)
    skipped_high_vix = 0
    skipped_concurrency = 0
    skipped_extended = 0

    for i, today in enumerate(dates):
        spot = spot_by_date[today]
        rsi = rsi_by_date.get(today)
        vix = vix_by_date.get(today)
        if vix is None:
            continue
        iv = (vix / 100.0) * iv_mult
        rate = args.rate

        # If --cash-in-underlying, convert prior-day cash into shares once at start
        # (idempotent: we re-derive cash equivalent each iteration from shares)
        if args.cash_in_underlying:
            cash_value_today = cash_shares * spot
            equity = cash_value_today  # equity here is the cash-leg only; LEAPs counted separately
        # else: equity stays as raw cash, no daily mark needed

        # 1) Mark all open positions
        leap_value = 0.0
        for pos in open_positions:
            ttm = max(0.0, (pos.expiry - today).days / 365.0)
            mark = bs_call_price(spot, pos.strike, ttm, iv, rate)
            pos.last_premium = mark
            leap_value += mark * pos.contracts * 100

        # 2) Roll / stop / time-based closes
        survivors: list[LeapPosition] = []
        for pos in open_positions:
            dte = (pos.expiry - today).days
            drawdown = (spot - pos.open_spot) / pos.open_spot
            close_reason = None
            if dte <= args.roll_dte:
                close_reason = "roll"
            elif drawdown <= -args.stop_pct:
                close_reason = "stop"
            elif args.profit_target and pos.last_premium >= pos.open_premium * (1 + args.profit_target):
                close_reason = "target"

            if close_reason:
                _close_position(pos, today, spot, iv, rate, close_reason)
                proceeds = pos.close_premium * pos.contracts * 100
                if args.cash_in_underlying:
                    cash_shares += proceeds / spot
                else:
                    equity += proceeds
                closed_positions.append(pos)
            else:
                survivors.append(pos)
        open_positions = survivors

        # 3) Open new positions (entry signals)
        if vix > args.vix_max:
            skipped_high_vix += 1
        else:
            wants_entry = False
            is_pullback = rsi is not None and rsi < args.rsi_pullback
            is_extended = (args.rsi_extended_skip is not None and rsi is not None
                          and rsi > args.rsi_extended_skip)

            if is_pullback:
                wants_entry = True  # pullback always wins
            elif _is_first_trading_day_of_month(i, dates):
                wants_entry = True
            elif vix < args.vix_low_entry:
                wants_entry = True

            # Extended-skip filter: gates monthly DCA + vol-cheap triggers,
            # never the pullback trigger (which is the dip-buy case by design).
            if wants_entry and is_extended and not is_pullback:
                skipped_extended += 1
                wants_entry = False

            if wants_entry and len(open_positions) >= args.max_concurrent:
                skipped_concurrency += 1
                wants_entry = False

            if wants_entry:
                # Sizing base = total equity (LEAP value + cash leg)
                cash_leg_dollars = (cash_shares * spot) if args.cash_in_underlying else equity
                leap_value_now = sum(p.last_premium * p.contracts * 100 for p in open_positions)
                total_equity_now = cash_leg_dollars + leap_value_now
                size_dollars = total_equity_now * (args.size_pct / 100.0)
                if size_dollars > cash_leg_dollars * 0.95:
                    size_dollars = cash_leg_dollars * 0.95
                if size_dollars > 0:
                    pos = _open_position(today, spot, iv, rate,
                                        args.dte_days, args.delta_target,
                                        size_dollars)
                    if pos is not None:
                        contract_cost = pos.open_premium * pos.contracts * 100
                        if args.cash_in_underlying:
                            cash_shares -= contract_cost / spot
                        else:
                            equity -= contract_cost
                        open_positions.append(pos)

        # 4) Mark equity curve
        leap_value_after = sum(p.last_premium * p.contracts * 100 for p in open_positions)
        cash_leg = (cash_shares * spot) if args.cash_in_underlying else equity
        total_equity = cash_leg + leap_value_after
        equity_curve.append((today.isoformat(), round(total_equity, 2),
                            round(leap_value_after, 2), round(cash_leg, 2)))

    # End of window: close all remaining at last spot
    final_today = dates[-1]
    final_spot = spot_by_date[final_today]
    final_vix = vix_by_date[final_today]
    final_iv = (final_vix / 100.0) * iv_mult
    for pos in open_positions:
        _close_position(pos, final_today, final_spot, final_iv, args.rate, "end_of_window")
        proceeds = pos.close_premium * pos.contracts * 100
        if args.cash_in_underlying:
            cash_shares += proceeds / final_spot
        else:
            equity += proceeds
        closed_positions.append(pos)
    open_positions = []
    if args.cash_in_underlying:
        equity = cash_shares * final_spot

    # Underlying buy/hold benchmark over same window
    start_spot = spot_by_date[dates[0]]
    end_spot = spot_by_date[dates[-1]]
    underlying_return_pct = (end_spot - start_spot) / start_spot * 100
    underlying_final = args.start_equity * (1 + underlying_return_pct / 100)

    strategy_return_pct = (equity - args.start_equity) / args.start_equity * 100
    alpha_pp = strategy_return_pct - underlying_return_pct

    # Drawdown analysis
    peak = args.start_equity
    max_dd = 0.0
    max_dd_date = None
    for d, eq, *_ in equity_curve:
        if eq > peak:
            peak = eq
        dd = (eq - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd
            max_dd_date = d

    # Closed-position outcome breakdown
    by_reason: dict = {}
    for p in closed_positions:
        by_reason.setdefault(p.close_reason, {"n": 0, "pnl": 0.0})
        by_reason[p.close_reason]["n"] += 1
        by_reason[p.close_reason]["pnl"] += p.pnl_dollars
    for k in by_reason:
        by_reason[k]["pnl"] = round(by_reason[k]["pnl"], 2)

    print(f"\n=== LEAP backtest: {args.underlying} {args.start} -> {args.end} ===")
    print(f"Params: delta={args.delta_target}  DTE={args.dte_days}  size={args.size_pct}%  "
          f"max_concurrent={args.max_concurrent}  stop={args.stop_pct*100:.0f}%  "
          f"roll_dte={args.roll_dte}  iv_mult={iv_mult}")
    print(f"\nClosed positions: {len(closed_positions)}")
    for reason, b in sorted(by_reason.items()):
        print(f"  {reason:<14} n={b['n']:<3}  pnl={b['pnl']:+,.2f}")
    print(f"Skipped (VIX > {args.vix_max}): {skipped_high_vix}")
    print(f"Skipped (concurrency cap):     {skipped_concurrency}")
    if args.rsi_extended_skip is not None:
        print(f"Skipped (RSI > {args.rsi_extended_skip} extended): {skipped_extended}")
    print(f"\nStrategy final equity: ${equity:,.2f}  ({strategy_return_pct:+.2f}%)")
    print(f"{args.underlying} buy/hold:       ${underlying_final:,.2f}  ({underlying_return_pct:+.2f}%)")
    print(f"Alpha vs underlying:   {alpha_pp:+.2f} pp")
    print(f"Max drawdown:          {max_dd:.2f}% on {max_dd_date}")

    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    out_path = BACKTEST_DIR / f"leap_{args.underlying}_{args.start}_{args.end}.json"
    with out_path.open("w") as f:
        json.dump({
            "params": vars(args),
            "iv_mult": iv_mult,
            "totals": {
                "final_equity": round(equity, 2),
                "strategy_return_pct": round(strategy_return_pct, 2),
                "underlying_final": round(underlying_final, 2),
                "underlying_return_pct": round(underlying_return_pct, 2),
                "alpha_pp": round(alpha_pp, 2),
                "max_drawdown_pct": round(max_dd, 2),
                "max_drawdown_date": max_dd_date,
                "closed_positions": len(closed_positions),
                "skipped_high_vix": skipped_high_vix,
                "skipped_concurrency": skipped_concurrency,
            },
            "by_close_reason": by_reason,
            "equity_curve_sample": equity_curve[::21],  # ~monthly samples
        }, f, indent=2, default=str)
    print(f"\nWrote {out_path}")
    return {"equity": equity, "alpha": alpha_pp, "dd": max_dd}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlying", required=True, help="SPY, QQQ, TQQQ, etc")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--start-equity", type=float, default=10000)
    ap.add_argument("--delta-target", type=float, default=0.80)
    ap.add_argument("--dte-days", type=int, default=540, help="~18 months")
    ap.add_argument("--roll-dte", type=int, default=90)
    ap.add_argument("--size-pct", type=float, default=5.0)
    ap.add_argument("--max-concurrent", type=int, default=6)
    ap.add_argument("--stop-pct", type=float, default=0.25,
                    help="close if underlying down this fraction from LEAP entry")
    ap.add_argument("--profit-target", type=float, default=None,
                    help="optional: close at LEAP premium up by this fraction")
    ap.add_argument("--vix-max", type=float, default=30.0,
                    help="skip new entries if VIX above this")
    ap.add_argument("--vix-low-entry", type=float, default=18.0,
                    help="opportunistic entry trigger when VIX below this")
    ap.add_argument("--rsi-pullback", type=float, default=40.0,
                    help="opportunistic entry trigger when RSI(14) below this")
    ap.add_argument("--rsi-extended-skip", type=float, default=None,
                    help="if RSI(14) above this, skip monthly-DCA and vol-cheap "
                         "triggers (pullback trigger still fires). Off by default.")
    ap.add_argument("--rate", type=float, default=0.045)
    ap.add_argument("--iv-mult", type=float, default=None,
                    help="multiplier on VIX/100 to derive IV; defaults from table")
    ap.add_argument("--cash-in-underlying", action="store_true",
                    help="park uninvested cash in the underlying instead of 0%% cash")
    args = ap.parse_args()
    run_backtest(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
