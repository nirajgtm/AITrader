#!/usr/bin/env python3
"""
build_thesis.py — per-ticker analysis layer.

Walks every ticker on staging.json that is reader-relevant and emits a
structured `thesis` block + grounded `detail.{plain, pro}` text, derived
from real data + the trader's codified rules. Replaces hand-typed
analyst prose with deterministic, auditable templating.

Inputs (per ticker):
  - The row itself (ticker, last, change_pct, trigger_zone, status, ...)
  - price.py SYM --json (close, RSI14, MA20/50/200, ATR14, FOMO ceiling, sector)
  - Brief-level context from staging.json: regime, vix_bucket, vol_yields

Rules applied (codified):
  - Buying-rule gate: RSI14 ≥ 70 → block new index/long entries
  - Trigger-zone protocol: row's `status` (TRIGGERED/IN_ZONE/NEAR/FAR/INVALIDATED) drives stance
  - FOMO ceiling: `last` ≥ ceiling → chase blocked (CONSTITUTION v2.2 three-tier)
  - Earnings blackout: `next_er_date` ≤ 7d → no new options
  - RSI buckets: oversold (<30) / weak (30-50) / mid (50-65) / strong (65-70) / overbought (70-80) / extreme (>80)
  - Distance from MA20 (ATR-normalized) → "stretched" vs "in-range"

Output: writes `thesis` block onto each ticker row in-place. Skill-written
thesis blocks are preserved (skill override always wins).

Usage:
  build_thesis.py                    # walk default staging.json
  build_thesis.py --staging PATH     # walk a specific file
  build_thesis.py --ticker AAPL      # one-off inspection
  build_thesis.py --dry              # don't write back, just print proposed JSON
  build_thesis.py --verbose          # per-ticker logging
"""

import argparse
import json
import os
import re
import subprocess
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")

SCRIPTS_DIR = Path(__file__).resolve().parent
SITE_DIR = Path.home() / "claude-configs" / "trader-site"
STAGING = SITE_DIR / "staging.json"
PYBIN = SCRIPTS_DIR / ".venv" / "bin" / "python3"


def log(msg, *, verbose=False, force=False):
    if not (verbose or force):
        return
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ───────────────────────────────────────────────────────────────────────
# Data fetch
# ───────────────────────────────────────────────────────────────────────

