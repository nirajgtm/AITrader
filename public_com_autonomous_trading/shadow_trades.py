#!/usr/bin/env python3
"""Shadow (paper) trade book for conviction ideas (state/shadow_trades.json).

The mind opens and closes SHADOW trades -- paper, no real money -- for the
conviction stocks, to track how its unconstrained convictions actually perform
over time. This book is deliberately SEPARATE from the real position book: the
real book trades under the codified risk guards and sizing, while a conviction
may be larger, earlier, or differently structured than what the guards allow.
Shadow trades let the mind keep an honest scoreboard of its raw read.

Each trade tracks the UNDERLYING price move in the idea's direction -- a clean
directional proxy. Option leverage and premium are NOT modeled: a "call" idea is
scored as a long in the underlying, a "put credit spread" as a long, a "put" or
"call credit spread" as a short. Profit is a fixed paper notional times the
directional percent move from entry, so two ideas with the same notional are
compared purely on how well the directional call worked.
"""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import date
from pathlib import Path

DIR = Path(__file__).resolve().parent
PATH = DIR / "state" / "shadow_trades.json"
DEFAULT_NOTIONAL = 1000.0

# Idea types and the directional stance they imply on the underlying.
BULLISH_TYPES = {"long", "call", "debit_spread", "put_credit_spread"}
BEARISH_TYPES = {"put", "call_credit_spread"}


def _load() -> dict:
    if PATH.exists():
        try:
            d = json.loads(PATH.read_text())
            if isinstance(d, dict) and isinstance(d.get("trades"), list):
                return d
        except (json.JSONDecodeError, OSError):
            pass
    return {"trades": []}


def _save(d: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(d, indent=2))


def _dir_from_type(t: str) -> str:
    """Map an idea type to a directional stance on the underlying."""
    t = (t or "").lower()
    if t in BEARISH_TYPES:
        return "short"
    if t in BULLISH_TYPES:
        return "long"
    return "long"


def _pnl(trade: dict, price: float) -> float:
    """Paper dollars for a directional underlying move from entry to price."""
    entry = float(trade.get("entry", 0) or 0)
    if entry <= 0:
        return 0.0
    notional = float(trade.get("notional", DEFAULT_NOTIONAL))
    price = float(price)
    if trade.get("direction") == "short":
        return notional * (1 - price / entry)
    return notional * (price / entry - 1)


def open_trade(ticker, entry, *, type="long", direction=None, target=None,
               stop=None, thesis="", notional=DEFAULT_NOTIONAL) -> str:
    """Open a shadow trade and return its id.

    De-dupes: if an OPEN trade for the same ticker+type already exists, returns
    that trade's id without adding a new one.
    """
    ticker = str(ticker).upper()
    type = (type or "long").lower()
    d = _load()
    for t in d["trades"]:
        if (t.get("status") == "open" and t.get("ticker") == ticker
                and t.get("type") == type):
            return t["id"]
    entry = float(entry)
    tid = uuid.uuid4().hex[:8]
    d["trades"].append({
        "id": tid,
        "ticker": ticker,
        "type": type,
        "direction": direction or _dir_from_type(type),
        "notional": float(notional),
        "opened": date.today().isoformat(),
        "entry": entry,
        "target": target,
        "stop": stop,
        "thesis": thesis,
        "status": "open",
        "last_price": entry,
        "unrealized_pnl": 0.0,
    })
    _save(d)
    return tid


def close_trade(trade_id, exit_price, reason="") -> bool:
    """Close an open shadow trade by id. Returns True if a trade was closed."""
    d = _load()
    for t in d["trades"]:
        if t.get("id") == trade_id and t.get("status") == "open":
            exit_price = float(exit_price)
            t["status"] = "closed"
            t["closed"] = date.today().isoformat()
            t["exit"] = exit_price
            t["realized_pnl"] = round(_pnl(t, exit_price), 2)
            t["close_reason"] = reason
            _save(d)
            return True
    return False


def mark(price_map: dict) -> int:
    """Mark open trades to current prices. Returns how many were marked."""
    d = _load()
    n = 0
    for t in d["trades"]:
        if t.get("status") != "open":
            continue
        price = price_map.get(t.get("ticker"))
        if price is None:
            continue
        price = float(price)
        t["last_price"] = price
        t["unrealized_pnl"] = round(_pnl(t, price), 2)
        n += 1
    if n:
        _save(d)
    return n


def _windows_for(closed_trades: list) -> dict:
    """Sum realized_pnl across time windows ending today."""
    today = date.today()
    w = {"day": 0.0, "week": 0.0, "month": 0.0, "year": 0.0, "all": 0.0}
    for t in closed_trades:
        pnl = float(t.get("realized_pnl", 0) or 0)
        w["all"] += pnl
        closed = t.get("closed")
        if not closed:
            continue
        try:
            days = (today - date.fromisoformat(closed)).days
        except (ValueError, TypeError):
            continue
        if days < 0:
            continue
        if days == 0:
            w["day"] += pnl
        if days <= 7:
            w["week"] += pnl
        if days <= 30:
            w["month"] += pnl
        if days <= 365:
            w["year"] += pnl
    return {k: round(v, 2) for k, v in w.items()}


