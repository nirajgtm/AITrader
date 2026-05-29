#!/usr/bin/env python3
"""Regret ledger — log every candidate the brief considered and rejected,
then mark each one's hypothetical outcome at T+5 and T+20 trading days.

Why: the brief publishes "no trade" days frequently when FOMO + earnings
gates fire. Without an outcome trail, we can't tell whether the gates are
saving us from losses or robbing us of gains. The regret ledger is the
audit data that lets a Friday review answer "would the rejected ideas have
paid?" and feeds the constitution amendment protocol.

Storage: state/regret_ledger.jsonl, append-only JSONL, one event per line.
  - LOG:    rejected candidate (ticker, strategy, reason, hypothetical entry/stop/target)
  - REVIEW: outcome at T+5 / T+20 (computed from yfinance closes vs trigger logic)

Usage:
  regret.py log --ticker XXX --strategy NAME --reason FOMO_INDEX --entry E \\
                --stop S --target T --horizon D --thesis "..."
                [--correlation-class long_tech] [--regime "spy_rsi_80_extended"]
  regret.py from-digest --digest PATH    # auto-log PEAD candidates from brief digest
  regret.py review [--days 5,20] [--limit N]   # mark unreviewed entries
  regret.py summary [--window 30]              # aggregate by reason and strategy
  regret.py list [--last N] [--reason X]
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from _common import STATE_DIR, fmt_usd, now_iso, today_str

REGRET_LEDGER_PATH = STATE_DIR / "regret_ledger.jsonl"

VALID_REASONS = {
    "FOMO_INDEX",          # SPY > 20MA + 2ATR, hard block per correlation class
    "FOMO_TICKER",         # ticker itself > 20MA + 2ATR
    "EARNINGS_BLACKOUT",   # ER inside trade horizon, no defined-risk fit
    "EARNINGS_SENTIMENT",  # defined-risk possible but sentiment alignment < threshold
    "LIQUIDITY_FLOOR",     # ADV or OI below floor
    "NO_SETUP",            # scan surfaced no qualifying entry condition today
    "STRATEGY_COOLDOWN",   # strategy in cooldown after consecutive misses
    "BREADTH",             # narrow tape, system risk-off
    "OTHER",               # free-form, requires note in thesis
}


def _new_id() -> str:
    return "r_" + uuid.uuid4().hex[:6]


def _append(entry: dict) -> None:
    entry.setdefault("ts", now_iso())
    REGRET_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with REGRET_LEDGER_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _read() -> list[dict]:
    if not REGRET_LEDGER_PATH.exists():
        return []
    out = []
    with REGRET_LEDGER_PATH.open() as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def cmd_log(args: argparse.Namespace) -> int:
    if args.reason not in VALID_REASONS:
        print(f"[ERR] reason must be one of {sorted(VALID_REASONS)}", file=sys.stderr)
        return 1
    rid = _new_id()
    entry = {
        "id": rid,
        "kind": "LOG",
        "logged_at": today_str(),
        "ticker": args.ticker.upper(),
        "strategy": args.strategy,
        "reason": args.reason,
        "entry": args.entry,
        "stop": args.stop,
        "target": args.target,
        "horizon": args.horizon,
        "thesis": args.thesis,
        "correlation_class": args.correlation_class,
        "regime": args.regime,
        "side": "LONG" if args.target >= args.stop else "SHORT",
    }
    _append(entry)
    print(f"Logged regret {rid}: {entry['ticker']} {entry['strategy']} "
          f"reason={entry['reason']} side={entry['side']}")
    return 0


def _trading_days_between(start: date, end: date) -> int:
    """Approximate trading-day count using yfinance index (handles holidays)."""
    try:
        import yfinance as yf
    except ImportError:
        return (end - start).days  # fallback, calendar days
    df = yf.Ticker("SPY").history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat())
    return len(df)


def _close_at_or_after(ticker: str, start: date, n_trading_days: int) -> tuple[date | None, float | None]:
    """Return (date, close) of the n-th trading day >= start. None if not yet available."""
    try:
        import yfinance as yf
    except ImportError:
        return None, None
    end_target = start + timedelta(days=n_trading_days * 2 + 10)  # buffer for weekends/holidays
    df = yf.Ticker(ticker).history(start=start.isoformat(), end=end_target.isoformat())
    if len(df) <= n_trading_days:
        return None, None
    row = df.iloc[n_trading_days]
    return row.name.date(), float(row["Close"])


def _hit_target_or_stop(ticker: str, start: date, days: int,
                       entry: float, stop: float, target: float, side: str) -> dict:
    """Walk forward `days` trading days, return what happened first.

    Returns dict with: outcome ("target", "stop", "open"), hit_date,
    hit_price, days_to_hit, mfe (max favorable), mae (max adverse).
    """
    try:
        import yfinance as yf
    except ImportError:
        return {"outcome": "data_unavailable"}
    end_target = start + timedelta(days=days * 2 + 10)
    df = yf.Ticker(ticker).history(start=start.isoformat(), end=end_target.isoformat())
    df = df.iloc[:days]  # cap at the lookback window
    if df.empty:
        return {"outcome": "data_unavailable"}

    # Walk daily. Use intraday high/low if available, else close.
    favorable = adverse = entry
    for ts, row in df.iterrows():
        hi, lo, cl = float(row["High"]), float(row["Low"]), float(row["Close"])
        if side == "LONG":
            favorable = max(favorable, hi)
            adverse = min(adverse, lo)
            if lo <= stop:
                return {"outcome": "stop", "hit_date": ts.date().isoformat(),
                        "hit_price": stop, "days_to_hit": (ts.date() - start).days,
                        "mfe_pct": (favorable - entry) / entry * 100,
                        "mae_pct": (adverse - entry) / entry * 100}
            if hi >= target:
                return {"outcome": "target", "hit_date": ts.date().isoformat(),
                        "hit_price": target, "days_to_hit": (ts.date() - start).days,
                        "mfe_pct": (favorable - entry) / entry * 100,
                        "mae_pct": (adverse - entry) / entry * 100}
        else:  # SHORT (target < stop)
            favorable = min(favorable, lo)
            adverse = max(adverse, hi)
            if hi >= stop:
                return {"outcome": "stop", "hit_date": ts.date().isoformat(),
                        "hit_price": stop, "days_to_hit": (ts.date() - start).days,
                        "mfe_pct": (entry - favorable) / entry * 100,
                        "mae_pct": (entry - adverse) / entry * 100}
            if lo <= target:
                return {"outcome": "target", "hit_date": ts.date().isoformat(),
                        "hit_price": target, "days_to_hit": (ts.date() - start).days,
                        "mfe_pct": (entry - favorable) / entry * 100,
                        "mae_pct": (entry - adverse) / entry * 100}

    last_close = float(df.iloc[-1]["Close"])
    if side == "LONG":
        unreal = (last_close - entry) / entry * 100
        mfe_pct = (favorable - entry) / entry * 100
        mae_pct = (adverse - entry) / entry * 100
    else:
        unreal = (entry - last_close) / entry * 100
        mfe_pct = (entry - favorable) / entry * 100
        mae_pct = (entry - adverse) / entry * 100
    return {"outcome": "open", "last_close": last_close,
            "unrealized_pct": unreal, "mfe_pct": mfe_pct, "mae_pct": mae_pct,
            "days_observed": len(df)}


def _already_reviewed(rid: str, days: int, entries: list[dict]) -> bool:
    return any(e.get("kind") == "REVIEW" and e.get("ref") == rid and e.get("days") == days
               for e in entries)


def cmd_review(args: argparse.Namespace) -> int:
    days_list = [int(x) for x in args.days.split(",")]
    entries = _read()
    logs = [e for e in entries if e.get("kind") == "LOG"]
    reviewed_count = 0
    today = date.today()

    for log in logs[-args.limit:] if args.limit else logs:
        log_date = date.fromisoformat(log["logged_at"])
        for d in days_list:
            elapsed = _trading_days_between(log_date, today)
            if elapsed < d:
                continue
            if _already_reviewed(log["id"], d, entries):
                continue
            outcome = _hit_target_or_stop(
                log["ticker"], log_date, d,
                float(log["entry"]), float(log["stop"]),
                float(log["target"]), log["side"],
            )
            review = {
                "id": _new_id(), "kind": "REVIEW", "ref": log["id"],
                "ticker": log["ticker"], "strategy": log["strategy"],
                "reason": log["reason"], "days": d, **outcome,
            }
            _append(review)
            entries.append(review)
            reviewed_count += 1
            outcome_label = outcome.get("outcome", "?")
            print(f"  reviewed {log['id']} {log['ticker']} T+{d}: {outcome_label}")

    print(f"Reviewed {reviewed_count} regret entries across windows {days_list}")
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    entries = _read()
    cutoff = today_str()
    cutoff_date = date.today() - timedelta(days=args.window)

    logs = [e for e in entries if e.get("kind") == "LOG"
            and date.fromisoformat(e["logged_at"]) >= cutoff_date]
    reviews = [e for e in entries if e.get("kind") == "REVIEW"]

    log_by_id = {l["id"]: l for l in logs}
    relevant_reviews = [r for r in reviews if r.get("ref") in log_by_id]

    by_reason: dict = defaultdict(lambda: {"n": 0, "target": 0, "stop": 0, "open": 0})
    by_strategy: dict = defaultdict(lambda: {"n": 0, "target": 0, "stop": 0, "open": 0})
    top_winners: list = []  # ones that hit target while gates blocked entry

    for r in relevant_reviews:
        if r.get("days") != 20:  # use T+20 as the headline window
            continue
        bucket = r.get("outcome", "open")
        by_reason[r["reason"]]["n"] += 1
        by_reason[r["reason"]][bucket if bucket in ("target", "stop", "open") else "open"] += 1
        by_strategy[r["strategy"]]["n"] += 1
        by_strategy[r["strategy"]][bucket if bucket in ("target", "stop", "open") else "open"] += 1
        if bucket == "target":
            top_winners.append(r)

    print(f"=== Regret summary (last {args.window}d, T+20 window) ===")
    print(f"Total logged: {len(logs)}  Reviewed at T+20: {sum(b['n'] for b in by_reason.values())}\n")

    print("By rejection reason:")
    for reason, b in sorted(by_reason.items(), key=lambda x: -x[1]["target"]):
        hit_rate = b["target"] / b["n"] * 100 if b["n"] else 0
        print(f"  {reason:<22} n={b['n']:<3}  target={b['target']:<3}  stop={b['stop']:<3}  "
              f"open={b['open']:<3}  hit_rate={hit_rate:.0f}%")

    print("\nBy strategy:")
    for strat, b in sorted(by_strategy.items(), key=lambda x: -x[1]["target"]):
        hit_rate = b["target"] / b["n"] * 100 if b["n"] else 0
        print(f"  {strat:<22} n={b['n']:<3}  target={b['target']:<3}  stop={b['stop']:<3}  "
              f"open={b['open']:<3}  hit_rate={hit_rate:.0f}%")

    if top_winners:
        print(f"\nTop rejected winners (would have hit target within 20d):")
        top_winners.sort(key=lambda r: -(r.get("mfe_pct") or 0))
        for r in top_winners[:10]:
            log = log_by_id[r["ref"]]
            print(f"  {r['ticker']:<6} strategy={r['strategy']:<22} reason={r['reason']:<22} "
                  f"hit_in={r.get('days_to_hit', '?')}d mfe={r.get('mfe_pct', 0):+.1f}%")

    return 0


def _spy_in_fomo(regime_data: dict) -> bool:
    """Check the brief's regime block for SPY > 20MA + 2ATR signal.

    The regime.py output structure varies; we look for known flags first, then
    fall back to a numerical check if MA/ATR/Close are exposed.
    """
    if not regime_data:
        return False
    spy = (regime_data.get("tickers") or {}).get("SPY") or {}
    fomo_flag = spy.get("fomo_extended") or spy.get("above_fomo_ceiling")
    if fomo_flag is not None:
        return bool(fomo_flag)
    close = spy.get("close") or spy.get("price")
    ma20 = spy.get("ma20") or spy.get("sma20")
    atr = spy.get("atr") or spy.get("atr14")
    if close and ma20 and atr:
        return close > ma20 + 2 * atr
    return False


def _hypothetical_levels(ticker: str, asof_day: str,
                         gap_threshold_pct: float = 4.5,
                         lookback_days: int = 7) -> dict | None:
    """Find the most recent gap-up >= threshold within lookback_days of asof,
    then compute PEAD-spec entry/stop/target from that day. Returns None if
    yfinance has no data or no qualifying gap.
    """
    try:
        import yfinance as yf
    except ImportError:
        return None
    from datetime import timedelta as _td
    try:
        asof = date.fromisoformat(asof_day)
    except Exception:
        return None
    df = yf.Ticker(ticker).history(
        start=(asof - _td(days=80)).isoformat(),
        end=(asof + _td(days=2)).isoformat(),
    )
    if df.empty or len(df) < 22:
        return None

    high_low = df["High"] - df["Low"]
    atr20_series = high_low.rolling(20).mean()
    closes = df["Close"]
    opens = df["Open"]

    # Walk backward through the lookback window for a qualifying gap
    for offset in range(0, lookback_days):
        i = len(df) - 1 - offset
        if i <= 20:
            return None
        prior_close = float(closes.iloc[i - 1])
        today_open = float(opens.iloc[i])
        if prior_close <= 0:
            continue
        gap_pct = (today_open - prior_close) / prior_close * 100
        if gap_pct < gap_threshold_pct:
            continue
        if i + 1 >= len(df):
            return None
        next_open = float(opens.iloc[i + 1])
        gap_close = float(closes.iloc[i])
        atr20 = float(atr20_series.iloc[i])
        return {
            "entry": round(next_open, 2),
            "stop": round(prior_close, 2),
            "target": round(gap_close + atr20 * 3, 2),
            "atr20": round(atr20, 4),
            "gap_day": df.index[i].date().isoformat(),
            "gap_pct": round(gap_pct, 2),
        }
    return None


def _already_logged_today(ticker: str, strategy: str, entries: list[dict]) -> bool:
    today = today_str()
    return any(e.get("kind") == "LOG" and e.get("ticker") == ticker
               and e.get("strategy") == strategy and e.get("logged_at") == today
               for e in entries)


def cmd_from_digest(args: argparse.Namespace) -> int:
    """Auto-log PEAD candidates from a brief digest JSON.

    Idempotent within a calendar day: skips tickers already logged for the same
    strategy today.
    """
    digest_path = Path(args.digest)
    if not digest_path.exists():
        print(f"[ERR] digest not found: {digest_path}", file=sys.stderr)
        return 1
    with digest_path.open() as f:
        digest = json.load(f)

    existing = _read()
    fomo_active = _spy_in_fomo(digest.get("regime") or digest.get("regime_summary") or {})
    spy_rsi = (digest.get("regime") or {}).get("SPY", {}).get("rsi") if digest.get("regime") else None
    regime_tag = f"spy_fomo={fomo_active} rsi={spy_rsi}" if spy_rsi else f"spy_fomo={fomo_active}"

    candidates = digest.get("candidates", [])
    pead_candidates = [
        c for c in candidates
        if any(s.startswith("pead") for s in c.get("sources", []))
    ]

    if not pead_candidates:
        print("from-digest: no PEAD candidates in digest")
        return 0

    today = today_str()
    logged_count = 0
    for c in pead_candidates:
        ticker = c["ticker"]
        if _already_logged_today(ticker, "pead_long", existing):
            continue
        levels = _hypothetical_levels(ticker, today)
        if levels is None:
            print(f"  skip {ticker}: no qualifying gap in past 7d")
            continue

        if fomo_active:
            reason = "FOMO_INDEX"
        else:
            reason = "NO_SETUP"  # surfaced but no auto-broadcast trigger fired

        sources_str = "+".join(c.get("sources", []))
        thesis = (f"PEAD candidate auto-logged from brief digest. "
                 f"Sources: {sources_str}. Details: {c.get('details', [])}")

        entry = {
            "id": _new_id(),
            "kind": "LOG",
            "logged_at": today,
            "ticker": ticker,
            "strategy": "pead_long",
            "reason": reason,
            "entry": levels["entry"],
            "stop": levels["stop"],
            "target": levels["target"],
            "horizon": 20,
            "thesis": thesis,
            "correlation_class": "unknown",
            "regime": regime_tag,
            "side": "LONG",
            "auto_logged": True,
        }
        _append(entry)
        existing.append(entry)
        logged_count += 1
        print(f"  logged {ticker} reason={reason} entry={levels['entry']} "
              f"stop={levels['stop']} target={levels['target']}")

    print(f"from-digest: logged {logged_count} of {len(pead_candidates)} PEAD candidates")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    entries = _read()
    if args.reason:
        entries = [e for e in entries if e.get("reason") == args.reason]
    if args.last:
        entries = entries[-args.last:]
    for e in entries:
        print(json.dumps(e))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("log", help="record a rejected candidate")
    lg.add_argument("--ticker", required=True)
    lg.add_argument("--strategy", required=True)
    lg.add_argument("--reason", required=True,
                    help=f"one of {sorted(VALID_REASONS)}")
    lg.add_argument("--entry", type=float, required=True,
                    help="hypothetical entry the strategy would have used")
    lg.add_argument("--stop", type=float, required=True)
    lg.add_argument("--target", type=float, required=True)
    lg.add_argument("--horizon", type=int, default=20,
                    help="trade horizon in trading days for outcome window")
    lg.add_argument("--thesis", required=True)
    lg.add_argument("--correlation-class", default="unknown",
                    help="long_tech, long_value, etc per risk.py classes")
    lg.add_argument("--regime", default="",
                    help="short tag describing market state at log time")

    rv = sub.add_parser("review", help="compute outcomes for unreviewed entries")
    rv.add_argument("--days", default="5,20",
                    help="comma-separated trading-day windows to review at")
    rv.add_argument("--limit", type=int, default=None,
                    help="cap number of LOG entries to process this run")

    sm = sub.add_parser("summary", help="aggregate by reason and strategy")
    sm.add_argument("--window", type=int, default=30,
                    help="lookback window in calendar days")

    fd = sub.add_parser("from-digest",
                        help="auto-log PEAD candidates from a brief digest JSON")
    fd.add_argument("--digest", required=True,
                    help="path to brief digest (state/cache/morning_digest_*.json)")

    ls = sub.add_parser("list")
    ls.add_argument("--last", type=int, default=None)
    ls.add_argument("--reason", default=None)

    args = ap.parse_args()
    return {"log": cmd_log, "review": cmd_review,
            "summary": cmd_summary, "list": cmd_list,
            "from-digest": cmd_from_digest}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