def fetch_price(sym):
    """price.py emits one step_result JSON. Returns the data dict or None."""
    try:
        out = subprocess.run(
            [str(PYBIN), str(SCRIPTS_DIR / "price.py"), sym, "--json"],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    line = out.stdout.strip().splitlines()[-1]
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not d.get("ok"):
        return None
    return d.get("data") or {}


# ───────────────────────────────────────────────────────────────────────
# Rule helpers
# ───────────────────────────────────────────────────────────────────────

def rsi_bucket(rsi):
    if rsi is None: return ("unknown", "unknown")
    if rsi < 30:    return ("oversold", "oversold")
    if rsi < 50:    return ("weak",     "below 50")
    if rsi < 65:    return ("mid",      "in the 50-65 band")
    if rsi < 70:    return ("strong",   "approaching the 70 line")
    if rsi < 80:    return ("overbought", "above 70 (overbought)")
    return            ("extreme",    "above 80 (extreme)")


def buying_rule_active(rsi):
    """v2.0 buying-rule gate: blocks new long index entries when RSI ≥ 70."""
    return rsi is not None and rsi >= 70


def fomo_status(last, ceiling):
    """Returns 'above', 'at', 'below', or None."""
    if last is None or ceiling is None:
        return None
    if last >= ceiling:    return "above"
    if last >= ceiling * 0.99: return "at"
    return "below"


def er_window(days_to_er):
    if days_to_er is None: return "none"
    if days_to_er <= 2:  return "imminent"   # 0-2d
    if days_to_er <= 7:  return "blackout"   # 3-7d
    if days_to_er <= 14: return "near"       # 8-14d
    return "far"


def trigger_direction(zone):
    """Parse the trigger_zone wording into 'long' / 'short' / 'unknown'."""
    if not zone: return "unknown"
    s = zone.lower()
    if "short" in s or "rejection" in s or "rollover" in s: return "short"
    if "long"  in s or "reversal"  in s or "bounce"   in s: return "long"
    if "pullback" in s: return "long"
    return "unknown"


# ───────────────────────────────────────────────────────────────────────
# Text composers
# ───────────────────────────────────────────────────────────────────────

def fmt_price(x):
    return f"${x:.2f}" if x is not None else "?"

def fmt_pct(x, sign=False):
    if x is None: return "?"
    return (f"+{x:.2f}%" if (sign and x >= 0) else f"{x:.2f}%")

def fmt(x, dp=2):
    return f"{x:.{dp}f}" if x is not None else "?"


def analyze_watchlist(row, pdata, ctx):
    """Watchlist row analyzer.

    Inputs:
      row    — staging.json watchlist[i]: ticker, last, change_pct, trigger_zone, status, ...
      pdata  — price.py data: rsi14, ma20, ma50, ma200, atr14, fomo_ceiling, ...
      ctx    — brief-level: regime, vix_bucket
    """
    sym   = row.get("ticker")
    last  = row.get("last") or pdata.get("close")
    chg   = row.get("change_pct") or pdata.get("chg_pct")
    zone  = row.get("trigger_zone") or ""
    status = (row.get("status") or "").upper()
    rsi   = pdata.get("rsi14")
    ma20  = pdata.get("ma20")
    atr   = pdata.get("atr14")
    fomo  = pdata.get("fomo_ceiling")
    direction = trigger_direction(zone)
    rsi_label_short, rsi_label_long = rsi_bucket(rsi)
    dist_ma20_pct = ((last - ma20) / ma20 * 100) if (last and ma20) else None
    atr_units_from_ma20 = (abs(last - ma20) / atr) if (last and ma20 and atr) else None

    # Stance from status
    if status == "TRIGGERED":
        stance = "WAIT"  # wait for confirmation
    elif status in ("IN_ZONE", "IN_ZONE_AWAITING_CONFIRMATION"):
        stance = "WAIT"
    elif status == "NEAR":
        stance = "WAIT"
    elif status == "FAR":
        stance = "WATCH"
    elif status == "INVALIDATED":
        stance = "WATCH"
    else:
        stance = "WATCH"

    # ── why (analytical reason for no-action today)
    if status == "TRIGGERED":
        why_p = (f"{sym} entered the {zone} zone today (last {fmt_price(last)}, "
                 f"{fmt_pct(chg, sign=True)}). RSI {fmt(rsi, 1)} is {rsi_label_long}. "
                 f"Triggered status means the level is reached, but we wait one bar for confirmation before sizing.")
        why_pr = (f"{sym} {fmt_price(last)} {fmt_pct(chg, sign=True)} entered {zone}; "
                  f"RSI14 {fmt(rsi,1)}. Trigger reached, awaiting confirmation candle.")
    elif status == "INVALIDATED":
        why_p = (f"{sym} thesis is invalidated. Last {fmt_price(last)} broke through the "
                 f"level the original setup required. The setup is dead until a fresh thesis emerges.")
        why_pr = f"{sym} {fmt_price(last)}; trigger zone INVALIDATED. Setup dead until fresh thesis."
    elif status == "NEAR":
        why_p = (f"{sym} is approaching the {zone} zone but hasn't reached it yet (last {fmt_price(last)}, "
                 f"{fmt_pct(chg, sign=True)}). RSI {fmt(rsi, 1)} is {rsi_label_long}. "
                 f"We don't pre-front the zone; let price come to us.")
        why_pr = (f"{sym} {fmt_price(last)} status NEAR {zone}; RSI14 {fmt(rsi,1)}. "
                  f"No pre-front — wait for zone touch.")
    elif status == "FAR":
        why_p = (f"{sym} is currently far from the {zone} setup (last {fmt_price(last)}, "
                 f"{fmt_pct(chg, sign=True)}). The level the trade requires isn't in play right now. "
                 f"RSI {fmt(rsi, 1)} is {rsi_label_long}.")
        why_pr = (f"{sym} {fmt_price(last)} status FAR from {zone}; RSI14 {fmt(rsi,1)}. "
                  f"No actionable level today.")
    else:  # IN_ZONE / unknown
        why_p = (f"{sym} sits inside the {zone} zone (last {fmt_price(last)}). "
                 f"RSI {fmt(rsi, 1)} is {rsi_label_long}. We hold for a confirmation bar before entering.")
        why_pr = f"{sym} {fmt_price(last)} in zone {zone}; RSI14 {fmt(rsi,1)}. Hold for confirm."

    # ── waiting_for (concrete trigger to act)
    if direction == "short" and status in ("TRIGGERED", "IN_ZONE", "NEAR"):
        wait_p = (f"First red close inside the zone followed by a confirmation lower-high. "
                  f"Volume above the 5-day average on the rejection candle.")
        wait_pr = (f"Short trigger: first close inside {zone} + lower-high; "
                   f"vol > 5d MA on rejection bar.")
    elif direction == "long" and status in ("TRIGGERED", "IN_ZONE", "NEAR"):
        wait_p = (f"A green close inside the zone with a reversal candle on volume above the 5-day average.")
        wait_pr = (f"Long trigger: close inside {zone} + reversal candle; vol > 5d MA.")
    elif direction == "short" and status == "FAR":
        wait_p = f"A relief bounce up into the {zone} zone followed by a rejection candle."
        wait_pr = f"Rally to {zone} + rejection candle; trigger on next daily close < intraday low."
    elif direction == "long" and status == "FAR":
        wait_p = f"A pullback down into the {zone} zone followed by a reversal candle."
        wait_pr = f"Pullback to {zone} + reversal candle; trigger on next close > intraday high."
    elif status == "INVALIDATED":
        wait_p = "Nothing — the original setup is dead. We re-engage only if a fresh thesis emerges."
        wait_pr = "INVALIDATED — no trigger. Archive candidate."
    else:
        wait_p = f"Price to enter the {zone} zone with a confirmation candle on volume."
        wait_pr = f"Entry into {zone} + confirmation bar with vol confirm."

    # ── watching (signals/levels we monitor)
    watch_p_parts = []
    watch_pr_parts = []
    if rsi is not None:
        if direction == "short":
            watch_p_parts.append(f"RSI {fmt(rsi, 1)} ({rsi_label_long}); a clean short setup wants RSI fading from above 70.")
            watch_pr_parts.append(f"RSI14 {fmt(rsi,1)} ({rsi_label_short}); short bias improves on RSI rolling from > 70.")
        elif direction == "long":
            watch_p_parts.append(f"RSI {fmt(rsi, 1)} ({rsi_label_long}); a clean long setup wants RSI rising from below 40.")
            watch_pr_parts.append(f"RSI14 {fmt(rsi,1)} ({rsi_label_short}); long bias improves on RSI cross > 40 from below.")
        else:
            watch_p_parts.append(f"RSI {fmt(rsi, 1)} ({rsi_label_long}).")
            watch_pr_parts.append(f"RSI14 {fmt(rsi,1)} ({rsi_label_short}).")
    if dist_ma20_pct is not None:
        side = "above" if dist_ma20_pct >= 0 else "below"
        watch_p_parts.append(f"Price is {abs(dist_ma20_pct):.1f}% {side} the 20-day average ({fmt_price(ma20)}).")
        watch_pr_parts.append(f"Dist to MA20: {fmt(dist_ma20_pct, 1)}% ({side}).")
    if fomo and last and direction == "long":
        f_status = fomo_status(last, fomo)
        if f_status == "above":
            watch_p_parts.append(f"Last is above the chase-too-late level ({fmt_price(fomo)}); the FOMO gate blocks chasing.")
            watch_pr_parts.append(f"FOMO ceiling {fmt(fomo,2)}: gate ACTIVE.")
        elif f_status == "below":
            watch_p_parts.append(f"Last sits below the chase-too-late level ({fmt_price(fomo)}); gate clear.")
            watch_pr_parts.append(f"FOMO ceiling {fmt(fomo,2)}: clear.")
    watch_p = " ".join(watch_p_parts) if watch_p_parts else ""
    watch_pr = " ".join(watch_pr_parts) if watch_pr_parts else ""

    # ── expecting (base case)
    if atr is not None and last is not None:
        lo, hi = last - atr, last + atr
        exp_p = (f"Base case: {sym} chops in roughly {fmt_price(lo)}-{fmt_price(hi)} over the next few sessions. "
                 f"That's one normal day's swing in either direction.")
        exp_pr = (f"Base range: {fmt(lo,2)}-{fmt(hi,2)} (1 ATR14 either side of {fmt(last,2)}); "
                  f"resolution within typical daily range.")
    else:
        exp_p = f"Base case: choppy around {fmt_price(last)} until a catalyst hits."
        exp_pr = f"Base case: chop near {fmt_price(last)}; binary on catalyst."

    return {
        "stance": stance,
        "why":         {"plain": why_p,   "pro": why_pr},
        "waiting_for": {"plain": wait_p,  "pro": wait_pr},
        "watching":    {"plain": watch_p, "pro": watch_pr} if watch_p else None,
        "expecting":   {"plain": exp_p,   "pro": exp_pr},
    }


def analyze_index(row, pdata, ctx):
    """Macro index analyzer (SPY/QQQ/IWM/DIA). Buying-rule gate is the dominant rule."""
    sym  = row.get("ticker")
    last = row.get("last") or pdata.get("close")
    rsi  = pdata.get("rsi14")
    ma20 = pdata.get("ma20")
    atr  = pdata.get("atr14")
    chg  = pdata.get("chg_pct")
    rsi_label_short, rsi_label_long = rsi_bucket(rsi)
    gate = buying_rule_active(rsi)
    dist_ma20_pct = ((last - ma20) / ma20 * 100) if (last and ma20) else None

    name_map = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000", "DIA": "Dow"}
    pretty = name_map.get(sym, sym)

    if gate:
        stance = "WATCH"
        why_p = (f"{pretty} is overbought with the meter at {fmt(rsi, 1)}, well past the 70 line. "
                 f"The buying rule blocks new long index entries while RSI stays here.")
        why_pr = f"{sym} RSI14 {fmt(rsi,1)} — buying-rule gate ACTIVE. New index longs blocked until RSI < 70."
        wait_p = f"RSI to cool below 70 with price holding above the 20-day average ({fmt_price(ma20)})."
        wait_pr = f"RSI14 < 70 + price > MA20 ({fmt(ma20,2)})."
    elif rsi is not None and rsi < 30:
        stance = "WATCH"
        why_p = (f"{pretty} is oversold with the meter at {fmt(rsi, 1)}, below the 30 line. "
                 f"Bounces from this zone are common but require confirmation.")
        why_pr = f"{sym} RSI14 {fmt(rsi,1)} oversold. Bounce candidates from < 30 require confirmation candle."
        wait_p = "RSI to cross back above 30 with a reversal candle on volume."
        wait_pr = f"RSI14 cross > 30 + reversal candle, vol > 5d MA."
    else:
        stance = "WATCH"
        why_p = (f"{pretty} is in the {rsi_label_long} zone (RSI {fmt(rsi, 1)}). "
                 f"No specific gate firing today; trend is intact while above the 20-day average.")
        why_pr = f"{sym} RSI14 {fmt(rsi,1)} — neutral zone, no gate. Trend ok > MA20 ({fmt(ma20,2)})."
        wait_p = "A clean trend signal — either RSI breaking above 70 (extension) or below 50 (cooling)."
        wait_pr = f"RSI14 break > 70 (extension) or < 50 (cooling)."

    # watching: breadth + sector + VIX bucket from context
    watch_parts_p = []
    watch_parts_pr = []
    if dist_ma20_pct is not None:
        side = "above" if dist_ma20_pct >= 0 else "below"
        watch_parts_p.append(f"Price is {abs(dist_ma20_pct):.1f}% {side} the 20-day average.")
        watch_parts_pr.append(f"Dist to MA20: {fmt(dist_ma20_pct,1)}% ({side}).")
    vix_bucket = (ctx or {}).get("vix_bucket")
    if vix_bucket:
        watch_parts_p.append(f"Volatility regime: {vix_bucket}.")
        watch_parts_pr.append(f"VIX bucket: {vix_bucket}.")
    regime = (ctx or {}).get("regime")
    if regime:
        watch_parts_p.append(f"Macro regime: {regime.lower()}.")
        watch_parts_pr.append(f"Regime: {regime}.")

    watch_p = " ".join(watch_parts_p) if watch_parts_p else ""
    watch_pr = " ".join(watch_parts_pr) if watch_parts_pr else ""

    if atr and last:
        lo, hi = last - atr, last + atr
        exp_p = (f"Base case is a 1-day swing of about {fmt_price(atr)} in either direction, "
                 f"so roughly {fmt_price(lo)}-{fmt_price(hi)}.")
        exp_pr = f"Base range: {fmt(lo,2)}-{fmt(hi,2)} (1 ATR14 around {fmt(last,2)})."
    else:
        exp_p = f"Base case is choppy around {fmt_price(last)} until a catalyst hits."
        exp_pr = f"Base case: chop near {fmt(last,2)}."

    return {
        "stance": stance,
        "why":         {"plain": why_p,   "pro": why_pr},
        "waiting_for": {"plain": wait_p,  "pro": wait_pr},
        "watching":    {"plain": watch_p, "pro": watch_pr} if watch_p else None,
        "expecting":   {"plain": exp_p,   "pro": exp_pr},
    }


def analyze_mover(row, pdata, ctx, side):
    """side ∈ {'gainers', 'losers'} from staging.movers structure."""
    sym  = row.get("ticker")
    last = row.get("last") or pdata.get("close")
    chg  = row.get("chg") or row.get("change_pct") or pdata.get("chg_pct")
    rsi  = pdata.get("rsi14")
    ma20 = pdata.get("ma20")
    atr  = pdata.get("atr14")
    fomo = pdata.get("fomo_ceiling")
    rsi_label_short, rsi_label_long = rsi_bucket(rsi)
    dist_ma20_pct = ((last - ma20) / ma20 * 100) if (last and ma20) else None

    big_move = chg is not None and abs(chg) >= 5

    if side == "gainers":
        # Big gainer logic: chase blocked by FOMO; wait for pullback
        stance = "WATCH"
        why_p = (f"{sym} is up {fmt_pct(chg, sign=True)} today on the gainers list (last {fmt_price(last)}). "
                 f"RSI {fmt(rsi, 1)} is {rsi_label_long}. We don't chase {abs(chg):.1f}% same-day moves "
                 f"without a follow-through plan; the FOMO rule blocks entries this stretched.")
        why_pr = (f"{sym} {fmt_pct(chg, sign=True)} {fmt_price(last)}; RSI14 {fmt(rsi,1)}. "
                  f"FOMO gate filters chase on a {abs(chg):.1f}% same-day move.")
        wait_p = "A pullback to the 5-day average followed by a green bar on volume; or a 1-2 day pause then continuation higher."
        wait_pr = "Pullback to 5DMA + reversal candle on vol > 5d MA, OR daily close > today's high after a pause."
    else:  # losers
        stance = "WATCH"
        why_p = (f"{sym} is down {fmt_pct(chg, sign=True)} today on the losers list (last {fmt_price(last)}). "
                 f"RSI {fmt(rsi, 1)} is {rsi_label_long}. Falling-knife rule applies — we don't catch a same-day "
                 f"breakdown without a confirmation bar.")
        why_pr = (f"{sym} {fmt_pct(chg, sign=True)} {fmt_price(last)}; RSI14 {fmt(rsi,1)}. "
                  f"No catch on a same-day breakdown; need stabilization candle.")
        wait_p = "A stabilization candle (small range, holds support) followed by a green bar on volume."
        wait_pr = "Stabilization bar + reversal candle on vol > 5d MA; or break of today's high to flip back into the long-side read."

    watch_parts_p = []
    watch_parts_pr = []
    if dist_ma20_pct is not None:
        sd = "above" if dist_ma20_pct >= 0 else "below"
        watch_parts_p.append(f"Price is {abs(dist_ma20_pct):.1f}% {sd} the 20-day average ({fmt_price(ma20)}).")
        watch_parts_pr.append(f"Dist to MA20: {fmt(dist_ma20_pct,1)}% ({sd}).")
    if fomo and last and side == "gainers":
        st = fomo_status(last, fomo)
        if st in ("at", "above"):
            watch_parts_p.append(f"Last sits {st} the chase-too-late level ({fmt_price(fomo)}); the FOMO gate is firing.")
            watch_parts_pr.append(f"FOMO ceiling {fmt(fomo,2)}: gate ACTIVE.")

    watch_p = " ".join(watch_parts_p) if watch_parts_p else ""
    watch_pr = " ".join(watch_parts_pr) if watch_parts_pr else ""

    if atr and last:
        lo, hi = last - atr, last + atr
        exp_p = (f"Base case is a 1-2 day {'cool-off' if side=='gainers' else 'stabilization'} "
                 f"in roughly {fmt_price(lo)}-{fmt_price(hi)} before the next directional read.")
        exp_pr = f"Base range: {fmt(lo,2)}-{fmt(hi,2)} (1 ATR14 around {fmt(last,2)})."
    else:
        exp_p = f"Base case is choppy around {fmt_price(last)} for 1-2 sessions."
        exp_pr = f"Base case: 1-2 session chop near {fmt(last,2)}."

    return {
        "stance": stance,
        "why":         {"plain": why_p,   "pro": why_pr},
        "waiting_for": {"plain": wait_p,  "pro": wait_pr},
        "watching":    {"plain": watch_p, "pro": watch_pr} if watch_p else None,
        "expecting":   {"plain": exp_p,   "pro": exp_pr},
    }


def analyze_smart_money(entry, pdata, ctx):
    """Smart-money cluster entry. Either string or {ticker, signals[], detail}."""
    if isinstance(entry, str):
        sym, signals, prior_detail = entry, [], None
    else:
        sym = entry.get("ticker")
        signals = list(entry.get("signals") or [])
        prior_detail = entry.get("detail")
    last = pdata.get("close")
    chg  = pdata.get("chg_pct")
    rsi  = pdata.get("rsi14")
    ma20 = pdata.get("ma20")
    atr  = pdata.get("atr14")
    fomo = pdata.get("fomo_ceiling")
    rsi_label_short, rsi_label_long = rsi_bucket(rsi)
    dist_ma20_pct = ((last - ma20) / ma20 * 100) if (last and ma20) else None
    n = len(signals) or 2  # cluster requires ≥2 by definition

    sig_str = ", ".join(signals) if signals else "two or more independent scanners"

    if rsi is not None and rsi >= 70:
        # Cluster on a stretched name → wait for pullback
        stance = "WAIT"
        why_p = (f"{sym} lit up on {n} independent scanners ({sig_str}) but it's already extended "
                 f"with RSI {fmt(rsi, 1)} ({rsi_label_long}). We don't chase clusters without a confirmed entry.")
        why_pr = f"{sym} {n}-source cluster ({sig_str}); RSI14 {fmt(rsi,1)} extended. Wait for entry confirmation."
        wait_p = "A pullback to the 5-day or 20-day average that holds with a green bar on volume."
        wait_pr = "Pullback to 5DMA / MA20 + reversal candle on vol > 5d MA."
    else:
        stance = "WATCH"
        why_p = (f"{sym} shows up on {n} independent scanners ({sig_str}). Cluster confirmation is the highest-quality "
                 f"directional read in the universe — but price action still has to confirm before we size.")
        why_pr = f"{sym} {n}-source cluster ({sig_str}); RSI14 {fmt(rsi,1)}. Awaiting price-action confirm."
        wait_p = "A clean breakout on volume above the 5-day average, or a pullback hold with reversal candle."
        wait_pr = "Daily close > recent high on vol > 5d ADV, OR pullback to 5DMA + reversal candle."

    watch_parts_p = []
    watch_parts_pr = []
    if dist_ma20_pct is not None:
        sd = "above" if dist_ma20_pct >= 0 else "below"
        watch_parts_p.append(f"Price is {abs(dist_ma20_pct):.1f}% {sd} the 20-day average ({fmt_price(ma20)}).")
        watch_parts_pr.append(f"Dist to MA20: {fmt(dist_ma20_pct,1)}% ({sd}).")
    if fomo and last:
        st = fomo_status(last, fomo)
        if st in ("at", "above"):
            watch_parts_p.append(f"Last sits {st} the chase-too-late level ({fmt_price(fomo)}); FOMO gate is firing.")
            watch_parts_pr.append(f"FOMO ceiling {fmt(fomo,2)}: gate ACTIVE.")
    watch_p = " ".join(watch_parts_p) if watch_parts_p else ""
    watch_pr = " ".join(watch_parts_pr) if watch_parts_pr else ""

    if atr and last:
        lo, hi = last - atr, last + atr
        exp_p = (f"Base case is choppy in roughly {fmt_price(lo)}-{fmt_price(hi)} until a catalyst confirms direction.")
        exp_pr = f"Base range: {fmt(lo,2)}-{fmt(hi,2)} (1 ATR14 around {fmt(last,2)}); binary on catalyst."
    else:
        exp_p = f"Base case is choppy around {fmt_price(last)} until a catalyst hits."
        exp_pr = f"Base case: chop near {fmt(last,2)}."

    return {
        "stance": stance,
        "why":         {"plain": why_p,   "pro": why_pr},
        "waiting_for": {"plain": wait_p,  "pro": wait_pr},
        "watching":    {"plain": watch_p, "pro": watch_pr} if watch_p else None,
        "expecting":   {"plain": exp_p,   "pro": exp_pr},
    }


# ───────────────────────────────────────────────────────────────────────
# Walk + write
# ───────────────────────────────────────────────────────────────────────

def is_valid_ticker(s):
    return isinstance(s, str) and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,7}", s) is not None


