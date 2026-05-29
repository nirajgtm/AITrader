#!/usr/bin/env python3
"""Portfolio state operations.

Usage:
  portfolio.py show
  portfolio.py set-cash --amount 1000 [--note "..."]
  portfolio.py reconcile --reported 1003.42 [--note "..."]
  portfolio.py add-position --ticker X --kind stock --qty 6 --entry 50.10 \
      --stop 47 --target 56 [--invalidation "..."] [--horizon-days 7] \
      [--strategy NAME] [--option-symbol "AAPL260516C180"]
  portfolio.py close-position --ticker X [--qty N] --fill 55.50 [--reason target]
  portfolio.py set-cooldown --days N
  portfolio.py mtm-sync         # re-mark open positions and update equity
"""
from __future__ import annotations

import argparse
import json
import sys

from _common import (
    append_ledger,
    fmt_usd,
    load_portfolio,
    now_iso,
    save_portfolio,
    today_str,
)


_DEPRECATED_MSG = (
    "[ERR] This subcommand operates on the legacy $1k challenge book, which is "
    "deprecated as of CONSTITUTION v2.0 (2026-04-28). The system is cash-agnostic. "
    "Use `add-user-position` / `close-user-position` for the personal book. "
    "Historical challenge fields are preserved in portfolio.json under "
    "`legacy_challenge` for audit only."
)


def _is_v3_cash_agnostic(p: dict) -> bool:
    """True iff the v3 cash-agnostic schema is active (user_positions present)."""
    return "user_positions" in p


def _last_price(ticker: str) -> float | None:
    """Best-effort last-trade price for a ticker via yfinance.

    Imported lazily so portfolio.py can run even without network for `show`.
    """
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="1d")
        if h.empty:
            return None
        return float(h["Close"].iloc[-1])
    except Exception:
        return None


def _position_market_value(pos: dict) -> float:
    """Live-ish market value of a position. Falls back to cost basis."""
    last = _last_price(pos["ticker"]) or pos["entry"]
    if pos.get("kind") == "option":
        # Without a live option-quote source, use last-known mark if recorded,
        # else cost basis. The system writes mark via mtm.py mark-option (manual).
        last_mark = pos.get("last_mark", pos.get("entry"))
        return float(pos["qty"]) * float(last_mark) * 100
    return float(pos["qty"]) * float(last)


def _recalc_equity(p: dict) -> float:
    """Re-mark open positions and update equity in-place. Returns new equity."""
    equity = float(p["cash"]) + sum(_position_market_value(pos) for pos in p.get("positions", []))
    p["equity"] = round(equity, 2)
    # High-water mark
    hw = float(p.get("equity_high_water", p["starting_equity"]))
    if equity > hw:
        p["equity_high_water"] = round(equity, 2)
    return equity


def _consecutive_losers_from_ledger(n: int = 2) -> bool:
    """Read real ledger and return True if the last `n` CLOSE entries are losses."""
    from _common import read_ledger
    closes = [e for e in read_ledger("real") if e.get("kind") == "CLOSE"]
    if len(closes) < n:
        return False
    last_n = closes[-n:]
    return all(float(e.get("pnl", 0)) < 0 for e in last_n)


def _print_pos_line(pos: dict) -> None:
    line = (f"    - {pos.get('ticker')} {pos.get('kind','stock')} {pos.get('side','LONG')} "
            f"qty={pos.get('qty')} entry={pos.get('entry')} stop={pos.get('stop')} "
            f"target={pos.get('target')}")
    if pos.get("option_symbol"):
        line += f"  symbol={pos['option_symbol']}"
    if pos.get("horizon_expires_at"):
        line += f"  expires={pos['horizon_expires_at']}"
    if pos.get("strategy"):
        line += f"  strategy={pos['strategy']}"
    print(line)
    if pos.get("invalidation"):
        print(f"        invalidation: {pos['invalidation']}")
    if pos.get("opened_at"):
        print(f"        opened: {pos['opened_at']}")
    if pos.get("thesis"):
        print(f"        thesis: {pos['thesis']}")


