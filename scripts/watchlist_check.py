#!/usr/bin/env python3
"""Deterministic watchlist trigger check with thesis validation.

Reads state/watchlist.json (single source of truth). For each entry:
  1. Fetches latest price + technicals (close, ma20, ma50, ma200, rsi14).
  2. Checks if the entry trigger fires.
  3. If triggered: runs all thesis_checks to validate the original thesis still
     holds. A thesis_check can WARN (proceed with caution) or INVALIDATE (abort).

Status values:
  NOT_TRIGGERED      -- price has not reached the entry trigger yet
  NEAR               -- within 1% of the entry trigger
  TRIGGERED_VALID    -- trigger fired, all thesis_checks passed
  TRIGGERED_WARN     -- trigger fired, at least one check warned
  TRIGGERED_INVALID  -- trigger fired but an invalidate check failed; do not enter
  STOP_HIT           -- price crossed the stop level
  AVOID_WATCH        -- AVOID direction entry, no trigger yet
  TRIGGERED_REEVAL   -- AVOID entry whose re-evaluation trigger fired
  NO_LEVELS          -- no entry_trigger defined
  NO_DATA            -- price fetch failed

Output shape is backward-compatible with brief.py: items[] always has
(ticker, angle, entry_trigger, stop, target, last, dist_pct, status, direction).
Adds thesis_validation field when checks were run.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PY = SCRIPTS / ".venv" / "bin" / "python3"
WATCHLIST_JSON = ROOT / "state" / "watchlist.json"

sys.path.insert(0, str(SCRIPTS))
from _cache import cache_get, cache_put  # noqa: E402
from _terse import emit, step_result    # noqa: E402


# ---------------------------------------------------------------------------
# Price data fetcher (cached per session)
# ---------------------------------------------------------------------------

_price_cache: dict[str, dict | None] = {}


def _fetch_price(ticker: str) -> dict | None:
    key = ticker.upper()
    if key in _price_cache:
        return _price_cache[key]

    cache_key = f"wl_price_{key}"
    hit = cache_get(cache_key, ttl_seconds=300)
    if hit is not None:
        try:
            parsed = json.loads(hit) if isinstance(hit, str) else hit
            _price_cache[key] = parsed
            return parsed
        except Exception:
            pass

    try:
        r = subprocess.run(
            [str(PY), str(SCRIPTS / "price.py"), ticker, "--json"],
            capture_output=True, text=True, timeout=25,
        )
        out = (r.stdout or "").strip()
        last_line = next((ln for ln in reversed(out.splitlines()) if ln.startswith("{")), None)
        if not last_line:
            _price_cache[key] = None
            return None
        d = json.loads(last_line)
        if not d.get("ok"):
            _price_cache[key] = None
            return None
        data = d.get("data") or {}
        cache_put(cache_key, json.dumps(data))
        _price_cache[key] = data
        return data
    except Exception:
        _price_cache[key] = None
        return None


# ---------------------------------------------------------------------------
# Thesis check runner
# ---------------------------------------------------------------------------

def _run_check(check: dict) -> dict:
    cid = check.get("id", "unknown")
    label = check.get("label", cid)
    check_type = check.get("check", "")
    ticker = check.get("ticker", "")
    threshold = check.get("threshold")
    on_fail = check.get("on_fail", "warn")

    pd = _fetch_price(ticker)
    if pd is None:
        return {
            "id": cid, "label": label, "result": "NO_DATA",
            "value": None, "threshold": threshold, "on_fail": on_fail,
            "detail": f"price fetch failed for {ticker}",
        }

    close = pd.get("close")
    rsi = pd.get("rsi14")
    ma20 = pd.get("ma20")
    ma50 = pd.get("ma50")
    ma200 = pd.get("ma200")

    result = "PASS"
    value = None
    detail = ""

    if check_type == "price_below":
        value = close
        if value is None or threshold is None:
            result = "NO_DATA"
        elif value < threshold:
            detail = f"{ticker} {value:.2f} < {threshold}"
        else:
            result = "FAIL"
            detail = f"{ticker} {value:.2f} >= {threshold} (expected below)"

    elif check_type == "price_above":
        value = close
        if value is None or threshold is None:
            result = "NO_DATA"
        elif value > threshold:
            detail = f"{ticker} {value:.2f} > {threshold}"
        else:
            result = "FAIL"
            detail = f"{ticker} {value:.2f} <= {threshold} (expected above)"

    elif check_type == "above_ma20":
        value = close
        ref = ma20
        if value is None or ref is None:
            result = "NO_DATA"
        elif value > ref:
            detail = f"{ticker} {value:.2f} above MA20 {ref:.2f}"
        else:
            result = "FAIL"
            detail = f"{ticker} {value:.2f} below MA20 {ref:.2f}"

    elif check_type == "above_ma50":
        value = close
        ref = ma50
        if value is None or ref is None:
            result = "NO_DATA"
        elif value > ref:
            detail = f"{ticker} {value:.2f} above MA50 {ref:.2f}"
        else:
            result = "FAIL"
            detail = f"{ticker} {value:.2f} below MA50 {ref:.2f} -- bull structure broken"

    elif check_type == "above_ma200":
        value = close
        ref = ma200
        if value is None or ref is None:
            result = "NO_DATA"
        elif value > ref:
            detail = f"{ticker} {value:.2f} above MA200 {ref:.2f}"
        else:
            result = "FAIL"
            detail = f"{ticker} {value:.2f} below MA200 {ref:.2f} -- long-term structure broken"

    elif check_type == "rsi_below":
        value = rsi
        if value is None or threshold is None:
            result = "NO_DATA"
        elif value < threshold:
            detail = f"RSI {value:.1f} < {threshold}"
        else:
            result = "FAIL"
            detail = f"RSI {value:.1f} >= {threshold} (too extended at entry)"

    elif check_type == "rsi_above":
        value = rsi
        if value is None or threshold is None:
            result = "NO_DATA"
        elif value > threshold:
            detail = f"RSI {value:.1f} > {threshold}"
        else:
            result = "FAIL"
            detail = f"RSI {value:.1f} <= {threshold} (momentum not confirmed)"

    else:
        result = "UNKNOWN_CHECK"
        detail = f"unrecognised check type: {check_type}"

    return {
        "id": cid, "label": label, "result": result,
        "value": round(value, 2) if isinstance(value, (int, float)) else value,
        "threshold": threshold, "on_fail": on_fail, "detail": detail,
    }


def _run_thesis_validation(entry: dict) -> dict:
    checks = entry.get("thesis_checks") or []
    if not checks:
        return {"ran": False, "overall": "NO_CHECKS", "checks": []}

    results = [_run_check(c) for c in checks]

    has_invalidate = any(r["result"] == "FAIL" and r["on_fail"] == "invalidate" for r in results)
    has_warn = any(r["result"] == "FAIL" for r in results)
    all_no_data = all(r["result"] == "NO_DATA" for r in results)

    if has_invalidate:
        overall = "INVALIDATED"
    elif has_warn:
        overall = "WARNINGS"
    elif all_no_data:
        overall = "PARTIAL"
    else:
        overall = "VALID"

    return {"ran": True, "overall": overall, "checks": results}


# ---------------------------------------------------------------------------
# Trigger + status logic
# ---------------------------------------------------------------------------

def _check_trigger(entry: dict, last: float) -> bool:
    levels = entry.get("levels") or {}
    trigger = levels.get("entry_trigger")
    cond = levels.get("entry_trigger_condition", "lte")
    if trigger is None:
        return False
    return last <= trigger if cond == "lte" else last >= trigger


def _dist_pct(entry: dict, last: float) -> float | None:
    trigger = (entry.get("levels") or {}).get("entry_trigger")
    if not trigger:
        return None
    return round((last / trigger - 1) * 100, 2)


def _compute_status(entry: dict, last: float | None, thesis_val: dict) -> str:
    direction = entry.get("direction", "LONG")
    levels = entry.get("levels") or {}
    trigger = levels.get("entry_trigger")
    stop = levels.get("stop")

    if direction == "AVOID":
        if trigger is None or last is None:
            return "AVOID_WATCH"
        return "TRIGGERED_REEVAL" if _check_trigger(entry, last) else "AVOID_WATCH"

    if last is None:
        return "NO_DATA"
    if trigger is None:
        return "NO_LEVELS"

    if stop is not None:
        if direction == "LONG" and last < stop:
            return "STOP_HIT"
        if direction == "SHORT" and last > stop:
            return "STOP_HIT"

    if _check_trigger(entry, last):
        overall = thesis_val.get("overall", "NO_CHECKS")
        if overall == "INVALIDATED":
            return "TRIGGERED_INVALID"
        if overall == "WARNINGS":
            return "TRIGGERED_WARN"
        return "TRIGGERED_VALID"

    dp = _dist_pct(entry, last)
    if dp is not None and abs(dp) <= 1.0:
        return "NEAR"
    return "NOT_TRIGGERED"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not WATCHLIST_JSON.exists():
        r = step_result("watchlist_check", ok=False,
                        headline="state/watchlist.json not found",
                        errors=[str(WATCHLIST_JSON)])
        emit(r) if args.json else print(json.dumps(r, indent=2))
        return 1

    wl = json.loads(WATCHLIST_JSON.read_text())
    entries = wl.get("entries") or []

    out_items: list[dict] = []
    flags: list[str] = []
    triggered: list[str] = []
    near: list[str] = []

    for entry in entries:
        ticker = entry.get("ticker", "")
        levels = entry.get("levels") or {}

        pd = _fetch_price(ticker)
        last = pd.get("close") if pd else None
        dp = _dist_pct(entry, last) if last is not None else None

        thesis_val = _run_thesis_validation(entry)
        status = _compute_status(entry, last, thesis_val)

        item: dict = {
            "ticker": ticker,
            "angle": entry.get("angle", ""),
            "direction": entry.get("direction", "LONG"),
            "strategy_tag": entry.get("strategy_tag", ""),
            "entry_trigger": levels.get("entry_trigger"),
            "stop": levels.get("stop"),
            "target": levels.get("target"),
            "last": round(last, 2) if last is not None else None,
            "dist_pct": dp,
            "status": status,
            "thesis_validation": thesis_val,
            "vehicle": entry.get("vehicle"),
            "horizon": entry.get("horizon", ""),
        }

        try:
            from ticker_lessons import load_lessons
            lessons = load_lessons(ticker, n=3)
            if lessons:
                item["lessons"] = lessons
        except Exception:
            pass

        out_items.append(item)

        tk_lower = ticker.lower()
        if status in ("TRIGGERED_VALID", "TRIGGERED_WARN", "TRIGGERED_INVALID", "TRIGGERED_REEVAL"):
            triggered.append(ticker)
            flags.append(f"watchlist_triggered_{tk_lower}")
            if status == "TRIGGERED_INVALID":
                flags.append(f"watchlist_thesis_invalid_{tk_lower}")
            elif status == "TRIGGERED_WARN":
                flags.append(f"watchlist_thesis_warn_{tk_lower}")
        elif status == "NEAR":
            near.append(ticker)
            flags.append(f"watchlist_near_{tk_lower}")
        elif status == "STOP_HIT":
            flags.append(f"watchlist_stop_hit_{tk_lower}")

    parts = [f"{len(entries)} active"]
    if triggered:
        parts.append(f"TRIGGERED={','.join(triggered)}")
    if near:
        parts.append(f"NEAR={','.join(near)}")

    result = step_result("watchlist_check", ok=True, headline="; ".join(parts),
                         data={"items": out_items}, flags=flags)
    emit(result) if args.json else print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