def walk_staging(staging, *, verbose=False):
    """In-place mutate staging['stocks']['watchlist'][i].thesis etc."""
    ctx = {
        "regime": staging.get("regime"),
        "vix_bucket": staging.get("vix_bucket"),
        "regime_note": staging.get("regime_note"),
    }
    counts = {"watchlist": 0, "indices": 0, "movers": 0, "smart_money": 0, "skipped_existing": 0}

    # 1. Watchlist
    for w in (staging.get("stocks") or {}).get("watchlist", []) or []:
        if w.get("thesis"):
            counts["skipped_existing"] += 1; continue
        sym = w.get("ticker")
        if not is_valid_ticker(sym): continue
        pdata = fetch_price(sym) or {}
        if not pdata:
            log(f"  watchlist {sym}: no price data, skipping", verbose=verbose); continue
        w["thesis"] = analyze_watchlist(w, pdata, ctx)
        counts["watchlist"] += 1
        log(f"  watchlist {sym}: stance={w['thesis']['stance']}", verbose=verbose)

    # 2. Indices
    for idx in (staging.get("macro") or {}).get("indices", []) or []:
        if idx.get("thesis"):
            counts["skipped_existing"] += 1; continue
        sym = idx.get("ticker")
        if not is_valid_ticker(sym): continue
        pdata = fetch_price(sym) or {}
        if not pdata:
            log(f"  index {sym}: no price data, skipping", verbose=verbose); continue
        idx["thesis"] = analyze_index(idx, pdata, ctx)
        counts["indices"] += 1
        log(f"  index {sym}: stance={idx['thesis']['stance']}", verbose=verbose)

    # 3. Movers
    movers = (staging.get("stocks") or {}).get("movers") or {}
    for side in ("gainers", "losers"):
        for m in (movers.get(side) or []):
            if m.get("thesis"):
                counts["skipped_existing"] += 1; continue
            sym = m.get("ticker")
            if not is_valid_ticker(sym): continue
            pdata = fetch_price(sym) or {}
            if not pdata:
                log(f"  mover {sym}: no price data, skipping", verbose=verbose); continue
            m["thesis"] = analyze_mover(m, pdata, ctx, side)
            counts["movers"] += 1
            log(f"  mover {sym}: stance={m['thesis']['stance']}", verbose=verbose)

    # 4. Smart-money clusters
    sm = (staging.get("stocks") or {}).get("smart_money_clusters") or []
    new_sm = []
    for entry in sm:
        if isinstance(entry, dict) and entry.get("thesis"):
            new_sm.append(entry); counts["skipped_existing"] += 1; continue
        sym = entry if isinstance(entry, str) else (entry.get("ticker") if isinstance(entry, dict) else None)
        if not is_valid_ticker(sym):
            new_sm.append(entry); continue
        pdata = fetch_price(sym) or {}
        if not pdata:
            new_sm.append(entry); log(f"  sm {sym}: no price data, skipping", verbose=verbose); continue
        thesis = analyze_smart_money(entry, pdata, ctx)
        if isinstance(entry, dict):
            entry["thesis"] = thesis
            new_sm.append(entry)
        else:
            new_sm.append({"ticker": entry, "signals": [], "thesis": thesis})
        counts["smart_money"] += 1
        log(f"  sm {sym}: stance={thesis['stance']}", verbose=verbose)
    if sm:
        staging.setdefault("stocks", {})["smart_money_clusters"] = new_sm

    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--staging", type=Path, default=STAGING)
    ap.add_argument("--ticker", help="One-off inspection: print thesis JSON for one ticker")
    ap.add_argument("--dry", action="store_true", help="Don't write back; print proposed JSON")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.ticker:
        sym = args.ticker.upper()
        pdata = fetch_price(sym)
        if not pdata:
            print(json.dumps({"ok": False, "ticker": sym, "error": "no price data"}))
            return 1
        # Use a default-context analyzer (mover-style for unknown tickers)
        ctx = {"regime": "BULL", "vix_bucket": "calm"}
        # Try as index first if it's a known one
        if sym in {"SPY","QQQ","IWM","DIA"}:
            thesis = analyze_index({"ticker": sym, "last": pdata.get("close")}, pdata, ctx)
        else:
            thesis = analyze_mover({"ticker": sym, "chg": pdata.get("chg_pct")}, pdata, ctx, "gainers")
        print(json.dumps({"ok": True, "ticker": sym, "data_used": pdata, "thesis": thesis}, indent=2))
        return 0

    if not args.staging.exists():
        log(f"staging missing: {args.staging}", force=True)
        return 1
    staging = json.loads(args.staging.read_text())
    log(f"walking {args.staging}", force=True)
    counts = walk_staging(staging, verbose=args.verbose)
    log(f"thesis added: watchlist={counts['watchlist']} indices={counts['indices']} "
        f"movers={counts['movers']} smart_money={counts['smart_money']} "
        f"(skipped {counts['skipped_existing']} pre-existing)", force=True)

    if args.dry:
        return 0
    args.staging.write_text(json.dumps(staging, indent=2))
    log(f"wrote {args.staging}", force=True)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
