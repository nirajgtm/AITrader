#!/usr/bin/env python3
"""vix_check.py -- daily VIX option play signal for the Robinhood book.

VIX options on RH are native (Cboe partnership, early 2025): cash-settled,
European, AM-settled on VRO Wednesday, 100x multiplier, Section 1256 tax.

Critical: VIX options price off VIX FUTURES not spot VIX. We use the term
structure (^VIX9D, ^VIX, ^VIX3M, ^VIX6M) as the futures-curve proxy and
pull the actual option chain for live premiums.

Signal logic:
  BACKWARDATION (spot > VIX3M):
    -> WAIT  (vol shock in progress; do not enter new vol trades)
  EXTREME_LOW (spot < 13, contango > 4):
    -> HEDGE_LONG_CALL  (cheap insurance; complacent regime)
  EXTREME_HIGH (spot > 28):
    -> FADE_VOL_SHORT_CALL_SPREAD  (mean-revert play; vol regimes don't sustain)
  NORMAL (14 <= spot <= 22, contango > 2):
    -> SHORT_PUT_SPREAD  (collect contango decay; structural tailwind)
  ELSE:
    -> WAIT  (no clear edge)

Outputs ONE actionable trade per day with strike/expiry/premium and reason,
or WAIT with why. All values pulled from yfinance this session, no recalls.

Usage:
  vix_check.py [--target-dte 45] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta


def fetch_term_structure() -> dict | None:
    try:
        import yfinance as yf
    except ImportError:
        return None
    out = {}
    for label, sym in [("vix_9d", "^VIX9D"), ("vix_spot", "^VIX"),
                       ("vix_3m", "^VIX3M"), ("vix_6m", "^VIX6M"),
                       ("vvix", "^VVIX")]:
        try:
            h = yf.Ticker(sym).history(period="3d")
            out[label] = float(h["Close"].iloc[-1]) if not h.empty else None
        except Exception:
            out[label] = None
    return out


def pick_expiry(target_dte: int) -> tuple[str | None, int | None]:
    """Find the listed VIX expiry closest to today + target_dte."""
    try:
        import yfinance as yf
    except ImportError:
        return None, None
    try:
        expiries = list(yf.Ticker("^VIX").options or [])
    except Exception:
        return None, None
    if not expiries:
        return None, None
    today = date.today()
    target = today + timedelta(days=target_dte)
    best = None
    best_diff = None
    for e in expiries:
        try:
            d = date.fromisoformat(e)
        except ValueError:
            continue
        diff = abs((d - target).days)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best = e
    if best is None:
        return None, None
    actual_dte = (date.fromisoformat(best) - today).days
    return best, actual_dte


def fetch_vix_chain(expiry_iso: str, right: str) -> list:
    """Return list of {strike, bid, ask, mid, lastPrice} for the chosen leg."""
    try:
        import yfinance as yf
    except ImportError:
        return []
    try:
        chain = yf.Ticker("^VIX").option_chain(expiry_iso)
        df = chain.calls if right == "C" else chain.puts
    except Exception:
        return []
    out = []
    for _, row in df.iterrows():
        bid = float(row.get("bid") or 0)
        ask = float(row.get("ask") or 0)
        last = float(row.get("lastPrice") or 0)
        mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else (last if last > 0 else 0)
        out.append({"strike": float(row["strike"]),
                    "bid": bid, "ask": ask, "last": last, "mid": round(mid, 2)})
    out.sort(key=lambda x: x["strike"])
    return out


def find_strike(chain: list, target_strike: float) -> dict | None:
    if not chain:
        return None
    return min(chain, key=lambda c: abs(c["strike"] - target_strike))


def classify_regime(ts: dict) -> tuple[str, float, float]:
    """Return (regime, contango_pp, futures_anchor) where futures_anchor is
    the proxy price for ~30-60 DTE VIX options (VIX3M)."""
    spot = ts.get("vix_spot")
    vix_3m = ts.get("vix_3m")
    if spot is None or vix_3m is None:
        return "unknown", 0.0, spot or 20.0
    contango = vix_3m - spot
    if spot > vix_3m:
        regime = "backwardation"
    elif spot < 13 and contango > 4:
        regime = "extreme_low"
    elif spot > 28:
        regime = "extreme_high"
    elif 14 <= spot <= 22 and contango > 2:
        regime = "normal_contango"
    else:
        regime = "transitional"
    return regime, contango, vix_3m


def pick_action(regime: str, ts: dict, expiry_iso: str | None, dte: int | None,
               futures_anchor: float, target_dte: int) -> dict:
    """Return action dict with what to execute on RH today."""
    spot = ts.get("vix_spot")
    contango = (ts.get("vix_3m") or 0) - (spot or 0)

    if expiry_iso is None:
        return {"signal": "WAIT", "reason": "no VIX expiries available from data feed",
                "trade": None}

    if regime == "backwardation":
        return {
            "signal": "WAIT",
            "reason": (f"backwardation: spot {spot:.2f} > VIX3M {ts.get('vix_3m'):.2f}. "
                       "vol regime in stress; do not initiate new vol trades."),
            "trade": None,
        }

    if regime == "extreme_low":
        # Long VIX call as crash hedge. Strike near futures_anchor (slightly OTM).
        target_strike = round(futures_anchor + 1)
        chain = fetch_vix_chain(expiry_iso, "C")
        leg = find_strike(chain, target_strike)
        return {
            "signal": "HEDGE_LONG_CALL",
            "reason": (f"spot VIX {spot:.2f} < 13 + contango {contango:+.2f} > 4. "
                       "complacent regime; crash insurance is cheap. expected loss "
                       "in calm quarters; rare payout in vol shock."),
            "trade": {
                "action": "BUY_TO_OPEN",
                "underlying": "VIX",
                "right": "C",
                "expiry": expiry_iso,
                "dte": dte,
                "target_strike": target_strike,
                "nearest_listed_strike": leg["strike"] if leg else None,
                "premium_bid_ask_mid": leg["mid"] if leg else None,
                "size_guidance": "0.5-1% of equity, treat premium as fully at risk",
                "exit": "let expire OR sell on vol spike at +200-500%",
            },
        }

    if regime == "extreme_high":
        # Fade vol via short call SPREAD (not naked)
        short_strike = round(futures_anchor)
        long_strike = short_strike + 5
        chain = fetch_vix_chain(expiry_iso, "C")
        short_leg = find_strike(chain, short_strike)
        long_leg = find_strike(chain, long_strike)
        return {
            "signal": "FADE_VOL_SHORT_CALL_SPREAD",
            "reason": (f"spot VIX {spot:.2f} > 28: extreme vol regime. mean-revert "
                       "play. selling call spread caps risk if vol spikes further."),
            "trade": {
                "action": "SELL_CALL_SPREAD (short lower, long upper)",
                "underlying": "VIX",
                "right": "C",
                "expiry": expiry_iso,
                "dte": dte,
                "short_strike": short_leg["strike"] if short_leg else short_strike,
                "long_strike": long_leg["strike"] if long_leg else long_strike,
                "credit_estimate": (round(short_leg["mid"] - long_leg["mid"], 2)
                                   if (short_leg and long_leg) else None),
                "max_risk_per_spread": ((long_leg["strike"] - short_leg["strike"]) * 100
                                       if (short_leg and long_leg) else None),
                "size_guidance": "max 1 spread per $5k equity (max risk < 1% per spread)",
                "exit": "close at 50% of max profit OR roll if vol drops further",
            },
        }

    if regime == "normal_contango":
        # Sell put spread to harvest contango
        short_strike = max(round(spot) - 1, 12)  # 1-2 below spot
        long_strike = short_strike - 2
        chain = fetch_vix_chain(expiry_iso, "P")
        short_leg = find_strike(chain, short_strike)
        long_leg = find_strike(chain, long_strike)
        return {
            "signal": "SHORT_PUT_SPREAD",
            "reason": (f"spot VIX {spot:.2f} in 14-22 + contango {contango:+.2f}: "
                       "normal calm regime. structural contango = roll-down tailwind. "
                       "spread caps loss if vol spikes."),
            "trade": {
                "action": "SELL_PUT_SPREAD (short higher, long lower)",
                "underlying": "VIX",
                "right": "P",
                "expiry": expiry_iso,
                "dte": dte,
                "short_strike": short_leg["strike"] if short_leg else short_strike,
                "long_strike": long_leg["strike"] if long_leg else long_strike,
                "credit_estimate": (round(short_leg["mid"] - long_leg["mid"], 2)
                                   if (short_leg and long_leg) else None),
                "max_risk_per_spread": ((short_leg["strike"] - long_leg["strike"]) * 100
                                       if (short_leg and long_leg) else None),
                "size_guidance": "max 1 spread per $5k equity (max risk < 1% per spread)",
                "exit": "close at 50% of max profit OR within 7 DTE",
            },
        }

    return {
        "signal": "WAIT",
        "reason": (f"transitional regime (spot {spot:.2f}, contango {contango:+.2f}). "
                   "no clear edge: vol not extreme low for hedge, not extreme high for "
                   "fade, not solidly in normal contango band for put spread."),
        "trade": None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-dte", type=int, default=45,
                    help="approximate target DTE for the suggested expiry")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    ts = fetch_term_structure()
    if ts is None or ts.get("vix_spot") is None:
        print("[ERR] failed to pull VIX term structure", file=sys.stderr)
        return 1

    expiry_iso, actual_dte = pick_expiry(args.target_dte)
    regime, contango, futures_anchor = classify_regime(ts)
    action = pick_action(regime, ts, expiry_iso, actual_dte, futures_anchor, args.target_dte)

    today = date.today()
    headline_bits = [f"VIX {ts['vix_spot']:.2f} | term {regime}",
                     f"-> {action['signal']}"]
    headline = " ".join(headline_bits)

    flags = [f"vix_{action['signal'].lower()}"]
    actions_list = []
    if action.get("trade"):
        actions_list.append({
            "kind": "vix_play",
            "signal": action["signal"],
            **action["trade"],
        })

    out = {
        "step": "vix_check",
        "ok": True,
        "headline": headline,
        "data": {
            "as_of_date": today.isoformat(),
            "term_structure": ts,
            "regime": regime,
            "contango_pp": round(contango, 2),
            "futures_anchor_proxy": round(futures_anchor, 2),
            "selected_expiry": expiry_iso,
            "selected_dte": actual_dte,
            "signal": action["signal"],
            "reason": action["reason"],
            "trade": action.get("trade"),
        },
        "flags": flags,
        "actions": actions_list,
    }

    if args.json:
        print(json.dumps(out, default=str))
        return 0

    print(f"=== VIX check  (as of {today}) ===")
    print(f"VIX9D:   {ts.get('vix_9d')}")
    print(f"VIX:     {ts.get('vix_spot')}   (spot)")
    print(f"VIX3M:   {ts.get('vix_3m')}")
    print(f"VIX6M:   {ts.get('vix_6m')}")
    print(f"VVIX:    {ts.get('vvix')}   (vol of vol)")
    print(f"\nRegime:           {regime}   (contango: {contango:+.2f})")
    print(f"Futures anchor:   {futures_anchor:.2f}  (proxy for ~{args.target_dte} DTE strike pricing)")
    print(f"Selected expiry:  {expiry_iso}  ({actual_dte} DTE)")
    print(f"\nSIGNAL:   {action['signal']}")
    print(f"WHY:      {action['reason']}")
    if action.get("trade"):
        print(f"\nTRADE on RH:")
        for k, v in action["trade"].items():
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
