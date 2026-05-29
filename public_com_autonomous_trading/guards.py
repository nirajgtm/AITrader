#!/usr/bin/env python3
"""Codified rules for the autonomous trader -- the 'constitution' AS ENFORCED CODE.

Every binding rule is a function here, reading tunable parameters from config.json.
The markdown docs are context only; THIS module is what actually enforces. All
checks are read-only decisions -- order_client.py is the only thing that acts.

Rules: armed flag, market-open (NYSE regular hours), kill-switch (equity floor),
per-position size cap, max open positions. The settled-cash / Good-Faith-Violation
rule is enforced in settlement.py (only buy with settled cash -> GFV impossible).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = None

DIR = Path(__file__).resolve().parent
CONFIG_PATH = DIR / "config.json"


def load_config() -> dict:
    """config.json with the risk block overridden by the live risk-state machine
    (risk_state.py), so guards always enforce the CURRENT evolved params, not the
    seed defaults. Falls back to the config seed if the state machine is absent."""
    cfg = json.loads(CONFIG_PATH.read_text())
    try:
        import risk_state
        cfg["risk"] = risk_state.current_params()
    except Exception:
        pass
    return cfg


def is_armed(cfg: dict | None = None) -> bool:
    """True only when config.enabled is explicitly set. Disarmed = dry-run."""
    cfg = cfg or load_config()
    return bool(cfg.get("enabled", False))


def is_market_open(now: datetime | None = None, cfg: dict | None = None) -> tuple[bool, str]:
    """(open?, reason). NYSE regular session via exchange calendar (holidays +
    half-days handled); falls back to weekday + 9:30-16:00 ET if the calendar
    library is unavailable."""
    cfg = cfg or load_config()
    now_et = (now or datetime.now(_ET)) if _ET else (now or datetime.now())
    if _ET is not None:
        now_et = now_et.astimezone(_ET) if now_et.tzinfo else now_et.replace(tzinfo=_ET)
    try:
        import pandas_market_calendars as mcal
        sched = mcal.get_calendar("NYSE").schedule(
            start_date=now_et.date(), end_date=now_et.date())
        if sched.empty:
            return False, f"market closed (no NYSE session {now_et.date()})"
        o = sched.iloc[0]["market_open"].astimezone(_ET)
        c = sched.iloc[0]["market_close"].astimezone(_ET)
        if o <= now_et <= c:
            return True, f"market open ({now_et:%H:%M} ET)"
        return False, f"market closed ({now_et:%H:%M} ET; session {o:%H:%M}-{c:%H:%M} ET)"
    except Exception:
        if now_et.weekday() >= 5:
            return False, f"market closed (weekend, {now_et:%a})"
        mins = now_et.hour * 60 + now_et.minute
        if 570 <= mins <= 960:
            return True, f"market open ({now_et:%H:%M} ET, fallback calendar)"
        return False, f"market closed ({now_et:%H:%M} ET, fallback calendar)"


def is_crypto_tradeable(cfg: dict | None = None) -> tuple[bool, str]:
    """Crypto trades 24/7 on public.com, so the only gate is the config switch.
    Equities use is_market_open (NYSE calendar); crypto bypasses it entirely."""
    cfg = cfg or load_config()
    cc = cfg.get("crypto") or {}
    if not cc.get("enabled", False):
        return False, "crypto disabled (config.crypto.enabled=false)"
    return True, "crypto open (24/7)"


def crypto_position_size_ok(usd: float, cfg: dict | None = None) -> tuple[bool, str]:
    """Per-trade crypto size cap, separate (smaller) from the equity cap because
    crypto runs 2-4x the volatility and the stop is software-only between runs."""
    cfg = cfg or load_config()
    cc = cfg.get("crypto") or {}
    mn = float(cc.get("min_position_usd", 25))
    mx = float(cc.get("max_position_usd", 150))
    if usd < mn:
        return False, f"crypto size ${usd:.2f} < min ${mn:.2f}"
    if usd > mx:
        return False, f"crypto size ${usd:.2f} > max ${mx:.2f}"
    return True, f"crypto size ${usd:.2f} within ${mn:.0f}-${mx:.0f}"


def crypto_capacity_ok(open_crypto_count: int, cfg: dict | None = None) -> tuple[bool, str]:
    cfg = cfg or load_config()
    mx = int((cfg.get("crypto") or {}).get("max_open_positions", 2))
    if open_crypto_count >= mx:
        return False, f"at crypto-position cap ({open_crypto_count}/{mx})"
    return True, f"crypto positions {open_crypto_count}/{mx}"


def crypto_budget_remaining(current_crypto_usd: float, equity_usd: float,
                            cfg: dict | None = None) -> float:
    """Dollars of crypto exposure still allowed under crypto.max_portfolio_pct (% of
    equity). None/0 pct disables the cap (returns +inf). Scales with the book."""
    cfg = cfg or load_config()
    pct = (cfg.get("crypto") or {}).get("max_portfolio_pct")
    if not pct:
        return float("inf")
    cap = float(pct) / 100.0 * float(equity_usd)
    return max(0.0, cap - float(current_crypto_usd))


def crypto_portfolio_ok(new_usd: float, current_crypto_usd: float, equity_usd: float,
                        cfg: dict | None = None) -> tuple[bool, str]:
    """Total crypto-exposure cap: held crypto market value + this new buy must stay
    within crypto.max_portfolio_pct % of equity (the user's keep-crypto-small directive).
    Binds alongside the per-position and count caps."""
    cfg = cfg or load_config()
    pct = (cfg.get("crypto") or {}).get("max_portfolio_pct")
    if not pct:
        return True, "no crypto portfolio cap configured"
    cap = float(pct) / 100.0 * float(equity_usd)
    total = float(current_crypto_usd) + float(new_usd)
    if total > cap + 1e-9:
        return False, (f"crypto exposure ${total:.2f} (held ${current_crypto_usd:.2f} + "
                       f"new ${new_usd:.2f}) > {float(pct):.0f}% of equity (${cap:.2f})")
    return True, f"crypto exposure ${total:.2f} within {float(pct):.0f}% of equity (${cap:.2f})"


def kill_switch_tripped(equity_usd: float, cfg: dict | None = None) -> tuple[bool, str]:
    cfg = cfg or load_config()
    start = float(cfg["starting_equity_usd"])
    floor = start - float(cfg["risk"]["kill_switch_drawdown_usd"])
    if equity_usd <= floor:
        return True, f"KILL-SWITCH: equity ${equity_usd:.2f} <= floor ${floor:.2f}"
    return False, f"equity ${equity_usd:.2f} above kill floor ${floor:.2f}"


def position_size_ok(usd: float, cfg: dict | None = None) -> tuple[bool, str]:
    cfg = cfg or load_config()
    mn = float(cfg["risk"]["min_position_usd"])
    mx = float(cfg["risk"]["max_position_usd"])
    if usd < mn:
        return False, f"size ${usd:.2f} < min ${mn:.2f}"
    if usd > mx:
        return False, f"size ${usd:.2f} > max ${mx:.2f}"
    return True, f"size ${usd:.2f} within ${mn:.0f}-${mx:.0f}"


def capacity_ok(open_count: int, cfg: dict | None = None) -> tuple[bool, str]:
    cfg = cfg or load_config()
    mx = int(cfg["risk"]["max_open_positions"])
    if open_count >= mx:
        return False, f"at open-position cap ({open_count}/{mx})"
    return True, f"open positions {open_count}/{mx}"


def stop_ok(entry: float, stop: float | None, side: str = "LONG",
            cfg: dict | None = None) -> tuple[bool, str]:
    """Every position MUST carry a stop-loss. Blocks a buy whose hypothesis has no
    stop, a stop on the wrong side of entry, or a stop wider than max_stop_loss_pct.
    Enforced before any buy AND a resting stop-limit is placed at the broker on fill."""
    cfg = cfg or load_config()
    if not cfg["risk"].get("require_stop_loss", True):
        return True, "stop not required (config)"
    if not stop or stop <= 0:
        return False, "no stop-loss set -- every position must have one"
    if side == "LONG":
        if stop >= entry:
            return False, f"long stop ${stop:.2f} must be below entry ${entry:.2f}"
        risk_pct = (entry - stop) / entry * 100
    else:
        if stop <= entry:
            return False, f"short stop ${stop:.2f} must be above entry ${entry:.2f}"
        risk_pct = (stop - entry) / entry * 100
    mx = float(cfg["risk"].get("max_stop_loss_pct", 15.0))
    if risk_pct > mx:
        return False, f"stop risk {risk_pct:.1f}% exceeds max {mx:.1f}%"
    return True, f"stop ${stop:.2f} ({risk_pct:.1f}% risk) within {mx:.0f}% cap"
