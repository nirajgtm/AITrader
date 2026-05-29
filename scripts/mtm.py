#!/usr/bin/env python3
"""Mark-to-market: walk open positions, fetch live-ish prices, compute PnL and equity.

Usage:
  mtm.py                              # mark all positions (yfinance fallback)
  mtm.py mark-option --ticker X --premium 1.45  # set last_mark for an option pos
"""
from __future__ import annotations

import argparse
import sys

import yfinance as yf

from _common import fmt_usd, load_portfolio, save_portfolio, today_str
from _cache import cache_get, cache_put
from _terse import emit, step_result

PRICE_TTL = 600  # 10 minutes


def _lc(p: dict) -> dict:
    """Read the legacy_challenge subobject (v3 schema) with empty-dict fallback."""
    return p.get("legacy_challenge") or {}


def _mark_position(pos: dict) -> tuple[float, float, float, str]:
    """Return (mv, cost, last_disp, source) for one position. Side-sign-naive."""
    is_option = pos.get("kind") == "option"
    if is_option:
        mark = pos.get("last_mark")
        if mark is None:
            mark = pos["entry"]
            source = "[STALE: entry]"
        else:
            source = "manual mark"
        mv = float(pos["qty"]) * float(mark) * 100
        cost = float(pos["qty"]) * float(pos["entry"]) * 100
        return mv, cost, float(mark), source
    last = last_price(pos["ticker"])
    if last is None:
        last = pos["entry"]
        source = "[STALE: entry]"
    else:
        source = "yfinance"
    mv = float(pos["qty"]) * float(last)
    cost = float(pos["qty"]) * float(pos["entry"])
    return mv, cost, float(last), source


