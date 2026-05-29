"""Standard schema for JSON outputs across all scripts.

Goal: Claude reads structured data, not prose. Every script's --json mode emits
a dict in this shape so the runbook digest can compose them mechanically.

Schema:
{
  "step": "regime",
  "ok": true,
  "ts": "2026-04-25T19:00:00Z",
  "headline": "BULL extended; VIX 18.71",          # one-line for the digest
  "data": {...},                                    # compressed numeric facts
  "flags": ["fomo_above_2atr", "rsi_extreme_qqq"],  # boolean conditions worth knowing
  "actions": [{"kind":"alert","msg":"..."}],        # deterministic recommendations
  "errors": []                                      # non-fatal issues / data gaps
}

Flags vocabulary (extend as needed):
  - regime_shift, fomo_above_2atr, rsi_extreme_{spy,qqq,iwm}
  - vix_5d_rising, vix_term_backwardation, breadth_narrow
  - earnings_in_horizon, fomc_in_horizon
  - stop_breach_intraday, target_hit, horizon_expiring
  - cluster_signal_<ticker>

Actions vocabulary:
  - alert      — surface to user; not actionable
  - exit_review — open position needs review
  - candidate  — new trade candidate surfaced
  - data_gap   — couldn't fetch; mention but don't block
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def step_result(
    step: str,
    *,
    ok: bool = True,
    headline: str = "",
    data: Optional[dict] = None,
    flags: Optional[list[str]] = None,
    actions: Optional[list[dict]] = None,
    errors: Optional[list[str]] = None,
) -> dict:
    return {
        "step": step,
        "ok": ok,
        "ts": _now_iso(),
        "headline": headline,
        "data": data or {},
        "flags": flags or [],
        "actions": actions or [],
        "errors": errors or [],
    }


def emit(result: dict) -> None:
    """Write JSON to stdout (compact, one line)."""
    json.dump(result, sys.stdout, separators=(",", ":"), default=str)
    sys.stdout.write("\n")


def emit_pretty(result: dict) -> None:
    """Write JSON to stdout (pretty, for human inspection)."""
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


# Helpers for action / flag construction so callers don't typo strings

def alert(msg: str, **kwargs) -> dict:
    return {"kind": "alert", "msg": msg, **kwargs}


def exit_review(ticker: str, reason: str, **kwargs) -> dict:
    return {"kind": "exit_review", "ticker": ticker, "reason": reason, **kwargs}


def candidate(ticker: str, thesis: str, **kwargs) -> dict:
    return {"kind": "candidate", "ticker": ticker, "thesis": thesis, **kwargs}


def data_gap(source: str, reason: str = "") -> dict:
    return {"kind": "data_gap", "source": source, "reason": reason}
