#!/usr/bin/env python3
"""Per-ticker transaction history -- indexed by ticker for deterministic, cheap reads.

Each ticker's records live in their own file (state/history/<TICKER>.json), so a
caller (or the LLM) can read exactly one ticker's history without loading the
whole book into context. Records are appended on every fill.

Record shape (one per buy/sell fill):
  {ts, side, qty, price, dollars, order_id, realized_r, note, hypothesis}

CLI:
  history.py get AAPL      # just AAPL's records (deterministic)
  history.py list          # tickers with counts (index only, no record bodies)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DIR = Path(__file__).resolve().parent
HISTORY_DIR = DIR / "state" / "history"


def _path(ticker: str) -> Path:
    return HISTORY_DIR / f"{ticker.upper()}.json"


def get(ticker: str) -> list[dict]:
    """All transaction records for one ticker (empty list if none). Reads one file."""
    p = _path(ticker)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def record(ticker: str, side: str, qty: float, price: float, *, order_id: str = "",
           realized_r: float | None = None, note: str = "",
           hypothesis: dict | None = None) -> dict:
    """Append a fill record to this ticker's file. Returns the record."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "side": side,
        "qty": round(float(qty), 6),
        "price": round(float(price), 4),
        "dollars": round(float(qty) * float(price), 2),
        "order_id": order_id,
        "realized_r": realized_r,
        "note": note,
        "hypothesis": hypothesis or {},
    }
    recs = get(ticker)
    recs.append(rec)
    _path(ticker).write_text(json.dumps(recs, indent=2))
    return rec


def tickers() -> list[str]:
    if not HISTORY_DIR.exists():
        return []
    return sorted(p.stem for p in HISTORY_DIR.glob("*.json"))


def index() -> dict[str, int]:
    """Ticker -> record count. Cheap overview without loading record bodies."""
    return {tk: len(get(tk)) for tk in tickers()}


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-ticker transaction history.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get", help="Print one ticker's records.")
    g.add_argument("ticker")
    sub.add_parser("list", help="Ticker index with counts.")
    args = ap.parse_args()
    if args.cmd == "get":
        print(json.dumps(get(args.ticker), indent=2))
    elif args.cmd == "list":
        print(json.dumps(index(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