def _pos_to_item(pos: dict) -> dict:
    mv, cost, last, _src = _mark_position(pos)
    side_sign = 1 if pos.get("side", "LONG") == "LONG" else -1
    pnl = (mv - cost) * side_sign
    return {
        "tk": pos["ticker"], "kind": pos.get("kind", "stock"),
        "qty": pos["qty"], "entry": pos["entry"], "last": round(last, 2),
        "mv": round(mv, 2), "cost": round(cost, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round((pnl / cost * 100) if cost else 0, 2),
        "stop": pos.get("stop"), "target": pos.get("target"),
        "horizon_expires_at": pos.get("horizon_expires_at"),
        "option_symbol": pos.get("option_symbol"),
        "strategy": pos.get("strategy"),
        "thesis": pos.get("thesis"),
    }


def last_price(ticker: str) -> float | None:
    cache_key = f"price_{ticker.upper()}"
    cached = cache_get(cache_key, ttl_seconds=PRICE_TTL)
    if cached is not None:
        return float(cached)
    try:
        h = yf.Ticker(ticker).history(period="1d")
        if h.empty:
            return None
        px = float(h["Close"].iloc[-1])
        cache_put(cache_key, px)
        return px
    except Exception as e:
        print(f"[WARN] {ticker}: {e}", file=sys.stderr)
        return None


def cmd_mark_option(args: argparse.Namespace) -> int:
    """Manually set the last_mark on an option position (premium per contract).

    Searches both `positions` (challenge book) and `user_positions` (personal
    book). If --option-symbol is given it disambiguates between multiple
    contracts on the same ticker.
    """
    p = load_portfolio(getattr(args, "portfolio_id", "primary"))
    haystack = list(p.get("positions", [])) + list(p.get("user_positions", []))
    matches = [x for x in haystack
               if x["ticker"] == args.ticker.upper() and x.get("kind") == "option"]
    if getattr(args, "option_symbol", None):
        matches = [x for x in matches if x.get("option_symbol") == args.option_symbol]
    if not matches:
        print(f"No open option position in {args.ticker}"
              + (f" with symbol {args.option_symbol}" if getattr(args, "option_symbol", None) else ""),
              file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"[ERR] {len(matches)} matching option positions in {args.ticker}; "
              f"pass --option-symbol to disambiguate.", file=sys.stderr)
        for x in matches:
            print(f"  - {x.get('option_symbol')}  qty={x.get('qty')}  entry={x.get('entry')}",
                  file=sys.stderr)
        return 1
    pos = matches[0]
    old = pos.get("last_mark", pos["entry"])
    pos["last_mark"] = args.premium
    save_portfolio(p, getattr(args, "portfolio_id", "primary"))
    print(f"{args.ticker} {pos.get('option_symbol','')}: last_mark {old} -> {args.premium}")
    return cmd_show(args)


def _print_book(label: str, positions: list) -> float:
    """Print one book's positions table; return total cost basis."""
    if not positions:
        print(f"\n--- {label} ---")
        print("  (none)")
        return 0.0
    print(f"\n--- {label} ({len(positions)} position{'s' if len(positions)!=1 else ''}) ---")
    print(f"{'Ticker':<8}{'Kind':<7}{'Side':<6}{'Qty':>6}{'Entry':>10}{'Last':>10}"
          f"{'MTM$':>12}{'PnL$':>12}{'PnL%':>8} Source")
    total_cost = 0.0
    for pos in positions:
        mv, cost, last, source = _mark_position(pos)
        total_cost += cost
        side_sign = 1 if pos.get("side", "LONG") == "LONG" else -1
        pnl = (mv - cost) * side_sign
        pnl_pct = pnl / cost * 100 if cost else 0
        sym = pos.get("option_symbol") or ""
        print(f"{pos['ticker']:<8}{pos.get('kind','stock'):<7}{pos.get('side','LONG'):<6}"
              f"{pos['qty']:>6}{pos['entry']:>10.2f}{last:>10.2f}{mv:>12.2f}{pnl:>12.2f}"
              f"{pnl_pct:>7.2f}% {source}{(' '+sym) if sym else ''}")
    return total_cost


def cmd_show(args: argparse.Namespace) -> int:
    p = load_portfolio(getattr(args, "portfolio_id", "primary"))
    challenge_pos = p.get("positions", [])
    user_pos = p.get("user_positions", [])
    lc = _lc(p)
    print(f"=== Mark-to-market  ({today_str()}) ===")
    print(f"Schema version: {p.get('version', 'unknown')}")

    if lc:
        print(f"\nLegacy challenge book (deprecated {lc.get('deprecated_at','')}):")
        print(f"  Cash:    {fmt_usd(lc.get('cash', 0))}")
        print(f"  Equity:  {fmt_usd(lc.get('equity', 0))}  "
              f"(realized: {fmt_usd(lc.get('realized_pnl', 0))})")

    _print_book("Challenge positions[]", challenge_pos)
    user_cost = _print_book("User personal book user_positions[]", user_pos)

    if user_pos:
        print(f"\nUser book cost basis (sum): {fmt_usd(user_cost)}")
        print("(no equity / drawdown calc — cash-agnostic per CONSTITUTION v2.0)")

    save_portfolio(p, getattr(args, "portfolio_id", "primary"))
    return 0


def cmd_json(args) -> int:
    p = load_portfolio(getattr(args, "portfolio_id", "primary"))
    challenge_pos = p.get("positions", [])
    user_pos = p.get("user_positions", [])
    lc = _lc(p)

    challenge_items = [_pos_to_item(pos) for pos in challenge_pos]
    user_items = [_pos_to_item(pos) for pos in user_pos]
    user_cost_total = round(sum(it["cost"] for it in user_items), 2)
    user_mv_total = round(sum(it["mv"] for it in user_items), 2)
    user_pnl_total = round(sum(it["pnl"] for it in user_items), 2)

    flags = []
    headline_bits = [f"user_book_count={len(user_items)}"]
    if user_items:
        headline_bits.append(
            f"user_cost={user_cost_total} mv={user_mv_total} pnl={user_pnl_total:+.2f}"
        )

    legacy_payload = None
    if lc:
        legacy_cash = lc.get("cash", 0)
        legacy_equity = lc.get("equity", legacy_cash)
        legacy_starting = lc.get("starting_equity", legacy_equity)
        legacy_hw = lc.get("equity_high_water", legacy_starting)
        legacy_payload = {
            "deprecated_at": lc.get("deprecated_at"),
            "cash": legacy_cash,
            "equity": legacy_equity,
            "starting_equity": legacy_starting,
            "high_water": legacy_hw,
            "realized_pnl": lc.get("realized_pnl", 0),
            "positions": challenge_items,
        }

    headline = "; ".join(headline_bits) if headline_bits else "empty book"

    emit(step_result(
        "mtm", ok=True, headline=headline,
        data={
            "schema_version": p.get("version"),
            "book_mode": "cash_agnostic" if "user_positions" in p else "legacy_challenge",
            "user_positions": user_items,
            "user_cost_basis_total": user_cost_total,
            "user_mv_total": user_mv_total,
            "user_pnl_total": user_pnl_total,
            "challenge_positions": challenge_items,
            "legacy_challenge": legacy_payload,
        },
        flags=flags,
    ))
    save_portfolio(p, getattr(args, "portfolio_id", "primary"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--portfolio-id", default="primary",
                    help="Portfolio id. 'primary' (default) reads "
                         "state/portfolio.json. Other ids read "
                         "state/portfolios/<id>.json.")
    sub = ap.add_subparsers(dest="cmd")
    # Default to show
    show_p = sub.add_parser("show")
    show_p.add_argument("--json", action="store_true")

    mo = sub.add_parser("mark-option")
    mo.add_argument("--ticker", required=True)
    mo.add_argument("--premium", type=float, required=True,
                    help="current option premium per contract")
    mo.add_argument("--option-symbol",
                    help="OCC symbol; required if multiple option positions on the ticker.")

    args = ap.parse_args()
    if args.cmd is None or args.cmd == "show":
        if getattr(args, "json", False):
            return cmd_json(args)
        return cmd_show(args)
    if args.cmd == "mark-option":
        return cmd_mark_option(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
