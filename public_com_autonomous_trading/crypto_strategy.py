#!/usr/bin/env python3
"""Crypto strategy for the autonomous trader: the rules, discipline, and the math.

public.com trades crypto 24/7 (verified live 2026-05-22). This is the crypto analog
of the equity guards + surface_candidates logic: it pulls public.com CRYPTO quotes +
daily OHLCV, computes RSI/MA/ATR, and applies a CODIFIED ruleset to (a) surface buy
candidates and (b) review held crypto. Stops are SOFTWARE-only (crypto carries no
resting broker stop), checked each run, so size is kept small (config.crypto) and
stops are ATR-based but capped. Overnight gaps between runs are a known, accepted risk.

RULESET (the discipline):
  Universe: config.crypto.universe (majors + liquid alts).
  Entry, two setups (need >=50 daily bars):
    MOMENTUM    -- last > MA20 > MA50 (uptrend) AND min_rsi_entry <= RSI <= max_rsi_entry.
    MEAN_REVERT -- RSI < 35 (oversold) AND last >= MA50*0.90 (a dip, not a freefall).
  Hard blockers (veto regardless of setup): insufficient data; RSI >= 80 or > max_rsi_entry
    (chase); ATR% > 12/day (too wild for a software stop checked only each run);
    downtrend (last < MA50) unless the mean-revert signal is present.
  Stop:   entry - stop_atr_mult*ATR, floored so risk <= max_stop_loss_pct (so it clears guards.stop_ok).
  Target: entry + 2R where R = entry - stop  -> R:R 2:1, which clears the ~1.2%
          crypto round-trip commission (public.com ~0.6%/side) comfortably.
  Size:   config.crypto.max_position_usd (smaller than equities), settled-cash bound.
  Cap:    config.crypto.max_open_positions concurrent crypto positions.
EVERY entry still re-validates against a fresh quote in run_autonomous before placing;
a setup is a reason to analyze, never an automatic buy.
"""
from __future__ import annotations

import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent
ROOT = DIR.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(DIR))

import publicdotcom_api as pub   # noqa: E402  read-only client (quotes + OHLCV)
import guards                    # noqa: E402


def _sma(vals: list[float], n: int):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _rsi(closes: list[float], n: int = 14):
    """Simple n-period RSI on closes."""
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(-n, 0):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / n, losses / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def _atr(bars: list[dict], n: int = 14):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n if len(trs) >= n else None


def metrics(symbol: str) -> dict | None:
    """Live crypto quote + technicals from public.com CRYPTO bars. None on hard failure."""
    try:
        q = pub.get_quote(symbol, "CRYPTO") or {}
        last = float(q.get("last") or 0) or None
        bars = pub.get_daily_ohlcv(symbol, "QUARTER", "CRYPTO")
    except Exception:
        return None
    if not last:
        return None
    if len(bars) < 30:
        return {"symbol": symbol, "last": last, "bars": len(bars), "rsi14": None,
                "ma20": None, "ma50": None, "atr14": None, "atr_pct": None}
    closes = [b["close"] for b in bars]
    atr = _atr(bars)
    return {
        "symbol": symbol, "last": last, "bars": len(bars),
        "rsi14": _rsi(closes), "ma20": _sma(closes, 20), "ma50": _sma(closes, 50),
        "atr14": atr, "atr_pct": (atr / last * 100) if (atr and last) else None,
        # prior established daily close + RSI excluding today's forming bar, for the
        # mean_revert stabilization gate (last bar is the current-day forming bar).
        "prev_close": closes[-2] if len(closes) >= 2 else None,
        "rsi_prev": _rsi(closes[:-1]),
    }


