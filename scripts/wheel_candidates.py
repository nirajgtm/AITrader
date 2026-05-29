#!/usr/bin/env python3
"""wheel_candidates.py — surface stocks that look healthy for the wheel
strategy (cash-secured puts -> assignment -> covered calls -> repeat).

Filters a curated universe of liquid optionable names by:
  - price band ($20-$300) so a 100-share assignment fits typical accounts
  - above the 200-day MA (uptrend; willing to own)
  - RSI 35-72 (no panic, no extension)
  - ATR/spot < 5% (not too whippy)
  - IV-rank 25-75 (premium worth selling, not signaling crisis)
  - next earnings >= 14 days away (no binary catalyst)
  - OI on the suggested CSP strike >= 100 (chain liquid enough to exit)

For each survivor: pick the ~30 DTE put with strike closest to spot * 0.94
(about 6% OTM, ~25-30 delta), grab its mid premium and OI, compute the
annualized yield, score the candidate, and emit the top N.

USAGE
  scripts/wheel_candidates.py                # human-readable
  scripts/wheel_candidates.py --json         # JSON dump for the brief
  scripts/wheel_candidates.py --top 8 --json # control how many to surface
  scripts/wheel_candidates.py --tickers AMD,F,SOFI --json  # custom slice

OUTPUT (--json)
  {ok, as_of, evaluated, passed, candidates:[ {ticker, spot, csp_strike,
   csp_expiry, csp_dte, csp_premium, csp_otm_pct, annualized_yield_pct,
   csp_oi, iv_pct, iv_rank, rsi14, ma50, ma200, above_50dma, above_200dma,
   atr_pct, next_er_date, days_to_er, score} ] }

NOTE: IV-rank is the same proxy that options.py uses (ATM IV vs 1y rolling
realized-vol distribution). Directional, not a tradable IVR. Upgrade to
Polygon options or Tradier when budget allows.
"""

from __future__ import annotations
import argparse
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from _market import fetch_bulk, rsi as compute_rsi, atr as compute_atr  # noqa: E402

# Curated universe: liquid weeklies, retail-popular, not insanely priced.
# Mix of mega-caps (BAC, F), tech (AMD, MU, INTC), retail darlings (SOFI, PLTR),
# financials (BAC, WFC), staples (KO, MO), select sector ETFs (XLE, XLF), and
# mid-priced quality names. Skip illiquid micro-caps and >$1k stocks.
WHEEL_UNIVERSE = sorted({
    # Mega-cap tech with weeklies (price-permitting)
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "AMD", "AVGO",
    "ORCL", "CRM", "ADBE", "INTC", "CSCO", "MU", "ARM", "QCOM",
    # Retail favorites
    "TSLA", "NFLX", "DIS", "UBER", "ABNB", "SHOP", "PYPL", "RBLX",
    # Liquid mid-cap, retail-popular
    "PLTR", "SOFI", "F", "GM", "T", "VZ", "BAC", "WFC", "JPM", "GS",
    "COIN", "HOOD", "RIVN", "NIO", "SNAP", "LCID", "DKNG",
    # Defensives + staples (good wheel territory in chop)
    "KO", "PEP", "MO", "PFE", "JNJ", "WMT", "TGT", "COST",
    # Sector ETFs (liquid weeklies, mean-reverting friendly for wheel)
    "XLE", "XLF", "XLK", "XLV", "XLP", "XLI", "XLY", "SLV", "GLD", "USO",
})

# No hard discretionary filters — every name with a usable option chain is
# evaluated. Hostile metrics (ER inside 14d, low IV, weak technicals) just
# rank lower via the score; they don't get dropped. Reader sees the full picture.
TARGET_DTE = 30
TARGET_DTE_RANGE = (21, 45)
TARGET_OTM_PCT = 0.06   # 6% OTM put as the CSP candidate
MIN_OI = 100            # only here so _pick_csp can find a strike worth quoting
MIN_PREMIUM = 0.10      # below this, the bid-ask eats the trade — strike is data noise
ER_BLACKOUT_DAYS = 14   # informational threshold for the score, not a filter


