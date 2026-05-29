#!/usr/bin/env python3
"""Settled-cash ledger for the cash account -- Good-Faith-Violation proof by design.

The single invariant that prevents GFV / free-riding: BUY ONLY WITH SETTLED CASH.
If every buy is funded by settled cash, the position is fully paid, so it can be
sold any time without a violation. Sell proceeds settle T+1, so they are NOT
spendable until they settle -- which is exactly what stops unsettled proceeds from
funding a new buy.

Ledger (state/settlement.json):
  { "settled_cash": float,
    "pending": [ {"amount": float, "settle_date": "YYYY-MM-DD", "note": str} ],
    "seeded": bool }

Flow each run: apply_matured(today) -> available_settled() -> buys consume settled
cash; sells add a pending T+1 settlement.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

DIR = Path(__file__).resolve().parent
STATE_DIR = DIR / "state"
LEDGER_PATH = STATE_DIR / "settlement.json"


def _load() -> dict:
    if LEDGER_PATH.exists():
        try:
            return json.loads(LEDGER_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"settled_cash": 0.0, "pending": [], "seeded": False}


def _save(led: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(led, indent=2))


def next_trading_day(d: date) -> date:
    """T+1 in trading days (NYSE calendar; falls back to next weekday)."""
    try:
        import pandas_market_calendars as mcal
        sched = mcal.get_calendar("NYSE").schedule(
            start_date=d + timedelta(days=1), end_date=d + timedelta(days=7))
        if not sched.empty:
            return sched.index[0].date()
    except Exception:
        pass
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def seed_if_needed(account_cash: float) -> dict:
    """First run only: seed settled cash from the funded account balance."""
    led = _load()
    if not led.get("seeded"):
        led = {"settled_cash": round(float(account_cash), 2), "pending": [], "seeded": True}
        _save(led)
    return led


def apply_matured(today: date | None = None) -> dict:
    """Move any pending settlements whose settle_date has arrived into settled cash."""
    today = today or date.today()
    led = _load()
    still_pending = []
    for p in led.get("pending", []):
        try:
            sd = date.fromisoformat(p["settle_date"])
        except Exception:
            continue
        if sd <= today:
            led["settled_cash"] = round(led.get("settled_cash", 0.0) + float(p["amount"]), 2)
        else:
            still_pending.append(p)
    led["pending"] = still_pending
    _save(led)
    return led


def available_settled(account_cash: float | None = None) -> float:
    """Spendable settled cash now. Capped at the live account cash (safety floor)
    so we never try to spend more than the account actually holds."""
    led = apply_matured()
    avail = float(led.get("settled_cash", 0.0))
    if account_cash is not None:
        avail = min(avail, float(account_cash))
    return round(max(avail, 0.0), 2)


def record_buy(amount: float) -> dict:
    led = apply_matured()
    led["settled_cash"] = round(led.get("settled_cash", 0.0) - float(amount), 2)
    _save(led)
    return led


def record_sell(proceeds: float, when: date | None = None) -> dict:
    """Sell proceeds settle T+1 -- added to pending, NOT immediately spendable."""
    when = when or date.today()
    led = apply_matured(when)
    led.setdefault("pending", []).append({
        "amount": round(float(proceeds), 2),
        "settle_date": next_trading_day(when).isoformat(),
        "note": f"sale proceeds {datetime.now().isoformat(timespec='seconds')}",
    })
    _save(led)
    return led
