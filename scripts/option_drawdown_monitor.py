#!/usr/bin/env python3
"""option_drawdown_monitor.py -- flag user option positions that have drawn
down hard, classify the cause, and suggest action.

Why: an 80%+ drawdown on an option means very different things depending on
whether the underlying crashed (delta-driven), time ran out (theta-driven),
or IV crushed (vega-driven). Each cause has a different right answer for
"is this a buy opportunity?" The monitor surfaces the case and gives a
deterministic verdict so the user can decide without re-deriving the math.

For each user option position (state/portfolio.json user_positions[] where
kind='option' and option_symbol is set):
  1. Parse OCC option_symbol to extract underlying, expiry, strike, right.
  2. Pull current underlying price (yfinance).
  3. Pull current option chain mid (yfinance); fallback to Black-Scholes
     synthesis from current spot + VIX-derived IV.
  4. Compute drawdown vs entry premium.
  5. If drawdown > threshold (default 70%), categorize cause:
       delta_driven: underlying dropped > 15% from entry
       theta_driven: DTE < 60 days remaining
       iv_driven:    neither of the above, premium fell anyway
       mixed:        more than one factor
  6. Emit suggestion based on cause + RSI + VIX + DTE remaining.

Suggestions:
  STRONG_BUY_NEW_CONTRACT  -- delta-driven + DTE > 90 + RSI < 40 (deep dip)
  CONSIDER_NEW_CONTRACT    -- delta-driven + DTE > 90 + RSI 40-70
  NO_REBUY_THETA           -- theta-driven OR DTE < 60 (decay you can't recover)
  HOLD_AND_RESEARCH        -- iv_driven or mixed; needs context
  HOLD                     -- drawdown below threshold

Critical rule (per memory feedback_no_hallucinated_dates_prices.md):
  Every price/date in the output is computed from a tool call this session,
  never recalled. Underlying prices come from yfinance; current option
  premium is yfinance chain mid (preferred) or BS estimate (fallback,
  flagged as such).

Usage:
  option_drawdown_monitor.py [--threshold 0.70] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from _bs import bs_call_price  # noqa: E402

PORTFOLIO_PATH = ROOT / "state" / "portfolio.json"

OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


def parse_occ(symbol: str) -> dict | None:
    """Parse OCC option symbol like 'ORCL260821C00300000'.

    Returns dict with ticker, expiry (date), right ('C'/'P'), strike (float)
    or None if not parseable.
    """
    if not symbol:
        return None
    m = OCC_RE.match(symbol.strip().upper())
    if not m:
        return None
    ticker, ymd, right, strike_int = m.groups()
    try:
        expiry = datetime.strptime(ymd, "%y%m%d").date()
    except ValueError:
        return None
    strike = int(strike_int) / 1000
    return {"ticker": ticker, "expiry": expiry, "right": right, "strike": strike}


def get_current_underlying_price(ticker: str) -> float | None:
    # public.com first (real-time last), yfinance fallback.
    try:
        import publicdotcom_api as pub
        q = pub.get_quote(ticker) or {}
        s = pub._to_float(q.get("last")) or pub._to_float(q.get("previousClose"))
        if s:
            return s
    except Exception:
        pass
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        h = yf.Ticker(ticker).history(period="2d")
        if h.empty:
            return None
        return float(h["Close"].iloc[-1])
    except Exception:
        return None


def get_current_vix() -> float | None:
    try:
        import yfinance as yf
        h = yf.Ticker("^VIX").history(period="2d")
        if h.empty:
            return None
        return float(h["Close"].iloc[-1])
    except Exception:
        return None


def get_current_rsi14(ticker: str) -> float | None:
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="60d")
        if h.empty or len(h) < 15:
            return None
        delta = h["Close"].diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-9)
        rsi = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1])
    except Exception:
        return None


def _closest_listed_expiry(expiries_iso: list[str], expiry: date, max_days: int = 5) -> str | None:
    """Pick the listed expiration matching `expiry` exactly, or the nearest one
    within `max_days`. Returns the ISO string or None."""
    target = expiry.isoformat()
    if target in expiries_iso:
        return target
    best = None
    best_diff = None
    for e in expiries_iso:
        try:
            e_date = date.fromisoformat(e)
        except ValueError:
            continue
        diff = abs((e_date - expiry).days)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best = e
    if best is None or best_diff > max_days:
        return None
    return best


def _fetch_chain_premium_public(ticker: str, expiry: date, strike: float, right: str) -> tuple[float | None, str]:
    """public.com path for fetch_chain_premium. Returns (premium, source) with
    source 'public_chain_mid' / 'public_chain_last' / 'unavailable'. Never
    raises; on any failure returns (None, 'unavailable') so the caller falls
    back to yfinance."""
    try:
        import publicdotcom_api as pub
        expiries_iso = pub.get_option_expirations(ticker)
        if not expiries_iso:
            return None, "unavailable"
        target = _closest_listed_expiry(expiries_iso, expiry)
        if target is None:
            return None, "unavailable"
        chain = pub.get_option_chain(ticker, target)
        contracts = chain.get("calls" if right == "C" else "puts", [])
        rows = [pub.parse_option_contract(c) for c in contracts]
        rows = [r for r in rows if r and r.get("strike") is not None]
        if not rows:
            return None, "unavailable"
        # Exact strike, else nearest.
        exact = [r for r in rows if abs(r["strike"] - strike) < 1e-6]
        row = exact[0] if exact else min(rows, key=lambda r: abs(r["strike"] - strike))
        bid = row.get("bid") or 0
        ask = row.get("ask") or 0
        last = row.get("last") or 0
        mid = row.get("mid")
        if bid > 0 and ask > 0:
            return (bid + ask) / 2, "public_chain_mid"
        if mid and mid > 0:
            return mid, "public_chain_mid"
        if last > 0:
            return last, "public_chain_last"
        return None, "unavailable"
    except Exception:
        return None, "unavailable"


def fetch_chain_premium(ticker: str, expiry: date, strike: float, right: str) -> tuple[float | None, str]:
    """Pull current option mid for the specific strike. Returns (premium, source).

    Tries public.com (real-time broker quotes) first, then yfinance.

    Source values:
      'public_chain_mid' / 'public_chain_last' -- live public.com quote
      'chain_mid'  -- live yfinance bid/ask mid
      'chain_last' -- yfinance lastPrice (fallback when bid/ask absent)
      'unavailable' -- both sources failed; caller should fall back to BS
    """
    premium, source = _fetch_chain_premium_public(ticker, expiry, strike, right)
    if premium is not None:
        return premium, source
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        expiries_iso = list(t.options or [])
        if not expiries_iso:
            return None, "unavailable"
        target = _closest_listed_expiry(expiries_iso, expiry)
        if target is None:
            return None, "unavailable"
        chain = t.option_chain(target)
        df = chain.calls if right == "C" else chain.puts
        # Find exact strike
        row = df[df["strike"] == strike]
        if row.empty:
            # Find nearest strike
            nearest_idx = (df["strike"] - strike).abs().idxmin()
            row = df.loc[[nearest_idx]]
        bid = float(row["bid"].iloc[0]) if "bid" in row else 0
        ask = float(row["ask"].iloc[0]) if "ask" in row else 0
        last = float(row["lastPrice"].iloc[0]) if "lastPrice" in row else 0
        if bid > 0 and ask > 0:
            return (bid + ask) / 2, "chain_mid"
        if last > 0:
            return last, "chain_last"
        return None, "unavailable"
    except Exception:
        return None, "unavailable"


def bs_fallback_premium(spot: float, strike: float, ttm: float, vix: float, right: str) -> float:
    """Fallback: BS price from current spot and VIX-derived IV.

    Only call for calls (we hold long calls in this strategy). For puts the
    BS formula differs; if put support is needed, extend _bs.py.
    """
    if right != "C":
        return 0.0
    iv = (vix / 100.0) if vix else 0.20
    return bs_call_price(spot, strike, ttm, iv, rate=0.045)


def categorize_cause(entry: float, current: float, entry_spot: float | None,
                    current_spot: float | None, dte: int) -> tuple[str, list]:
    drawdown = (current - entry) / entry * 100  # negative when we lost
    causes = []
    underlying_drop_pct = None
    if entry_spot and current_spot:
        underlying_drop_pct = (current_spot - entry_spot) / entry_spot * 100
        if underlying_drop_pct < -15:
            causes.append("delta_driven")
    if dte < 60:
        causes.append("theta_driven")
    if not causes:
        # Premium fell but no obvious driver -- flag as iv-driven
        causes.append("iv_driven")
    if len(causes) > 1:
        return "mixed", causes
    return causes[0], causes


def make_suggestion(cause: str, dte: int, rsi: float | None,
                   underlying_drop_pct: float | None) -> tuple[str, list]:
    reasons = []
    if "theta_driven" in cause or dte < 60:
        return "NO_REBUY_THETA", [
            f"DTE {dte} < 60: most extrinsic value gone, decay locked in",
            "do NOT average down on this contract; theta is unrecoverable",
        ]
    if cause == "delta_driven":
        if rsi is not None and rsi < 40:
            return "STRONG_BUY_NEW_CONTRACT", [
                f"underlying dropped {underlying_drop_pct:.1f}%",
                f"RSI {rsi:.1f} < 40 = pullback signal",
                f"DTE {dte} > 60 leaves room for recovery",
                "buy NEW LEAP at lower spot, do NOT average the existing one",
            ]
        if rsi is not None and rsi < 70:
            return "CONSIDER_NEW_CONTRACT", [
                f"underlying dropped {underlying_drop_pct:.1f}%",
                f"RSI {rsi:.1f} between 40-70: setup but no clear trigger",
                "wait for RSI < 40 OR vol-cheap entry, then buy NEW contract",
            ]
        return "HOLD_AND_RESEARCH", [
            f"underlying dropped {underlying_drop_pct:.1f}%",
            f"RSI {rsi:.1f if rsi else '?'}: still extended, no buy signal yet",
            "watch for RSI < 70 before considering re-entry",
        ]
    if cause == "iv_driven":
        return "HOLD_AND_RESEARCH", [
            "premium fell without underlying dropping",
            "likely IV crush post-event (earnings, FOMC, etc)",
            "research the catalyst; if thesis still intact, hold; do NOT add",
        ]
    return "HOLD", []


def evaluate_position(pos: dict, today: date, threshold: float) -> dict | None:
    sym = pos.get("option_symbol")
    parsed = parse_occ(sym) if sym else None
    if not parsed:
        return None
    ticker = parsed["ticker"]
    expiry = parsed["expiry"]
    strike = parsed["strike"]
    right = parsed["right"]
    entry = float(pos.get("entry") or 0)
    if entry <= 0:
        return None

    current_spot = get_current_underlying_price(ticker)
    if current_spot is None:
        return None
    dte = max(0, (expiry - today).days)
    ttm = dte / 365.0

    current_premium, source = fetch_chain_premium(ticker, expiry, strike, right)
    if current_premium is None:
        vix = get_current_vix() or 20.0
        current_premium = bs_fallback_premium(current_spot, strike, ttm, vix, right)
        source = "bs_estimate"

    drawdown_pct = (current_premium - entry) / entry * 100
    if drawdown_pct >= -threshold * 100:
        return {
            "ticker": ticker, "option_symbol": sym,
            "entry": entry, "current": round(current_premium, 4),
            "drawdown_pct": round(drawdown_pct, 2),
            "source": source, "dte": dte,
            "current_spot": round(current_spot, 2),
            "below_threshold": False,
        }

    entry_spot = pos.get("entry_spot")  # may be missing for older entries
    rsi = get_current_rsi14(ticker)
    underlying_drop_pct = None
    if entry_spot and current_spot:
        underlying_drop_pct = (current_spot - entry_spot) / entry_spot * 100

    cause, raw_causes = categorize_cause(entry, current_premium, entry_spot,
                                        current_spot, dte)
    suggestion, reasons = make_suggestion(cause, dte, rsi, underlying_drop_pct)
    return {
        "ticker": ticker, "option_symbol": sym,
        "entry_premium": entry, "current_premium": round(current_premium, 4),
        "drawdown_pct": round(drawdown_pct, 2),
        "current_spot": round(current_spot, 2),
        "entry_spot": entry_spot,
        "underlying_drop_pct": round(underlying_drop_pct, 2) if underlying_drop_pct is not None else None,
        "rsi14": round(rsi, 2) if rsi is not None else None,
        "dte": dte,
        "expiry": expiry.isoformat(),
        "strike": strike, "right": right,
        "premium_source": source,
        "cause": cause,
        "all_causes": raw_causes,
        "suggestion": suggestion,
        "reasons": reasons,
        "below_threshold": True,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.70,
                    help="drawdown fraction to flag (default 0.70 = -70%%)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not PORTFOLIO_PATH.exists():
        print("[ERR] portfolio.json missing", file=sys.stderr)
        return 1
    p = json.loads(PORTFOLIO_PATH.read_text())
    ups = p.get("user_positions", [])
    options = [u for u in ups if u.get("kind") == "option" or u.get("option_symbol")]

    today = date.today()
    flagged = []
    all_evaluated = []
    for pos in options:
        result = evaluate_position(pos, today, args.threshold)
        if result is None:
            continue
        all_evaluated.append(result)
        if result.get("below_threshold"):
            flagged.append(result)

    headline = (f"option_drawdown: {len(flagged)} positions below "
                f"-{int(args.threshold*100)}% drawdown of {len(all_evaluated)} evaluated")

    flags = []
    actions = []
    for f in flagged:
        flags.append(f"opt_dd_{f['ticker'].lower()}_{f['suggestion'].lower()}")
        actions.append({
            "kind": "option_drawdown_action",
            "ticker": f["ticker"],
            "option_symbol": f["option_symbol"],
            "drawdown_pct": f["drawdown_pct"],
            "suggestion": f["suggestion"],
            "reasons": f["reasons"],
            "cause": f["cause"],
        })

    out = {
        "step": "option_drawdown_monitor",
        "ok": True,
        "headline": headline,
        "data": {
            "as_of_date": today.isoformat(),
            "threshold_pct": args.threshold * 100,
            "evaluated_count": len(all_evaluated),
            "flagged_count": len(flagged),
            "flagged": flagged,
        },
        "flags": flags,
        "actions": actions,
    }
    if args.json:
        print(json.dumps(out, default=str))
        return 0

    print(f"=== Option drawdown monitor (as of {today}, threshold -{int(args.threshold*100)}%) ===")
    print(f"Evaluated: {len(all_evaluated)} option positions")
    print(f"Flagged:   {len(flagged)}\n")
    if not flagged:
        print("No positions below threshold. Healthy.")
        return 0
    for f in flagged:
        print(f"--- {f['ticker']} {f['option_symbol']} ---")
        print(f"  entry premium:  ${f['entry_premium']:.2f}")
        print(f"  current:        ${f['current_premium']:.2f}  (source: {f['premium_source']})")
        print(f"  drawdown:       {f['drawdown_pct']:+.2f}%")
        print(f"  underlying:     spot ${f['current_spot']} "
              + (f"(was ${f['entry_spot']}, {f['underlying_drop_pct']:+.2f}%)" if f.get('underlying_drop_pct') is not None else "(entry spot unknown)"))
        print(f"  DTE:            {f['dte']} days  (expiry {f['expiry']})")
        print(f"  RSI(14):        {f['rsi14']}")
        print(f"  cause:          {f['cause']} (causes: {f['all_causes']})")
        print(f"  SUGGESTION:     {f['suggestion']}")
        for r in f["reasons"]:
            print(f"    - {r}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
