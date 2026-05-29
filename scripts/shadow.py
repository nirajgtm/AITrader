#!/usr/bin/env python3
"""Shadow book — hypothetical trades tracked as if real.

Why: even on no-trade days, mark what we *would* have done, let the market
resolve it, and compare judgment against reality. Shadow wins validate patterns;
shadow losses protect the real account.

Accounting:
  - For STOCK / ETF / CRYPTO: P&L = (exit - entry) × qty × side_sign.
  - For OPTIONS (long_call, long_put, debit_spread): P&L is computed in *option-premium*
    space — both `entry` and `exit` must be premiums per contract. The `--exit` to
    close is the option premium.
  - MTM proxy when no live option price available: derive a synthetic premium move
    from underlying delta-1 approximation: ΔP ≈ Δunderlying × |delta| (default 0.5
    for ATM long_call, -0.5 for long_put). Marked as STALE-PROXY in output.

Usage:
  shadow.py open --ticker XXX --vehicle stock|long_call|long_put|debit_spread \
                 --qty N --entry E --stop S --target T --thesis "..." [--premium P]
                 [--strategy NAME] [--horizon D]
  shadow.py close --id <shadow_id> --exit <exit_price> [--reason target|stop|invalidation|time]
  shadow.py mark --id <shadow_id> --premium <current premium>   # for options only
  shadow.py list [--open-only]
  shadow.py mtm
  shadow.py pnl
  shadow.py sweep [--dry-run] [--json]    # auto-close stop/target/time breaches
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date

from _common import (
    append_ledger,
    fmt_usd,
    load_shadow_positions,
    read_ledger,
    save_shadow_positions,
    today_str,
)
from _cache import cache_get, cache_put
from _terse import emit, step_result

OPTION_VEHICLES = {"long_call", "long_put", "debit_spread", "calendar"}
PRICE_TTL = 600


def _new_id() -> str:
    return "s_" + uuid.uuid4().hex[:6]


def _last_price(ticker: str) -> float | None:
    cache_key = f"price_{ticker.upper()}"
    cached = cache_get(cache_key, ttl_seconds=PRICE_TTL)
    if cached is not None:
        return float(cached)
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="1d")
        if h.empty:
            return None
        px = float(h["Close"].iloc[-1])
        cache_put(cache_key, px)
        return px
    except Exception:
        return None


def _approx_option_mtm(pos: dict) -> tuple[float, str]:
    """Return (mtm_$, source_note) for an option shadow position.

    Uses last_mark if recorded; else derives from underlying move via approx delta.
    """
    qty = float(pos["qty"])
    entry_premium = float(pos.get("premium", pos["entry"]))
    last_mark = pos.get("last_mark")
    if last_mark is not None:
        mtm = (float(last_mark) - entry_premium) * qty * 100
        return mtm, "manual mark"

    # Synthetic approximation
    underlying_last = _last_price(pos["ticker"]) or pos["entry"]
    underlying_entry = float(pos.get("underlying_entry", pos["entry"]))
    delta_assumed = 0.5 if pos["vehicle"] in ("long_call", "debit_spread") else -0.5
    if pos["vehicle"] == "long_put":
        delta_assumed = -0.5
    # synthetic premium move
    underlying_move = underlying_last - underlying_entry
    synth_premium_move = underlying_move * delta_assumed
    mtm = synth_premium_move * qty * 100
    return mtm, f"STALE-PROXY (delta~{delta_assumed:+.1f})"


def cmd_open(args: argparse.Namespace) -> int:
    sid = _new_id()
    pos: dict = {
        "id": sid,
        "opened_at": today_str(),
        "ticker": args.ticker.upper(),
        "vehicle": args.vehicle,
        "qty": args.qty,
        "entry": args.entry,
        "stop": args.stop,
        "target": args.target,
        "thesis": args.thesis,
        "strategy": args.strategy,
        "horizon": args.horizon,
    }
    if args.premium is not None:
        pos["premium"] = args.premium
    if args.underlying_entry is not None:
        pos["underlying_entry"] = args.underlying_entry
    elif args.vehicle in OPTION_VEHICLES:
        # Auto-capture underlying price at open for synthetic MTM later
        ul = _last_price(args.ticker)
        if ul is not None:
            pos["underlying_entry"] = ul

    state = load_shadow_positions()
    state["positions"].append(pos)
    state["updated_at"] = today_str()
    save_shadow_positions(state)

    append_ledger({
        "id": sid, "kind": "OPEN", "shadow": True, **{
            k: v for k, v in pos.items() if k != "id"
        }
    }, book="shadow")

    print(f"Opened shadow: {sid} {pos['ticker']} {pos['vehicle']} qty={pos['qty']} @ {pos['entry']}")
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    """Record a current option premium for an open shadow position."""
    state = load_shadow_positions()
    pos = next((p for p in state["positions"] if p["id"] == args.id), None)
    if pos is None:
        print(f"No open shadow with id {args.id}", file=sys.stderr)
        return 1
    if pos["vehicle"] not in OPTION_VEHICLES:
        print(f"[ERR] {pos['vehicle']} is not an option vehicle.", file=sys.stderr)
        return 1
    pos["last_mark"] = args.premium
    save_shadow_positions(state)
    print(f"{args.id} ({pos['ticker']} {pos['vehicle']}): last_mark = {args.premium}")
    return 0


def _close_position(state: dict, position_id: str, exit_price: float, reason: str) -> dict | None:
    """Remove position from state, write CLOSE ledger entry, return summary.

    Caller is responsible for save_shadow_positions(state). Returns None if id
    not found. Mutates state in place (pops from positions, bumps cum P&L).
    """
    idx = next((i for i, p in enumerate(state["positions"]) if p["id"] == position_id), None)
    if idx is None:
        return None
    pos = state["positions"].pop(idx)

    if pos["vehicle"] in OPTION_VEHICLES:
        entry_premium = float(pos.get("premium", pos["entry"]))
        pnl = (float(exit_price) - entry_premium) * float(pos["qty"]) * 100
        r_unit = entry_premium * float(pos["qty"]) * 100
    else:
        pnl = (float(exit_price) - float(pos["entry"])) * float(pos["qty"])
        r_unit = abs(float(pos["entry"]) - float(pos["stop"])) * float(pos["qty"])
    r_multiple = pnl / r_unit if r_unit else 0

    state["closed_pnl_cumulative"] = round(state.get("closed_pnl_cumulative", 0.0) + pnl, 2)
    state["updated_at"] = today_str()

    append_ledger({
        "id": _new_id(), "kind": "CLOSE", "shadow": True, "ref": position_id,
        "ticker": pos["ticker"], "vehicle": pos["vehicle"], "qty": pos["qty"],
        "exit": exit_price, "reason": reason, "pnl": round(pnl, 2),
        "r_multiple": round(r_multiple, 2),
    }, book="shadow")

    return {
        "id": position_id, "ticker": pos["ticker"], "vehicle": pos["vehicle"],
        "qty": pos["qty"], "entry": pos["entry"], "exit": exit_price,
        "reason": reason, "pnl": round(pnl, 2), "r_multiple": round(r_multiple, 2),
    }


def cmd_close(args: argparse.Namespace) -> int:
    state = load_shadow_positions()
    rec = _close_position(state, args.id, float(args.exit), args.reason)
    if rec is None:
        print(f"No open shadow with id {args.id}", file=sys.stderr)
        return 1
    save_shadow_positions(state)
    print(f"Closed shadow {rec['id']} {rec['ticker']} @ {rec['exit']}  "
          f"reason={rec['reason']}  PnL={fmt_usd(rec['pnl'])}  R={rec['r_multiple']:+.2f}")
    return 0


def _detect_side(pos: dict) -> str:
    """LONG if target >= stop; else SHORT. (Long puts/inverse-bets store target<stop.)"""
    return "LONG" if float(pos["target"]) >= float(pos["stop"]) else "SHORT"


def _detect_breach(pos: dict, last: float | None, today: date) -> tuple[str, float] | None:
    """Return (reason, exit_value) if a stop/target/time breach is met today.

    Priority: stop > target > time. Stop/target evaluated against `last`
    (underlying for option vehicles, last price for stocks). Time evaluated
    against horizon since opened_at. Exit_value is in the position's accounting
    space (premium for options, price for equities). Returns None if no breach.
    """
    side = _detect_side(pos)
    stop = float(pos["stop"])
    target = float(pos["target"])
    is_option = pos["vehicle"] in OPTION_VEHICLES

    # Stop / target evaluation needs a price
    if last is not None:
        if side == "LONG":
            stop_hit = last <= stop
            target_hit = last >= target
        else:  # SHORT
            stop_hit = last >= stop
            target_hit = last <= target

        if stop_hit:
            return ("stop", _synth_exit(pos, last, is_option, trigger=stop))
        if target_hit:
            return ("target", _synth_exit(pos, last, is_option, trigger=target))

    # Time stop (calendar days)
    horizon = int(pos.get("horizon") or 0)
    if horizon > 0:
        try:
            opened = date.fromisoformat(pos["opened_at"])
        except Exception:
            return None
        age_days = (today - opened).days
        if age_days >= horizon:
            time_last = last if last is not None else float(pos.get("entry", 0))
            return ("time", _synth_exit(pos, time_last, is_option, trigger=None))

    return None


def _synth_exit(pos: dict, underlying_last: float, is_option: bool,
                trigger: float | None) -> float:
    """Compute exit value for `_close_position`.

    Stocks/ETF/crypto: use trigger price for stop/target hits, underlying_last
    for time hits.

    Options: synthesize premium via entry_premium + (last - underlying_entry) * delta,
    floored at $0 (max loss = premium paid). Path-unaware — uses today's
    underlying_last regardless of trigger, since option premium at the moment
    the underlying touched the trigger is unknown without intraday data.
    """
    if not is_option:
        return float(trigger) if trigger is not None else float(underlying_last)

    entry_premium = float(pos.get("premium", pos["entry"]))
    underlying_entry = float(pos.get("underlying_entry", pos.get("entry", underlying_last)))
    vehicle = pos["vehicle"]
    # debit_spread can be bull-call or bear-put; resolve from side rather than vehicle name.
    side = _detect_side(pos)
    if vehicle == "long_call":
        delta = 0.5
    elif vehicle == "long_put":
        delta = -0.5
    elif vehicle == "debit_spread":
        delta = 0.5 if side == "LONG" else -0.5
    elif vehicle == "calendar":
        delta = 0.0  # vega-driven, can't proxy from underlying delta
    else:
        delta = 0.5 if side == "LONG" else -0.5
    synth_premium = entry_premium + (underlying_last - underlying_entry) * delta
    return max(0.0, round(synth_premium, 4))


def cmd_sweep(args: argparse.Namespace) -> int:
    """Auto-close shadows whose stop/target/time conditions have triggered.

    Backstop, not path-aware: evaluates against today's underlying last price.
    Run once per morning brief; cleanup of stale shadows that would otherwise
    accumulate and pollute shadow_pnl math.

    Limitations:
      - Path-unaware: a position whose horizon expired 3 days ago closes today
        at today's price, not the price on the day the time stop fired.
      - Debit spreads: synthetic exit premium uses delta-1 extrapolation but
        doesn't know the strike width, so it overshoots max profit when the
        underlying moves well past the short strike. Wins on debit spreads
        from sweep are directionally correct but magnitude-inflated.
    """
    state = load_shadow_positions()
    today = date.today()
    closed: list[dict] = []
    skipped: list[dict] = []

    # Iterate over a copy of ids since _close_position mutates state["positions"]
    ids = [p["id"] for p in state["positions"]]
    for pid in ids:
        pos = next((p for p in state["positions"] if p["id"] == pid), None)
        if pos is None:
            continue
        last = _last_price(pos["ticker"])
        breach = _detect_breach(pos, last, today)
        if breach is None:
            continue
        reason, exit_val = breach
        if args.dry_run:
            closed.append({
                "id": pid, "ticker": pos["ticker"], "vehicle": pos["vehicle"],
                "would_close": True, "reason": reason, "exit": exit_val,
                "underlying_last": last,
            })
            continue
        rec = _close_position(state, pid, exit_val, reason)
        if rec is None:
            skipped.append({"id": pid, "error": "close_failed"})
            continue
        rec["underlying_last"] = last
        closed.append(rec)

    if not args.dry_run and closed:
        save_shadow_positions(state)

    flags = [f"shadow_swept_{len(closed)}"] if closed else []
    headline_parts = [f"{c['ticker']}({c['reason']})" for c in closed[:6]]
    headline = (f"swept {len(closed)} shadows: " + ", ".join(headline_parts)
                if closed else "no shadows breached")
    if args.dry_run:
        headline = "DRY-RUN: " + headline

    if args.json:
        emit(step_result(
            "shadow_sweep", ok=True, headline=headline,
            data={"closed": closed, "open_remaining": len(state["positions"]),
                  "dry_run": args.dry_run},
            flags=flags,
            actions=[],
        ))
        return 0

    print(f"=== Shadow sweep ({today.isoformat()}) ===")
    print(headline)
    for c in closed:
        if args.dry_run:
            print(f"  WOULD CLOSE {c['id']} {c['ticker']:<6} reason={c['reason']:<10} "
                  f"exit~{c['exit']:.2f} (last={c.get('underlying_last')})")
        else:
            print(f"  CLOSED {c['id']} {c['ticker']:<6} reason={c['reason']:<10} "
                  f"exit={c['exit']:.2f} pnl={fmt_usd(c['pnl'])} R={c['r_multiple']:+.2f}")
    print(f"Open remaining: {len(state['positions'])}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    state = load_shadow_positions()
    positions = state["positions"]
    if args.open_only:
        print(f"Open shadow positions ({len(positions)}):")
        for p in positions:
            print(f"  {p['id']}  {p['ticker']:<6} {p['vehicle']:<14} qty={p['qty']} "
                  f"entry={p['entry']} stop={p['stop']} target={p['target']}  "
                  f"(opened {p['opened_at']})")
    else:
        for e in read_ledger("shadow"):
            print(json.dumps(e))
    return 0


def cmd_mtm(_args: argparse.Namespace) -> int:
    state = load_shadow_positions()
    positions = state["positions"]
    print(f"=== Shadow MTM ({today_str()}) ===")
    print(f"Closed cumulative P&L: {fmt_usd(state.get('closed_pnl_cumulative', 0.0))}")
    if not positions:
        print("No open shadow positions.")
        return 0
    print(f"{'id':<10}{'Ticker':<8}{'Vehicle':<14}{'Qty':>6}{'Entry':>10}"
          f"{'Last/Mark':>12}{'MTM$':>12}  Source")
    open_pnl = 0.0
    for p in positions:
        if p["vehicle"] in OPTION_VEHICLES:
            mtm, source = _approx_option_mtm(p)
            if p.get("last_mark") is not None:
                last_disp = float(p["last_mark"])
            else:
                last_disp = _last_price(p["ticker"]) or p["entry"]
        else:
            last = _last_price(p["ticker"])
            if last is None:
                last = p["entry"]
                source = "STALE entry"
            else:
                source = "yfinance"
            mtm = (last - float(p["entry"])) * float(p["qty"])
            last_disp = last
        open_pnl += mtm
        print(f"{p['id']:<10}{p['ticker']:<8}{p['vehicle']:<14}{p['qty']:>6}"
              f"{p['entry']:>10.2f}{last_disp:>12.2f}{mtm:>12.2f}  {source}")
    print(f"\nOpen PnL (approx):       {fmt_usd(open_pnl)}")
    print(f"Total P&L (closed+open): {fmt_usd(state.get('closed_pnl_cumulative', 0.0) + open_pnl)}")
    return 0


def cmd_pnl(args) -> int:
    state = load_shadow_positions()
    closed_shadow = state.get("closed_pnl_cumulative", 0.0)
    real = read_ledger("real")
    real_closed = sum(e.get("pnl", 0.0) for e in real if e.get("kind") == "CLOSE")
    diff = closed_shadow - real_closed
    flags = []
    if diff > 50:
        flags.append("shadow_outperforming")
    elif diff < -50:
        flags.append("shadow_underperforming")

    if getattr(args, "json", False):
        emit(step_result("shadow_pnl", ok=True,
                         headline=f"real={real_closed:.2f} shadow={closed_shadow:.2f} delta={diff:+.2f}",
                         data={"real_closed": real_closed, "shadow_closed": closed_shadow,
                               "open_count": len(state.get("positions", []))},
                         flags=flags))
        return 0

    print("=== Book comparison ===")
    print(f"Real   closed P&L: {fmt_usd(real_closed)}")
    print(f"Shadow closed P&L: {fmt_usd(closed_shadow)}")
    print(f"Delta (shadow - real): {fmt_usd(diff)}")
    if diff > 50:
        print("  -> shadow materially outperforming. Rules may be too restrictive.")
    elif diff < -50:
        print("  -> shadow materially underperforming real. Tighten watchlist.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    op = sub.add_parser("open")
    op.add_argument("--ticker", required=True)
    op.add_argument("--vehicle", required=True,
                    choices=["stock", "etf", "crypto", "long_call", "long_put",
                             "debit_spread", "calendar"])
    op.add_argument("--qty", type=float, required=True)
    op.add_argument("--entry", type=float, required=True)
    op.add_argument("--stop", type=float, required=True)
    op.add_argument("--target", type=float, required=True)
    op.add_argument("--thesis", required=True)
    op.add_argument("--premium", type=float, help="option premium per contract (if options)")
    op.add_argument("--underlying-entry", type=float,
                    help="underlying spot at open (auto-captured if omitted, for synthetic MTM)")
    op.add_argument("--strategy", default="discretionary")
    op.add_argument("--horizon", type=int, default=10)

    cp = sub.add_parser("close")
    cp.add_argument("--id", required=True)
    cp.add_argument("--exit", dest="exit", type=float, required=True)
    cp.add_argument("--reason", default="discretionary",
                    choices=["target", "stop", "invalidation", "time", "discretionary"])

    mk = sub.add_parser("mark")
    mk.add_argument("--id", required=True)
    mk.add_argument("--premium", type=float, required=True)

    lp = sub.add_parser("list")
    lp.add_argument("--open-only", action="store_true")

    sub.add_parser("mtm")
    pnl_p = sub.add_parser("pnl")
    pnl_p.add_argument("--json", action="store_true")

    sw = sub.add_parser("sweep")
    sw.add_argument("--json", action="store_true")
    sw.add_argument("--dry-run", action="store_true",
                    help="report which positions would close, do not write")

    args = ap.parse_args()
    return {
        "open": cmd_open, "close": cmd_close, "list": cmd_list,
        "mtm": cmd_mtm, "pnl": cmd_pnl, "mark": cmd_mark,
        "sweep": cmd_sweep,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