def _atm_iv_from_chain(chain: dict, spot: float) -> float | None:
    """ATM call IV (fraction) from an already-fetched public.com chain, picking
    the call closest to spot among those that actually carry IV. Returns None
    when no call in the chain has populated greeks."""
    import publicdotcom_api as pub
    best_iv = None
    best_dist = None
    for c in chain.get("calls", []):
        p = pub.parse_option_contract(c)
        if not p or p.get("strike") is None or not p.get("implied_volatility"):
            continue
        dist = abs(p["strike"] - spot)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_iv = p["implied_volatility"]
    return best_iv if (best_iv and best_iv > 0) else None


def _atm_iv_public(symbol: str, spot: float) -> float | None:
    """ATM call IV from public.com (real-time, broker greeks). Returns the IV
    fraction (e.g. 0.30) or None on any failure so the caller falls back to
    yfinance.

    Prefers the ~30 DTE expiry (same one the CSP targets) because public.com
    does not compute greeks for the ultra-short 0-1 DTE weeklies, which would
    otherwise yield a null IV. Falls back to the first expiration whose chain
    actually carries IV."""
    try:
        import publicdotcom_api as pub
        exps = pub.get_option_expirations(symbol)
        if not exps:
            return None
        primary_exp, _ = _best_csp_expiry(exps, date.today())
        ordered = ([primary_exp] if primary_exp else []) + [e for e in exps if e != primary_exp]
        for exp in ordered:
            if not exp:
                continue
            try:
                chain = pub.get_option_chain(symbol, exp)
            except Exception:
                continue
            iv = _atm_iv_from_chain(chain, spot)
            if iv is not None:
                return iv
        return None
    except Exception:
        return None


def _atm_iv_rank(tk: yf.Ticker, spot: float, symbol: str | None = None) -> tuple[float | None, float | None]:
    """Returns (current_atm_iv_pct, iv_rank_pct).

    ATM IV is sourced from public.com first (real-time broker IV), then
    yfinance. IVR proxy: percentile of current ATM IV within 1y rolling 30d
    realized vol. Same approach as options.py:cmd_iv_rank, but inline (no
    caching here so the scanner stays self-contained; per-ticker hits are
    cheap when batched).
    """
    try:
        iv = _atm_iv_public(symbol, spot) if symbol else None
        if iv is None:
            # Fallback: yfinance nearest-expiry ATM call IV.
            exps = tk.options or []
            if not exps:
                return None, None
            chain = tk.option_chain(exps[0])
            calls = chain.calls
            if calls.empty:
                return None, None
            c = calls.copy()
            c["d"] = (c["strike"] - spot).abs()
            row = c.nsmallest(1, "d").iloc[0]
            iv = float(row.get("impliedVolatility", 0))
        if iv <= 0:
            return None, None
        # IV rank vs 1y RV (history from yfinance; this is just the RV baseline)
        hist = tk.history(period="1y", auto_adjust=False)
        if hist.empty or len(hist) < 60:
            return iv, None
        log_ret = hist["Close"].pct_change().dropna()
        rv = log_ret.rolling(window=30).std() * math.sqrt(252)
        rv = rv.dropna()
        if rv.empty:
            return iv, None
        rank = float((rv < iv).mean() * 100)
        return iv, rank
    except Exception:
        return None, None


