#!/usr/bin/env python3
"""Open-position hypotheses store (state/hypotheses.json), keyed by ticker.

Every open position carries a hypothesis: entry, stop, target, mechanics, the
resting stop order id (whole-share) or 'software' (fractional), and the thesis
(expected trend, horizon, why). order_client persists this on a fill and removes
it on exit; run_autonomous reads it to manage positions each run. Keyed by ticker
so a single position reads cheaply.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

DIR = Path(__file__).resolve().parent
HYPOTHESES_PATH = DIR / "state" / "hypotheses.json"


def load() -> dict:
    if HYPOTHESES_PATH.exists():
        try:
            return json.loads(HYPOTHESES_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save(d: dict) -> None:
    HYPOTHESES_PATH.parent.mkdir(parents=True, exist_ok=True)
    HYPOTHESES_PATH.write_text(json.dumps(d, indent=2))


def get(ticker: str) -> dict:
    return load().get(ticker.upper(), {})


def save_hypothesis(ticker: str, *, entry: float, stop: float, target: float,
                    mechanics: str, stop_kind: str, qty: float,
                    stop_order_id: str | None = None, thesis: dict | None = None) -> dict:
    d = load()
    d[ticker.upper()] = {
        "entry": round(float(entry), 4),
        "entry_date": date.today().isoformat(),
        "stop": round(float(stop), 4),
        "target": round(float(target), 4),
        "mechanics": mechanics,
        "stop_kind": stop_kind,
        "qty": round(float(qty), 6),
        "stop_order_id": stop_order_id,
        "hypothesis": thesis or {},
    }
    _save(d)
    return d[ticker.upper()]


def remove(ticker: str) -> None:
    d = load()
    if ticker.upper() in d:
        del d[ticker.upper()]
        _save(d)