def cmd_show(_args: argparse.Namespace) -> int:
    p = load_portfolio()
    print("=== Portfolio ===")
    print(f"  Broker:         {p.get('broker')}  |  Account: {p.get('account_type')}  |  Options lvl: {p.get('options_level')}")
    print(f"  Schema:         v{p.get('version')} on {p.get('starting_date')}")
    print(f"  Cooldown days remaining: {p.get('cooldown_days_remaining', 0)}")
    print(f"  Consecutive losers: {p.get('consecutive_losers', 0)}")

    user_positions = p.get("user_positions", [])
    challenge_positions = p.get("positions", [])

    print(f"\n  User personal book ({len(user_positions)} position{'s' if len(user_positions)!=1 else ''}):")
    if not user_positions:
        print("    (none)")
    for pos in user_positions:
        _print_pos_line(pos)

    print(f"\n  Challenge book positions ({len(challenge_positions)}):")
    if not challenge_positions:
        print("    (none — challenge book deprecated per CONSTITUTION v2.0)")
    for pos in challenge_positions:
        _print_pos_line(pos)

    lc = p.get("legacy_challenge")
    if lc:
        print("\n  Legacy challenge fields (archival, deprecated "
              f"{lc.get('deprecated_at','')}):")
        print(f"    starting:      {fmt_usd(lc.get('starting_equity', 0))}")
        print(f"    cash:          {fmt_usd(lc.get('cash', 0))}")
        print(f"    equity:        {fmt_usd(lc.get('equity', 0))}")
        print(f"    high_water:    {fmt_usd(lc.get('equity_high_water', 0))}")
        print(f"    realized_pnl:  {fmt_usd(lc.get('realized_pnl', 0))}")

    if p.get("notes"):
        print(f"\n  Notes: {p['notes']}")
    return 0


