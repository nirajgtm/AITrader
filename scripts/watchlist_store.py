#!/usr/bin/env python3
"""Single source of truth for reading and updating the watchlist.

Every script that needs the watchlist (the curated names /trader always analyzes)
goes through this module instead of parsing markdown or re-reading the JSON its
own way. The watchlist lives in ``state/watchlist.json`` (schema_version 2).

Public helpers:
  load_watchlist()           -> full dict (entries + archive + metadata)
  active_entries()           -> list of active entry dicts
  active_tickers()           -> list[str] of active tickers
  get_entry(ticker)          -> the entry dict for a ticker, or None
  has_ticker(ticker)         -> bool, is this ticker on the active watchlist
  add_stub(ticker, why)      -> append a minimal "needs a real thesis" entry and save
  is_macro(symbol)           -> bool, is this a macro level (10Y/VIX/DXY), not a stock

Macro levels are NOT stocks, so they are never required to be watchlist entries
(see is_macro). Everything else an alert points at must be a watchlist entry.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_JSON = ROOT / "state" / "watchlist.json"

# Macro levels that ride in their own lane: tracked by alerts but never required
# to be watchlist entries. Kept in sync with alerts.py TICKER_ALIAS.
MACRO_SYMBOLS = {"VIX", "^VIX", "DXY", "DX-Y.NYB", "TNX", "^TNX", "10Y", "TYX", "^TYX"}


def load_watchlist() -> dict:
    """Return the full watchlist dict, or an empty schema if the file is missing."""
    if not WATCHLIST_JSON.exists():
        return {"schema_version": 2, "updated_at": None, "entries": [], "archive": []}
    try:
        return json.loads(WATCHLIST_JSON.read_text())
    except (json.JSONDecodeError, OSError):
        return {"schema_version": 2, "updated_at": None, "entries": [], "archive": []}


def save_watchlist(wl: dict) -> None:
    """Write the watchlist back atomically (temp file + os.replace)."""
    wl["updated_at"] = date.today().isoformat()
    tmp = WATCHLIST_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl, indent=2))
    os.replace(tmp, WATCHLIST_JSON)


def active_entries() -> list[dict]:
    return load_watchlist().get("entries") or []


def active_tickers() -> list[str]:
    return [e["ticker"] for e in active_entries() if e.get("ticker")]


def get_entry(ticker: str) -> dict | None:
    tk = (ticker or "").upper()
    for e in active_entries():
        if (e.get("ticker") or "").upper() == tk:
            return e
    return None


def has_ticker(ticker: str) -> bool:
    return get_entry(ticker) is not None


def is_macro(symbol: str) -> bool:
    return (symbol or "").upper() in MACRO_SYMBOLS


def add_stub(ticker: str, why: str) -> dict:
    """Append a minimal watchlist entry for `ticker` and save.

    Used when an alert is created for a ticker that isn't on the watchlist yet,
    so the two never drift. The stub carries the alert's reason as a starter
    thesis and is flagged needs_validation=true with no thesis_checks, so it
    produces NO_LEVELS status (no false signals) until a real thesis is written.
    Returns the new entry. If the ticker already exists, returns the existing one.
    """
    existing = get_entry(ticker)
    if existing is not None:
        return existing
    wl = load_watchlist()
    entry = {
        "ticker": ticker.upper(),
        "angle": "(auto-stub from alert — needs a real thesis)",
        "direction": "LONG",
        "strategy_tag": "needs_validation",
        "added": date.today().isoformat(),
        "horizon": "TBD",
        "thesis": why or "(no hypothesis provided when the alert was created)",
        "invalidation": "TBD",
        "catalysts": [],
        "levels": {
            "entry_trigger": None, "entry_trigger_condition": None,
            "entry_zone_lo": None, "entry_zone_hi": None,
            "stop": None, "target": None, "target2": None, "fomo_ceiling": None,
        },
        "vehicle": None,
        "thesis_checks": [],
        "needs_validation": True,
        "notes": ["Auto-created from an alert. Replace with a real thesis and "
                  "thesis_checks before acting on a fired alert."],
    }
    wl.setdefault("entries", []).append(entry)
    save_watchlist(wl)
    return entry
