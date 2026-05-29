"""Tradable-window classifier for brief signal actions.

Tags each action with: tradable_now, tradable_window_label, next_open_eta.
Source of truth: knowledge/robinhood_after_hours.md (verified 2026-05-02).

Times are evaluated in America/New_York (ET) since RH/Cboe schedule is ET.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None  # zoneinfo missing in old Python; fallback to naive


# Action kind -> classification key. Keep this in sync with what scripts emit.
KIND_TO_INSTRUMENT = {
    "leap_entry": "equity_option",        # SPY/QQQ options = single-name equity option
    "option_drawdown_action": "equity_option",
    "vix_play": "index_option_extended",  # SPX/VIX/XSP/RUT have overnight window
    "position_action": "equity_option",   # most user positions are options
    "leap_roll": "equity_option",
    "leap_stop": "equity_option",
    "stock_entry": "equity_extended",
    "crypto_action": "crypto",
    "futures_action": "futures",
}


def _now_et() -> datetime:
    if _ET is not None:
        return datetime.now(_ET)
    return datetime.now()


def _is_weekend(dt: datetime) -> bool:
    # Monday=0 ... Sunday=6
    return dt.weekday() >= 5


def _next_weekday(dt: datetime, target_weekday: int) -> datetime:
    """Return next datetime with the given weekday (0=Mon...6=Sun)."""
    days_ahead = (target_weekday - dt.weekday()) % 7
    if days_ahead == 0 and dt.time() >= time(0, 0):
        days_ahead = 7
    return dt + timedelta(days=days_ahead)


def _classify_equity_option(now: datetime) -> tuple[bool, str, int | None]:
    """Equity options: 9:30 AM - 4:00 PM ET, weekdays only."""
    label = "regular hours only (9:30 AM - 4:00 PM ET)"
    if _is_weekend(now):
        # Next open: Monday 9:30 AM ET
        next_open = _next_weekday(now.replace(hour=9, minute=30, second=0, microsecond=0), 0)
        eta = int((next_open - now).total_seconds() / 60)
        return False, label, eta
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if open_t <= now < close_t:
        return True, label, None
    if now < open_t:
        eta = int((open_t - now).total_seconds() / 60)
        return False, label, eta
    # After close on a weekday
    if now.weekday() < 4:  # Mon-Thu
        next_open = (now + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
    else:  # Friday after close -> Monday open
        next_open = _next_weekday(now.replace(hour=9, minute=30, second=0, microsecond=0), 0)
    eta = int((next_open - now).total_seconds() / 60)
    return False, label, eta


def _classify_index_option_extended(now: datetime) -> tuple[bool, str, int | None]:
    """SPX/VIX/XSP/RUT: regular 9:30-5pm + overnight 8:15 PM - 9:25 AM ET, Sun-Fri."""
    label = "regular 9:30 AM - 5:00 PM ET + overnight 8:15 PM - 9:25 AM ET (Sun-Fri)"
    weekday = now.weekday()
    t = now.time()

    # Friday 5:00 PM through Sunday 6:00 PM is hard closed (no overnight Friday eve)
    # Overnight reopens Sunday 6 PM (futures-style) but Cboe GTH is 8:15 PM Sun.
    # Saturday: closed all day
    if weekday == 5:  # Saturday
        next_open = (now + timedelta(days=1)).replace(hour=20, minute=15, second=0, microsecond=0)
        return False, label, int((next_open - now).total_seconds() / 60)
    # Friday after 5 PM: closed
    if weekday == 4 and t >= time(17, 0):
        next_open = (now + timedelta(days=2)).replace(hour=20, minute=15, second=0, microsecond=0)
        # Sunday 8:15 PM ET
        return False, label, int((next_open - now).total_seconds() / 60)
    # Sunday before 8:15 PM: closed
    if weekday == 6 and t < time(20, 15):
        next_open = now.replace(hour=20, minute=15, second=0, microsecond=0)
        return False, label, int((next_open - now).total_seconds() / 60)
    # Monday-Friday during/around regular hours
    if weekday < 5:
        # Regular session 9:30 AM - 5:00 PM
        regular_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        regular_close = now.replace(hour=17, minute=0, second=0, microsecond=0)
        if regular_open <= now < regular_close:
            return True, label, None
        # Overnight session 8:15 PM - 9:25 AM ET (next morning)
        evening_open = now.replace(hour=20, minute=15, second=0, microsecond=0)
        if t >= time(20, 15):
            return True, label, None
        if t < time(9, 25):
            return True, label, None
        # Between 5 PM and 8:15 PM on a weekday: closed for ~3 hours
        if t >= time(17, 0) and t < time(20, 15):
            next_open = now.replace(hour=20, minute=15, second=0, microsecond=0)
            return False, label, int((next_open - now).total_seconds() / 60)
        # Between 9:25 AM and 9:30 AM: closed for 5 minutes
        if time(9, 25) <= t < time(9, 30):
            next_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            return False, label, int((next_open - now).total_seconds() / 60)
    # Sunday 8:15 PM and later: open
    if weekday == 6 and t >= time(20, 15):
        return True, label, None
    return False, label, None


def _classify_equity_extended(now: datetime) -> tuple[bool, str, int | None]:
    """Stocks/ETFs in extended hours: 7 AM - 8 PM ET weekdays.
    24-Hour Market for select symbols (~900) extends Sun 8 PM - Fri 8 PM."""
    label = "extended 7 AM - 8 PM ET weekdays (24-Hour Market for select symbols Sun 8 PM - Fri 8 PM)"
    weekday = now.weekday()
    t = now.time()
    if weekday == 5:  # Saturday: hard closed
        next_open = (now + timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
        return False, label, int((next_open - now).total_seconds() / 60)
    if weekday == 6 and t < time(20, 0):  # Sunday before 8 PM
        next_open = now.replace(hour=20, minute=0, second=0, microsecond=0)
        return False, label, int((next_open - now).total_seconds() / 60)
    if weekday == 4 and t >= time(20, 0):  # Friday after 8 PM
        next_open = (now + timedelta(days=2)).replace(hour=20, minute=0, second=0, microsecond=0)
        return False, label, int((next_open - now).total_seconds() / 60)
    if weekday < 5:  # Mon-Fri
        if time(7, 0) <= t < time(20, 0):
            return True, label, None
        if t < time(7, 0):
            next_open = now.replace(hour=7, minute=0, second=0, microsecond=0)
            return False, label, int((next_open - now).total_seconds() / 60)
        # After 8 PM weekday: 24-Hour Market still open for select symbols
        return True, "after-hours via 24-Hour Market (select symbols only)", None
    # Sunday 8 PM and later: open via 24-Hour Market
    if weekday == 6 and t >= time(20, 0):
        return True, "24-Hour Market (Sun 8 PM open)", None
    return False, label, None


def _classify_crypto(now: datetime) -> tuple[bool, str, int | None]:
    return True, "24/7", None


def _classify_futures(now: datetime) -> tuple[bool, str, int | None]:
    """Futures: 6 PM Sun - 5 PM Fri ET, daily halt 5-6 PM ET."""
    label = "Sun 6 PM - Fri 5 PM ET (daily halt 5-6 PM ET)"
    weekday = now.weekday()
    t = now.time()
    # Saturday: closed
    if weekday == 5:
        next_open = (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
        return False, label, int((next_open - now).total_seconds() / 60)
    # Sunday before 6 PM: closed
    if weekday == 6 and t < time(18, 0):
        next_open = now.replace(hour=18, minute=0, second=0, microsecond=0)
        return False, label, int((next_open - now).total_seconds() / 60)
    # Friday after 5 PM: closed
    if weekday == 4 and t >= time(17, 0):
        next_open = (now + timedelta(days=2)).replace(hour=18, minute=0, second=0, microsecond=0)
        return False, label, int((next_open - now).total_seconds() / 60)
    # Daily halt 5-6 PM Mon-Thu
    if weekday < 4 and time(17, 0) <= t < time(18, 0):
        next_open = now.replace(hour=18, minute=0, second=0, microsecond=0)
        return False, label, int((next_open - now).total_seconds() / 60)
    return True, label, None


_CLASSIFIERS = {
    "equity_option": _classify_equity_option,
    "index_option_extended": _classify_index_option_extended,
    "equity_extended": _classify_equity_extended,
    "crypto": _classify_crypto,
    "futures": _classify_futures,
}


def tag_action(action: dict) -> dict:
    """Return action with tradable_now, tradable_window_label, next_open_eta_min added."""
    kind = action.get("kind") or ""
    instrument = KIND_TO_INSTRUMENT.get(kind)
    if instrument is None:
        return {**action, "tradable_now": None, "tradable_window_label": "unknown",
                "next_open_eta_min": None}
    now = _now_et()
    classifier = _CLASSIFIERS[instrument]
    is_now, label, eta = classifier(now)
    out = dict(action)
    out["tradable_now"] = is_now
    out["tradable_window_label"] = label
    out["next_open_eta_min"] = eta
    out["instrument_class"] = instrument
    return out


def tag_actions(actions: list) -> list:
    return [tag_action(a) for a in (actions or [])]