def summary() -> dict:
    """Open/closed lists, per-stock windows, and totals. Dollars rounded to 2dp."""
    d = _load()
    open_trades = [t for t in d["trades"] if t.get("status") == "open"]
    closed_trades = [t for t in d["trades"] if t.get("status") == "closed"]
    closed_sorted = sorted(closed_trades, key=lambda t: t.get("closed", ""),
                           reverse=True)

    tickers = sorted({t.get("ticker") for t in d["trades"] if t.get("ticker")})
    per_stock = {}
    for tk in tickers:
        tk_closed = [t for t in closed_trades if t.get("ticker") == tk]
        tk_open = [t for t in open_trades if t.get("ticker") == tk]
        windows = _windows_for(tk_closed)
        unrealized = round(sum(float(t.get("unrealized_pnl", 0) or 0)
                               for t in tk_open), 2)
        per_stock[tk] = {
            "realized_all": windows["all"],
            "unrealized": unrealized,
            "n_open": len(tk_open),
            "n_closed": len(tk_closed),
            "windows": windows,
        }

    totals = {
        "windows": _windows_for(closed_trades),
        "open_unrealized": round(sum(float(t.get("unrealized_pnl", 0) or 0)
                                     for t in open_trades), 2),
    }
    return {
        "open": open_trades,
        "closed": closed_sorted,
        "per_stock": per_stock,
        "totals": totals,
    }


def _fmt_trade(t: dict) -> str:
    parts = [
        t.get("id", "?"),
        t.get("ticker", "?"),
        t.get("type", "?"),
        t.get("direction", "?"),
        f"entry={t.get('entry')}",
        f"notional={t.get('notional')}",
    ]
    if t.get("status") == "open":
        parts.append(f"last={t.get('last_price')}")
        parts.append(f"unrl={t.get('unrealized_pnl')}")
        parts.append(f"opened={t.get('opened')}")
    else:
        parts.append(f"exit={t.get('exit')}")
        parts.append(f"real={t.get('realized_pnl')}")
        parts.append(f"closed={t.get('closed')}")
        if t.get("close_reason"):
            parts.append(f"reason={t.get('close_reason')}")
    return "  " + " | ".join(str(p) for p in parts)


def main() -> None:
    ap = argparse.ArgumentParser(description="Shadow (paper) trade book.")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list", help="pretty-print open and closed shadow trades")

    p_sum = sub.add_parser("summary", help="per-stock and total paper P&L")
    p_sum.add_argument("--json", action="store_true", help="emit raw JSON")

    p_open = sub.add_parser("open", help="open a shadow trade")
    p_open.add_argument("ticker")
    p_open.add_argument("entry", type=float)
    p_open.add_argument("--type", default="long")
    p_open.add_argument("--target", type=float, default=None)
    p_open.add_argument("--stop", type=float, default=None)
    p_open.add_argument("--thesis", default="")
    p_open.add_argument("--notional", type=float, default=DEFAULT_NOTIONAL)

    p_close = sub.add_parser("close", help="close a shadow trade by id")
    p_close.add_argument("id")
    p_close.add_argument("exit", type=float)
    p_close.add_argument("--reason", default="")

    args = ap.parse_args()

    if args.cmd == "open":
        tid = open_trade(args.ticker, args.entry, type=args.type,
                         target=args.target, stop=args.stop, thesis=args.thesis,
                         notional=args.notional)
        print(f"opened {tid} {args.ticker.upper()} {args.type} @ {args.entry}")
    elif args.cmd == "close":
        ok = close_trade(args.id, args.exit, args.reason)
        print(f"closed {args.id} @ {args.exit}" if ok
              else f"no open trade with id {args.id}")
    elif args.cmd == "summary":
        s = summary()
        if args.json:
            print(json.dumps(s, indent=2))
        else:
            print("Totals (realized by window):")
            for k, v in s["totals"]["windows"].items():
                print(f"  {k:>5}: {v:+.2f}")
            print(f"  open unrealized: {s['totals']['open_unrealized']:+.2f}")
            print("Per stock:")
            for tk, st in s["per_stock"].items():
                print(f"  {tk}: realized_all={st['realized_all']:+.2f} "
                      f"unrealized={st['unrealized']:+.2f} "
                      f"open={st['n_open']} closed={st['n_closed']}")
    else:  # list (default)
        s = summary()
        print(f"Open shadow trades ({len(s['open'])}):")
        for t in s["open"]:
            print(_fmt_trade(t))
        print(f"Closed shadow trades ({len(s['closed'])}):")
        for t in s["closed"]:
            print(_fmt_trade(t))


if __name__ == "__main__":
    main()