def cmd_set_cash(args: argparse.Namespace) -> int:
    if _is_v3_cash_agnostic(load_portfolio()):
        print(_DEPRECATED_MSG, file=sys.stderr)
        return 1
    p = load_portfolio()
    old = p["cash"]
    p["cash"] = round(args.amount, 2)
    _recalc_equity(p)
    save_portfolio(p)
    append_ledger({
        "kind": "NOTE",
        "text": f"set-cash: {old} -> {args.amount}. {args.note or ''}".strip(),
    })
    print(json.dumps(p, indent=2))
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Reconcile against user-reported broker balance.

    Behavior:
      - Compute expected equity via re-MTM.
      - Compare to reported.
      - Save reported balance + reconcile timestamp.
      - If drift > $1: warn but do NOT silently overwrite cash; investigation is required.
      - If --trust-reported flag: also force cash to (reported - sum(position cost basis))
        so that future MTM lines up. Use only after manual investigation.
    """
    if _is_v3_cash_agnostic(load_portfolio()):
        print(_DEPRECATED_MSG, file=sys.stderr)
        return 1
    p = load_portfolio()
    expected = _recalc_equity(p)
    drift = args.reported - expected
    p["last_reconciled_balance"] = round(args.reported, 2)
    p["last_reconciled_at"] = today_str()

    if args.trust_reported and abs(drift) > 0.01:
        # Force cash to align with reported equity, holding positions at MTM.
        position_mv = sum(_position_market_value(pos) for pos in p.get("positions", []))
        p["cash"] = round(args.reported - position_mv, 2)
        _recalc_equity(p)
        append_ledger({
            "kind": "NOTE",
            "text": (f"reconcile (trust-reported): reported={args.reported} "
                     f"expected={expected:.2f} drift={drift:+.2f}; cash forced to {p['cash']}. "
                     f"{args.note or ''}").strip(),
        })
    else:
        append_ledger({
            "kind": "NOTE",
            "text": (f"reconcile: reported={args.reported} expected={expected:.2f} "
                     f"drift={drift:+.2f}. {args.note or ''}").strip(),
        })

    save_portfolio(p)
    print(f"Reported: {fmt_usd(args.reported)}")
    print(f"Expected: {fmt_usd(expected)}")
    print(f"Drift:    {drift:+.2f}")
    if abs(drift) > 1:
        print("[WARN] drift > $1. Investigate (missed fill, dividend, fee, slippage on stop-limit).")
        if not args.trust_reported:
            print("       Once investigated, re-run with --trust-reported to align cash.")
    return 0


def cmd_add_position(args: argparse.Namespace) -> int:
    p = load_portfolio()
    if _is_v3_cash_agnostic(p):
        print(_DEPRECATED_MSG, file=sys.stderr)
        return 1
    capital = args.qty * args.entry * (100 if args.kind == "option" else 1)
    if capital > p["cash"] + 0.01:
        print(f"[ERR] capital {capital:.2f} > cash {p['cash']:.2f}", file=sys.stderr)
        return 1
    pos: dict = {
        "ticker": args.ticker.upper(),
        "kind": args.kind,
        "side": args.side,
        "qty": args.qty,
        "entry": args.entry,
        "stop": args.stop,
        "target": args.target,
        "opened_at": today_str(),
    }
    if args.invalidation:
        pos["invalidation"] = args.invalidation
    if args.horizon_days:
        from datetime import datetime, timedelta
        expires = datetime.now().date() + timedelta(days=args.horizon_days)
        # crude business-days count: add weekend padding
        weekend_pad = sum(1 for i in range(args.horizon_days)
                          if (datetime.now().date() + timedelta(days=i)).weekday() >= 5)
        expires += timedelta(days=weekend_pad)
        pos["horizon_expires_at"] = expires.isoformat()
    if args.strategy:
        pos["strategy"] = args.strategy
    if args.option_symbol:
        pos["option_symbol"] = args.option_symbol
    if args.thesis:
        pos["thesis"] = args.thesis

    p["cash"] = round(p["cash"] - capital, 2)
    p["positions"].append(pos)
    _recalc_equity(p)
    save_portfolio(p)
    print(json.dumps(pos, indent=2))
    return 0


def cmd_close_position(args: argparse.Namespace) -> int:
    p = load_portfolio()
    if _is_v3_cash_agnostic(p):
        print(_DEPRECATED_MSG, file=sys.stderr)
        return 1
    positions = p["positions"]
    idx = next((i for i, x in enumerate(positions) if x["ticker"] == args.ticker.upper()), None)
    if idx is None:
        print(f"No open position in {args.ticker}", file=sys.stderr)
        return 1
    pos = positions[idx]
    fill = args.fill if args.fill is not None else pos["entry"]

    is_option = pos.get("kind") == "option"
    multiplier = 100 if is_option else 1

    if args.qty is not None and args.qty < pos["qty"]:
        closing_qty = args.qty
        proceeds = closing_qty * fill * multiplier
        cost = closing_qty * pos["entry"] * multiplier
        # mutate qty on remaining lot
        pos["qty"] -= closing_qty
        full_close = False
    else:
        closing_qty = pos["qty"]
        proceeds = closing_qty * fill * multiplier
        cost = closing_qty * pos["entry"] * multiplier
        positions.pop(idx)
        full_close = True

    side_sign = 1 if pos.get("side", "LONG") == "LONG" else -1
    pnl = (proceeds - cost) * side_sign
    p["cash"] = round(p["cash"] + proceeds, 2)
    p["realized_pnl"] = round(p.get("realized_pnl", 0.0) + pnl, 2)
    _recalc_equity(p)

    # Append CLOSE event to ledger automatically
    append_ledger({
        "kind": "CLOSE",
        "ticker": pos["ticker"],
        "qty": closing_qty,
        "exit": fill,
        "reason": args.reason,
        "pnl": round(pnl, 2),
        "full_close": full_close,
        "ref_opened_at": pos.get("opened_at"),
        "strategy": pos.get("strategy"),
    })

    # Auto-cooldown after 2 consecutive losers
    if pnl < 0 and full_close:
        if _consecutive_losers_from_ledger(n=2):
            p["cooldown_days_remaining"] = max(p.get("cooldown_days_remaining", 0), 1)
            p["consecutive_losers"] = 2
            print("[AUTO] 2 consecutive losers detected → cooldown set to 1 day.")
        else:
            p["consecutive_losers"] = p.get("consecutive_losers", 0) + 1
    elif pnl >= 0 and full_close:
        p["consecutive_losers"] = 0

    save_portfolio(p)
    print(f"Closed {closing_qty} of {pos['ticker']} @ {fill}. "
          f"Proceeds {fmt_usd(proceeds)}.  PnL {fmt_usd(pnl)}.")
    return 0


def cmd_add_user_position(args: argparse.Namespace) -> int:
    """Add a position to the user personal book (cash-agnostic, v3 schema).

    Unlike `add-position` (challenge book), this does NOT debit cash, does NOT
    auto-write an INTENT ledger entry, and does NOT enforce risk gates. It
    records an existing fill in the user's personal account so the morning
    brief can review it for ACT/HOLD/EXIT actions.
    """
    p = load_portfolio()
    if "user_positions" not in p:
        p["user_positions"] = []
    pos: dict = {
        "ticker": args.ticker.upper(),
        "kind": args.kind,
        "side": args.side,
        "qty": args.qty,
        "entry": args.entry,
        "stop": args.stop,
        "target": args.target,
        "opened_at": args.opened_at or today_str(),
    }
    if args.invalidation:
        pos["invalidation"] = args.invalidation
    if args.horizon_days:
        from datetime import datetime, timedelta
        expires = datetime.now().date() + timedelta(days=args.horizon_days)
        weekend_pad = sum(1 for i in range(args.horizon_days)
                          if (datetime.now().date() + timedelta(days=i)).weekday() >= 5)
        expires += timedelta(days=weekend_pad)
        pos["horizon_expires_at"] = expires.isoformat()
    if args.expires:
        pos["horizon_expires_at"] = args.expires
    if args.strategy:
        pos["strategy"] = args.strategy
    if args.option_symbol:
        pos["option_symbol"] = args.option_symbol
    if args.thesis:
        pos["thesis"] = args.thesis
    p["user_positions"].append(pos)
    save_portfolio(p)
    print(json.dumps(pos, indent=2))
    return 0


def cmd_close_user_position(args: argparse.Namespace) -> int:
    """Remove a position from the user personal book.

    Looks up by ticker, optionally narrowed by --option-symbol if multiple
    contracts on the same ticker exist. No cash credit, no realized_pnl
    bookkeeping (cash-agnostic). The exit fill and reason are appended to the
    real ledger so the close is auditable.
    """
    p = load_portfolio()
    user = p.get("user_positions", [])
    matches = [(i, x) for i, x in enumerate(user) if x["ticker"] == args.ticker.upper()]
    if args.option_symbol:
        matches = [(i, x) for i, x in matches if x.get("option_symbol") == args.option_symbol]
    if not matches:
        print(f"No user position in {args.ticker}"
              + (f" with symbol {args.option_symbol}" if args.option_symbol else ""),
              file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"[ERR] {len(matches)} matching positions in {args.ticker}; "
              f"pass --option-symbol to disambiguate.", file=sys.stderr)
        for _, x in matches:
            print(f"  - {x.get('option_symbol')}  qty={x.get('qty')}  entry={x.get('entry')}",
                  file=sys.stderr)
        return 1
    idx, pos = matches[0]
    fill = args.fill if args.fill is not None else pos["entry"]
    multiplier = 100 if pos.get("kind") == "option" else 1
    side_sign = 1 if pos.get("side", "LONG") == "LONG" else -1
    cost = float(pos["qty"]) * float(pos["entry"]) * multiplier
    proceeds = float(pos["qty"]) * float(fill) * multiplier
    pnl = (proceeds - cost) * side_sign
    user.pop(idx)
    save_portfolio(p)
    append_ledger({
        "kind": "CLOSE",
        "book": "user",
        "ticker": pos["ticker"],
        "option_symbol": pos.get("option_symbol"),
        "qty": pos["qty"],
        "exit": fill,
        "reason": args.reason,
        "pnl": round(pnl, 2),
        "ref_opened_at": pos.get("opened_at"),
    })
    print(f"Closed user position {pos['ticker']} {pos.get('option_symbol','')} @ {fill}. "
          f"PnL {fmt_usd(pnl)}.")
    return 0


def cmd_set_cooldown(args: argparse.Namespace) -> int:
    p = load_portfolio()
    p["cooldown_days_remaining"] = args.days
    save_portfolio(p)
    print(f"Cooldown set to {args.days} day(s).")
    return 0


def cmd_mtm_sync(_args: argparse.Namespace) -> int:
    p = load_portfolio()
    if _is_v3_cash_agnostic(p):
        print(_DEPRECATED_MSG, file=sys.stderr)
        return 1
    old = p["equity"]
    new = _recalc_equity(p)
    save_portfolio(p)
    print(f"Equity re-marked: {fmt_usd(old)} -> {fmt_usd(new)}  ({new - old:+.2f})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show")

    sc = sub.add_parser("set-cash")
    sc.add_argument("--amount", type=float, required=True)
    sc.add_argument("--note")

    rc = sub.add_parser("reconcile")
    rc.add_argument("--reported", type=float, required=True)
    rc.add_argument("--trust-reported", action="store_true",
                    help="Force cash to align with reported balance after investigation.")
    rc.add_argument("--note")

    apos = sub.add_parser("add-position")
    apos.add_argument("--ticker", required=True)
    apos.add_argument("--kind", default="stock", choices=["stock", "option", "crypto", "etf"])
    apos.add_argument("--side", default="LONG", choices=["LONG", "SHORT"])
    apos.add_argument("--qty", type=float, required=True)
    apos.add_argument("--entry", type=float, required=True)
    apos.add_argument("--stop", type=float)
    apos.add_argument("--target", type=float)
    apos.add_argument("--invalidation", help="Thesis-level invalidation condition.")
    apos.add_argument("--horizon-days", type=int, help="Time-stop in days from today.")
    apos.add_argument("--strategy", help="Strategy file name (no extension).")
    apos.add_argument("--option-symbol", help="OCC contract symbol for option positions.")
    apos.add_argument("--thesis", help="Short thesis line (also goes to ledger via INTENT).")

    cp = sub.add_parser("close-position")
    cp.add_argument("--ticker", required=True)
    cp.add_argument("--qty", type=float)
    cp.add_argument("--fill", type=float)
    cp.add_argument("--reason", default="discretionary",
                    choices=["target", "stop", "invalidation", "time", "discretionary", "expiry"])

    cd = sub.add_parser("set-cooldown")
    cd.add_argument("--days", type=int, required=True)

    sub.add_parser("mtm-sync")

    aup = sub.add_parser("add-user-position",
                         help="Record a position in the user personal book (no cash check).")
    aup.add_argument("--ticker", required=True)
    aup.add_argument("--kind", default="stock", choices=["stock", "option", "crypto", "etf"])
    aup.add_argument("--side", default="LONG", choices=["LONG", "SHORT"])
    aup.add_argument("--qty", type=float, required=True)
    aup.add_argument("--entry", type=float, required=True)
    aup.add_argument("--stop", type=float)
    aup.add_argument("--target", type=float)
    aup.add_argument("--invalidation")
    aup.add_argument("--horizon-days", type=int)
    aup.add_argument("--expires", help="Explicit expiration / horizon date (YYYY-MM-DD).")
    aup.add_argument("--opened-at", help="Override fill date (YYYY-MM-DD).")
    aup.add_argument("--strategy")
    aup.add_argument("--option-symbol")
    aup.add_argument("--thesis")

    cup = sub.add_parser("close-user-position",
                         help="Close a position from the user personal book.")
    cup.add_argument("--ticker", required=True)
    cup.add_argument("--option-symbol",
                     help="Required if multiple contracts on the same ticker.")
    cup.add_argument("--fill", type=float)
    cup.add_argument("--reason", default="discretionary",
                     choices=["target", "stop", "invalidation", "time", "discretionary", "expiry"])

    args = ap.parse_args()
    dispatch = {
        "show": cmd_show,
        "set-cash": cmd_set_cash,
        "reconcile": cmd_reconcile,
        "add-position": cmd_add_position,
        "close-position": cmd_close_position,
        "set-cooldown": cmd_set_cooldown,
        "mtm-sync": cmd_mtm_sync,
        "add-user-position": cmd_add_user_position,
        "close-user-position": cmd_close_user_position,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
