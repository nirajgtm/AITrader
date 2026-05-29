#!/usr/bin/env python3
"""leap_check.py -- daily LEAP entry signal for the Anchor LEAP DCA strategy.

Strategy spec: knowledge/strategies/leap_long.md.
Backtest: state/backtest/leap_SPY_2019-01-01_2024-12-31.json.

What this emits each morning:
  ENTER  -- today is a valid entry day per the strategy rules.
            Reasons: monthly DCA day, or VIX < vix_low_entry, or RSI < rsi_pullback.
  SKIP   -- VIX too high (premiums punitive); wait.
  HOLD   -- no entry trigger today; existing positions unaffected.

Output (JSON for brief consumption, plain text otherwise):
  {
    "step": "leap_check",
    "ok": true,
    "headline": "SPY LEAP: ENTER (monthly DCA + VIX 16.4)",
    "data": {
      "underlying": "SPY",
      "spot": 720.65,
      "vix": 16.4,
      "rsi14": 58.2,
      "signal": "ENTER",
      "reasons": ["monthly_dca", "vix_low"],
      "suggested_strike": 654.27,    # 0.80 delta strike
      "suggested_dte": 540,
      "suggested_premium": 88.10,    # BS estimate
      "suggested_size_pct": 5.0
    },
    "flags": ["leap_enter_spy"],
    "actions": [{"kind":"leap_entry","ticker":"SPY","reason":"monthly_dca+vix_low"}]
  }

Usage:
  leap_check.py --underlying SPY [--json]
  leap_check.py --underlying QQQ [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from _bs import bs_call_price, strike_for_delta  # noqa: E402

IV_SCALE_BY_UNDERLYING = {
    "SPY": 0.95, "QQQ": 1.15, "IWM": 1.30,
    "TQQQ": 3.0, "UPRO": 3.0,
}


def _pull_recent(ticker: str, days_back: int = 60):
    import yfinance as yf
    end = date.today()
    start = end - timedelta(days=days_back)
    df = yf.Ticker(ticker).history(
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
    )
    if df.empty:
        return None
    return df


def _rsi14(closes):
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))


def _is_first_trading_day_of_month(today_idx, idx_dates) -> bool:
    if not idx_dates or len(idx_dates) < 2:
        return False
    today = idx_dates[-1]
    yesterday = idx_dates[-2]
    return today.month != yesterday.month


def compute_signal(underlying: str, args: argparse.Namespace) -> dict:
    df_under = _pull_recent(underlying, days_back=60)
    df_vix = _pull_recent("^VIX", days_back=60)
    if df_under is None or df_vix is None:
        return {"ok": False, "errors": ["yfinance returned empty"]}

    spot = float(df_under["Close"].iloc[-1])
    vix = float(df_vix["Close"].iloc[-1])
    rsi_series = _rsi14(df_under["Close"])
    rsi = float(rsi_series.iloc[-1]) if len(rsi_series) > 0 else None

    idx_dates = [ts.date() for ts in df_under.index]
    monthly_dca_today = _is_first_trading_day_of_month(len(idx_dates) - 1, idx_dates)

    iv_mult = args.iv_mult or IV_SCALE_BY_UNDERLYING.get(underlying.upper(), 1.0)
    iv = (vix / 100.0) * iv_mult

    reasons = []
    skip_reasons = []
    signal = "HOLD"
    is_pullback = rsi is not None and rsi < args.rsi_pullback
    is_extended = (args.rsi_extended_skip is not None and rsi is not None
                  and rsi > args.rsi_extended_skip)

    if vix > args.vix_max:
        signal = "SKIP"
        reasons.append(f"vix_too_high ({vix:.1f} > {args.vix_max})")
    else:
        # Pullback always wins -- the dip-buy case
        if is_pullback:
            reasons.append(f"rsi_pullback ({rsi:.1f} < {args.rsi_pullback})")
        else:
            if monthly_dca_today:
                reasons.append("monthly_dca")
            if vix < args.vix_low_entry:
                reasons.append(f"vix_low ({vix:.1f} < {args.vix_low_entry})")
            # Extended-skip filter: gates monthly DCA + vol-cheap, never pullback
            if reasons and is_extended:
                skip_reasons.append(f"rsi_extended ({rsi:.1f} > {args.rsi_extended_skip})")
                signal = "WAIT"
        if signal != "WAIT":
            if reasons:
                signal = "ENTER"
            else:
                signal = "HOLD"
                reasons.append("no_trigger")

    suggested = None
    if signal == "ENTER":
        ttm = args.dte_days / 365.0
        strike = strike_for_delta(spot, ttm, args.delta_target, iv, args.rate)
        premium = bs_call_price(spot, strike, ttm, iv, args.rate)
        # Deterministic expiry date computation -- never let the consumer
        # mentally convert "DTE days" to a calendar month. Misreads cost money.
        today_date = date.today()
        target_expiry = today_date + timedelta(days=args.dte_days)
        suggested = {
            "strike": round(strike, 2),
            "dte_days": args.dte_days,
            "as_of_date": today_date.isoformat(),
            "target_expiry_date": target_expiry.isoformat(),
            "expiry_note": "approximate; pick the listed expiration nearest to target_expiry_date",
            "delta_target": args.delta_target,
            "premium_estimate": round(premium, 2),
            "iv_used": round(iv, 4),
            "size_pct": args.size_pct,
        }

    headline_bits = [f"{underlying} LEAP: {signal}"]
    if signal == "WAIT" and skip_reasons:
        headline_bits.append("(triggers: " + " + ".join(reasons) + " | blocked: "
                            + " + ".join(skip_reasons) + ")")
    elif reasons:
        headline_bits.append("(" + " + ".join(reasons) + ")")
    headline = " ".join(headline_bits)

    flags = []
    if signal == "ENTER":
        flags.append(f"leap_enter_{underlying.lower()}")
    elif signal == "SKIP":
        flags.append(f"leap_skip_{underlying.lower()}")
    elif signal == "WAIT":
        flags.append(f"leap_wait_{underlying.lower()}")

    actions = []
    if signal == "ENTER":
        actions.append({
            "kind": "leap_entry",
            "ticker": underlying,
            "reason": " + ".join(reasons),
            "strike": suggested["strike"],
            "dte_days": suggested["dte_days"],
            "premium_estimate": suggested["premium_estimate"],
            "size_pct": args.size_pct,
        })

    return {
        "step": "leap_check",
        "ok": True,
        "headline": headline,
        "data": {
            "underlying": underlying,
            "spot": round(spot, 2),
            "vix": round(vix, 2),
            "rsi14": round(rsi, 2) if rsi is not None else None,
            "monthly_dca_today": monthly_dca_today,
            "signal": signal,
            "reasons": reasons,
            "skip_reasons": skip_reasons,
            "suggested_entry": suggested,
            "iv_mult": iv_mult,
        },
        "flags": flags,
        "actions": actions,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlying", default="SPY")
    ap.add_argument("--delta-target", type=float, default=0.80)
    ap.add_argument("--dte-days", type=int, default=540)
    ap.add_argument("--size-pct", type=float, default=5.0)
    ap.add_argument("--vix-max", type=float, default=30.0)
    ap.add_argument("--vix-low-entry", type=float, default=18.0)
    ap.add_argument("--rsi-pullback", type=float, default=40.0)
    ap.add_argument("--rsi-extended-skip", type=float, default=70.0,
                    help="if RSI(14) above this, downgrade ENTER to WAIT for "
                         "monthly DCA / vol-cheap triggers (pullback still fires). "
                         "Default 70 = textbook overbought.")
    ap.add_argument("--rate", type=float, default=0.045)
    ap.add_argument("--iv-mult", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    out = compute_signal(args.underlying, args)
    if args.json:
        print(json.dumps(out, default=str))
        return 0 if out.get("ok") else 1

    d = out.get("data", {})
    print(f"=== LEAP check ({d.get('underlying')}) ===")
    print(f"Spot:           {d.get('spot')}")
    print(f"VIX:            {d.get('vix')}   (skip if > {args.vix_max}, low-entry if < {args.vix_low_entry})")
    print(f"RSI(14):        {d.get('rsi14')}   (pullback entry if < {args.rsi_pullback})")
    print(f"Monthly DCA day: {d.get('monthly_dca_today')}")
    print(f"\nSIGNAL:         {d.get('signal')}")
    print(f"Reasons:        {', '.join(d.get('reasons', []))}")
    if d.get("suggested_entry"):
        s = d["suggested_entry"]
        print(f"\nSuggested entry:")
        print(f"  as of:   {s.get('as_of_date')}")
        print(f"  strike:  {s['strike']}  (delta target {s['delta_target']})")
        print(f"  DTE:     {s['dte_days']} days  ->  target expiry {s.get('target_expiry_date')}")
        print(f"  expiry:  {s.get('expiry_note', '')}")
        print(f"  premium: ~${s['premium_estimate']:.2f} per contract (BS estimate, IV={s['iv_used']:.3f})")
        print(f"  size:    {s['size_pct']}% of equity")
    return 0


if __name__ == "__main__":
    sys.exit(main())