def plan_trade(m: dict, cfg: dict) -> dict:
    """Apply the ruleset to a metrics dict -> setup, blockers, entry/stop/target/size/score."""
    cc = cfg.get("crypto") or {}
    last, rsi, ma20, ma50 = m.get("last"), m.get("rsi14"), m.get("ma20"), m.get("ma50")
    atr, atr_pct = m.get("atr14"), m.get("atr_pct")
    if not last or rsi is None or ma50 is None or ma20 is None or atr is None:
        return {**m, "setup": None, "blockers": ["insufficient data"], "buyable": False, "score": -1}

    max_rsi = float(cc.get("max_rsi_entry", 72))
    min_rsi = float(cc.get("min_rsi_entry", 40))

    setup = None
    if last > ma20 > ma50 and min_rsi <= rsi <= max_rsi:
        setup = "momentum"
    elif rsi < 35 and last >= ma50 * 0.90:
        setup = "mean_revert"

    blockers = []
    if rsi >= 80 or rsi > max_rsi:
        blockers.append(f"RSI {rsi:.0f} overbought (chase)")
    if atr_pct is not None and atr_pct > 12:
        blockers.append(f"ATR {atr_pct:.0f}%/day too volatile for the software-stop model")
    if last < ma50 and setup != "mean_revert":
        blockers.append("downtrend (below 50-day) with no mean-revert signal")
    if setup is None and not blockers:
        blockers.append("no setup (not trending, not oversold)")

    # mean_revert stabilization gate + artifact sanity: oversold alone is not a buy.
    # Require the dip to be stabilizing (price bouncing OR RSI
    # turning up), and reject the UTC bar-boundary artifact where RSI craters on flat
    # price. The complex-wide deleveraging veto is cross-sectional (evaluate_universe).
    if setup == "mean_revert":
        prev_close, rsi_prev = m.get("prev_close"), m.get("rsi_prev")
        flat_pct = float(cc.get("meanrevert_flat_pct", 0.5))
        rsi_drop = float(cc.get("meanrevert_rsi_drop", 5.0))
        bouncing = prev_close is not None and last > prev_close
        rsi_up = rsi_prev is not None and rsi > rsi_prev
        if not (bouncing or rsi_up):
            blockers.append("no stabilization (not bouncing, RSI not turning up)")
        if prev_close:
            chg_pct = abs(last - prev_close) / prev_close * 100
            if rsi_prev is not None and (rsi_prev - rsi) >= rsi_drop and chg_pct < flat_pct:
                blockers.append(
                    f"suspect signal: RSI {rsi_prev:.0f}->{rsi:.0f} on flat price "
                    f"({chg_pct:.1f}%) -- likely bar-boundary artifact")

    stop_mult = float(cc.get("stop_atr_mult", 1.5))
    max_stop_pct = float(cc.get("max_stop_loss_pct", 15.0))
    stop = round(max(last - stop_mult * atr, last * (1 - max_stop_pct / 100)), 6)
    R = last - stop
    target = round(last + 2 * R, 6)
    score = 1.0 if setup == "momentum" else (0.6 if setup == "mean_revert" else 0.0)
    return {**m, "setup": setup, "blockers": blockers,
            "buyable": bool(setup) and not blockers,
            "entry": round(last, 6), "stop": stop, "target": target,
            "risk_pct": round(R / last * 100, 2) if last else None,
            "size_usd": float(cc.get("max_position_usd", 150)), "score": score}


def evaluate_universe(held: set, cfg: dict | None = None) -> list:
    """Score every coin in the universe (skipping ones already held). Buyable first."""
    cfg = cfg or guards.load_config()
    held = {s.upper() for s in (held or set())}
    out = []
    for sym in (cfg.get("crypto") or {}).get("universe", []):
        if sym.upper() in held:
            continue
        m = metrics(sym)
        if not m:
            continue
        out.append(plan_trade(m, cfg))

    # complex-wide deleveraging veto: when most of the universe is
    # oversold (mean_revert) at once, that is one broad risk-off flush -- a falling
    # knife -- not N independent dips. Veto the whole oversold set. Momentum names are
    # unaffected.
    cc = (cfg.get("crypto") or {})
    frac_thresh = float(cc.get("meanrevert_complex_frac", 0.6))
    min_count = int(cc.get("meanrevert_complex_min", 3))
    n_eval = len(out)
    n_oversold = sum(1 for d in out if d.get("setup") == "mean_revert")
    if n_eval >= min_count and n_oversold >= max(min_count, frac_thresh * n_eval):
        for d in out:
            if d.get("setup") == "mean_revert":
                d.setdefault("blockers", []).append(
                    f"complex-wide deleveraging ({n_oversold}/{n_eval} oversold) -- "
                    "broad risk-off, not independent dips")
                d["buyable"] = False

    out.sort(key=lambda d: (not d.get("buyable"), -(d.get("score") or 0),
                            d.get("atr_pct") or 99))
    return out


def review_position(symbol: str, hyp: dict, cfg: dict | None = None) -> dict:
    """Software-stop / target review for a held crypto position vs a fresh quote.
    SELL on stop-hit or target-reached; otherwise HOLD (thesis re-check in the run)."""
    cfg = cfg or guards.load_config()
    m = metrics(symbol) or {}
    last = m.get("last")
    stop = (hyp or {}).get("stop")
    target = (hyp or {}).get("target")
    if last is None:
        return {"symbol": symbol, "last": None, "stop": stop, "target": target,
                "suggestion": "HOLD", "why": "no quote"}
    sug, why = "HOLD", "within stop/target"
    if stop and last <= stop:
        sug, why = "SELL", f"software stop hit ({last} <= {stop})"
    elif target and last >= target:
        sug, why = "SELL", f"target reached ({last} >= {target})"
    return {"symbol": symbol, "last": last, "stop": stop, "target": target,
            "rsi14": m.get("rsi14"), "ma50": m.get("ma50"), "suggestion": sug, "why": why}


def universe(cfg: dict | None = None) -> set:
    cfg = cfg or guards.load_config()
    return {s.upper() for s in (cfg.get("crypto") or {}).get("universe", [])}


def main() -> int:
    import json
    cfg = guards.load_config()
    print(json.dumps(evaluate_universe(set(), cfg), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