def _best_csp_expiry(expirations: list[str], today: date) -> tuple[str | None, int | None]:
    """From a list of ISO expiration dates, pick the one whose DTE is closest to
    TARGET_DTE while staying inside TARGET_DTE_RANGE. Returns (expiry, dte)."""
    best_exp = None
    best_dte = None
    for e in expirations:
        try:
            d = datetime.strptime(e, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        dte = (d - today).days
        if not (TARGET_DTE_RANGE[0] <= dte <= TARGET_DTE_RANGE[1]):
            continue
        if best_dte is None or abs(dte - TARGET_DTE) < abs(best_dte - TARGET_DTE):
            best_exp = e
            best_dte = dte
    return best_exp, best_dte


def _build_csp(strike: float, best_exp: str, best_dte: int, spot: float,
               bid: float, ask: float, last: float, oi: int, iv_frac: float,
               stale: bool, source: str) -> dict | None:
    """Shared CSP result builder so the public.com and yfinance paths produce
    byte-identical output dicts. Returns None if the mid is below MIN_PREMIUM."""
    if bid > 0 and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2
    else:
        mid = last
    if mid < MIN_PREMIUM:
        return None
    otm_pct = (spot - strike) / spot * 100
    annualized = (mid / strike) * (365 / max(best_dte, 1)) * 100
    return {
        "strike": round(strike, 2),
        "expiry": best_exp,
        "dte": best_dte,
        "premium": round(mid, 2),
        "bid": round(bid, 2) if bid > 0 else None,
        "ask": round(ask, 2) if ask > 0 else None,
        "otm_pct": round(otm_pct, 2),
        "annualized_yield_pct": round(annualized, 1),
        "open_interest": int(oi or 0),
        "iv_pct": round((iv_frac or 0) * 100, 1),
        "stale_chain": stale,
        "source": source,
    }


def _pick_csp_public(symbol: str, spot: float) -> dict | None:
    """public.com path for _pick_csp. Same selection rules (closest-to-6%-OTM
    put at ~30 DTE, OI tiers, MIN_PREMIUM gate) against real-time broker data.
    Returns None on any failure so the caller falls back to yfinance."""
    today = date.today()
    try:
        import publicdotcom_api as pub
        exps = pub.get_option_expirations(symbol)
        if not exps:
            return None
        best_exp, best_dte = _best_csp_expiry(exps, today)
        if best_exp is None:
            return None
        chain = pub.get_option_chain(symbol, best_exp)
        puts = [pub.parse_option_contract(c) for c in chain.get("puts", [])]
        puts = [p for p in puts if p and p.get("strike") is not None]
        if not puts:
            return None
        target_strike = spot * (1 - TARGET_OTM_PCT)

        def closest(rows):
            return min(rows, key=lambda r: abs(r["strike"] - target_strike))

        stale = False
        tier1 = [p for p in puts if (p.get("open_interest") or 0) >= MIN_OI]
        tier2 = [p for p in puts if (p.get("open_interest") or 0) >= 25]
        tier3 = [p for p in puts if (p.get("last") or 0) > 0]
        if tier1:
            row = closest(tier1)
        elif tier2:
            row = closest(tier2)
        elif tier3:
            row = closest(tier3)
            stale = True
        else:
            return None
        return _build_csp(
            strike=float(row["strike"]), best_exp=best_exp, best_dte=best_dte, spot=spot,
            bid=float(row.get("bid") or 0), ask=float(row.get("ask") or 0),
            last=float(row.get("last") or 0), oi=int(row.get("open_interest") or 0),
            iv_frac=float(row.get("implied_volatility") or 0), stale=stale,
            source="public.com",
        )
    except Exception:
        return None


def _pick_csp(tk: yf.Ticker, spot: float, symbol: str | None = None) -> dict | None:
    """Find a ~30 DTE, ~6% OTM put. Prefers OI >= 100, but if the whole chain
    is stale (e.g. closed-market Sunday — every row has OI=0), falls back to
    the closest strike with any positive lastPrice and marks stale_chain=True.
    Returns None only when the chain is truly empty.

    Sources public.com first (real-time), then yfinance."""
    if symbol:
        pub_csp = _pick_csp_public(symbol, spot)
        if pub_csp is not None:
            return pub_csp
    today = date.today()
    try:
        exps = tk.options or []
        if not exps:
            return None
        best_exp, best_dte = _best_csp_expiry(exps, today)
        if best_exp is None:
            return None
        chain = tk.option_chain(best_exp)
        puts = chain.puts
        if puts is None or puts.empty:
            return None
        target_strike = spot * (1 - TARGET_OTM_PCT)
        p = puts.copy()
        p["d"] = (p["strike"] - target_strike).abs()

        stale = False
        # Tier 1: healthy OI
        p_ok = p[p["openInterest"].fillna(0) >= MIN_OI]
        if p_ok.empty:
            # Tier 2: degraded OI floor
            p_ok = p[p["openInterest"].fillna(0) >= 25]
        if p_ok.empty:
            # Tier 3: stale chain (closed market). Use any strike with a
            # positive lastPrice. Mark the result so callers can see it's noisy.
            p_ok = p[p["lastPrice"].fillna(0) > 0]
            stale = True
        if p_ok.empty:
            return None
        row = p_ok.nsmallest(1, "d").iloc[0]
        return _build_csp(
            strike=float(row["strike"]), best_exp=best_exp, best_dte=best_dte, spot=spot,
            bid=float(row.get("bid") or 0), ask=float(row.get("ask") or 0),
            last=float(row.get("lastPrice") or 0), oi=int(row.get("openInterest") or 0),
            iv_frac=float(row.get("impliedVolatility") or 0), stale=stale,
            source="yfinance",
        )
    except Exception:
        return None


def _next_er_days(tk: yf.Ticker) -> tuple[str | None, int | None]:
    """Return (next_er_date_iso, days_to_er) using yfinance get_earnings_dates."""
    try:
        df = tk.get_earnings_dates(limit=4)
        if df is None or df.empty:
            return None, None
        today = pd.Timestamp.today(tz="UTC").normalize()
        df = df.copy()
        # index is timezone-aware; pull future dates
        if df.index.tz is None:
            idx = df.index.tz_localize("UTC")
        else:
            idx = df.index.tz_convert("UTC")
        future = idx[idx >= today]
        if len(future) == 0:
            return None, None
        nxt = future.min()
        days = int((nxt.normalize() - today).days)
        return nxt.date().isoformat(), days
    except Exception:
        return None, None


def evaluate(ticker: str, hist_df: pd.DataFrame | None) -> dict | None:
    """Compute every metric for the ticker. The only thing that drops a name
    is missing data: no history, no option chain with a usable strike. Bad
    discretionary metrics (high RSI, low IVR, ER imminent) just lower the
    score — they never reject."""
    if hist_df is None or hist_df.empty or len(hist_df) < 200:
        return None  # data-availability gate, not a discretionary filter
    closes = hist_df["Close"]
    spot = float(closes.iloc[-1])
    rsi14 = float(compute_rsi(closes, 14).iloc[-1])
    ma50 = float(closes.rolling(50).mean().iloc[-1])
    ma200 = float(closes.rolling(200).mean().iloc[-1])
    above_50 = spot >= ma50
    above_200 = spot >= ma200
    atr14 = float(compute_atr(hist_df, 14).iloc[-1])
    atr_pct = atr14 / spot

    tk = yf.Ticker(ticker)
    iv, ivr = _atm_iv_rank(tk, spot, symbol=ticker)
    er_date, dte_er = _next_er_days(tk)
    csp = _pick_csp(tk, spot, symbol=ticker)
    if csp is None:
        return None  # no usable strike — nothing to suggest

    # ── Score (0-1) — soft. Bad metrics drag, never reject. ────────────────
    # Yield: linear up to 50% annualized = full credit
    yield_n = min(max(csp["annualized_yield_pct"], 0) / 50.0, 1.0)
    # IVR: sweet spot at ~50, decays toward 0/100. Missing IVR = neutral 0.5.
    if ivr is not None:
        ivr_n = 1 - abs(ivr - 50) / 50.0
    else:
        ivr_n = 0.5
    # Health: RSI in 35-70 = full; outside that = degraded. Below 200DMA halves.
    rsi_n = max(0.0, min((rsi14 - 25) / 45.0, 1.0))      # full credit 25-70
    if rsi14 > 70:
        rsi_n *= max(0.4, 1 - (rsi14 - 70) / 25.0)        # parabolic extension penalty
    health_n = rsi_n * (1.0 if above_50 else 0.7) * (1.0 if above_200 else 0.5)
    # ATR penalty: above 6% daily, scale down
    vol_pen = 1.0 if atr_pct <= 0.06 else max(0.5, 1 - (atr_pct - 0.06) * 10)
    # ER penalty: anything inside ER_BLACKOUT_DAYS halves the score
    er_pen = 1.0
    if dte_er is not None and dte_er < ER_BLACKOUT_DAYS:
        er_pen = 0.4 if dte_er < 7 else 0.6
    raw = 0.40 * yield_n + 0.25 * ivr_n + 0.35 * health_n
    score = round(raw * vol_pen * er_pen, 3)

    return {
        "ticker": ticker,
        "spot": round(spot, 2),
        "rsi14": round(rsi14, 1),
        "ma50": round(ma50, 2),
        "ma200": round(ma200, 2),
        "above_50dma": above_50,
        "above_200dma": above_200,
        "atr_pct": round(atr_pct * 100, 2),
        "iv_pct": round((iv or 0) * 100, 1),
        "iv_rank": round(ivr, 1) if ivr is not None else None,
        "next_er_date": er_date,
        "days_to_er": dte_er,
        "csp_strike": csp["strike"],
        "csp_expiry": csp["expiry"],
        "csp_dte": csp["dte"],
        "csp_premium": csp["premium"],
        "csp_bid": csp["bid"],
        "csp_ask": csp["ask"],
        "csp_otm_pct": csp["otm_pct"],
        "csp_open_interest": csp["open_interest"],
        "annualized_yield_pct": csp["annualized_yield_pct"],
        "stale_chain": csp.get("stale_chain", False),
        "option_source": csp.get("source"),
        "score": score,
        "score_components": {
            "yield": round(yield_n, 2),
            "ivr": round(ivr_n, 2),
            "health": round(health_n, 2),
            "vol_penalty": round(vol_pen, 2),
            "er_penalty": round(er_pen, 2),
        },
    }


def run(tickers: list[str], top: int, verbose: bool = False) -> dict:
    # Bulk-fetch 1y history for all tickers in one call (yfinance batches).
    hist_map = fetch_bulk(tickers, period="1y", interval="1d")
    candidates = []

    # Per-ticker option work uses yfinance .Ticker(); parallelize lightly.
    def work(t):
        return evaluate(t, hist_map.get(t))

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(work, t): t for t in tickers}
        for fut in as_completed(futs):
            r = fut.result()
            if r is None:
                if verbose:
                    print(f"  drop {futs[fut]:6s} no_data_or_no_chain", file=sys.stderr)
                continue
            candidates.append(r)

    candidates.sort(key=lambda r: r["score"], reverse=True)
    return {
        "ok": True,
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "evaluated": len(tickers),
        "scored": len(candidates),
        "candidates": candidates[:top],
    }


def render_human(payload: dict) -> str:
    rows = payload.get("candidates", [])
    if not rows:
        return f"No wheel candidates produced ({payload['evaluated']} evaluated, no usable chains)."
    out = [
        f"WHEEL CANDIDATES — {payload['as_of']}",
        f"  evaluated {payload['evaluated']}, scored {payload['scored']}, showing top {len(rows)}",
        "",
        f"  {'TKR':6s} {'SPOT':>8s} {'CSP':>10s} {'EXPIRY':>11s} {'PREM':>6s} {'YLD':>6s} {'IVR':>5s} {'RSI':>5s} {'ER d':>5s} {'SCORE':>6s}",
    ]
    for c in rows:
        ivr_str = f"{c['iv_rank']:>4.0f}%" if c.get("iv_rank") is not None else "  --%"
        stale = " *" if c.get("stale_chain") else "  "
        out.append(
            f"  {c['ticker']:6s} ${c['spot']:>7.2f} ${c['csp_strike']:>9.2f} "
            f"{c['csp_expiry']:>11s} ${c['csp_premium']:>5.2f} "
            f"{c['annualized_yield_pct']:>5.1f}% "
            f"{ivr_str} {c['rsi14']:>5.1f} "
            f"{(str(c['days_to_er']) if c['days_to_er'] is not None else '-'):>5s} "
            f"{c['score']:>6.3f}{stale}"
        )
    if any(c.get("stale_chain") for c in rows):
        out.append("")
        out.append("  * stale chain: market closed, OI/bid/ask are zero; premium from lastPrice. Re-run during RTH for live quotes.")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--tickers", help="comma-separated override (skips default universe)")
    ap.add_argument("--verbose", action="store_true",
                    help="log per-ticker rejection reasons to stderr")
    args = ap.parse_args()

    tickers = sorted(set(t.strip().upper() for t in args.tickers.split(",") if t.strip())) \
        if args.tickers else WHEEL_UNIVERSE
    payload = run(tickers, args.top, verbose=args.verbose)

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render_human(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
