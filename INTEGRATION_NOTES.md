# Integration notes — regret ledger + benchmark + backtest

Wired up 2026-05-02. Three new tools live in `scripts/`:
- `regret.py` — log rejected candidates, review at T+5/T+20, summarize by reason/strategy
- `benchmark.py` — compare real-book P&L to SPY (or chosen) buy/hold over the same window
- `backtest.py` — replay strategies under constitution gates ON vs OFF

These are standalone tools. The brief.py, morning_broadcast.py, and runbook.py call sites are not yet modified. This file lists where each integration needs to land.

## brief.py

**Add per-rejection logging.** Wherever the brief evaluates a candidate and decides not to broadcast it, call `regret.py log` with the reason. The candidate scan paths to instrument:

- FOMO_INDEX rejection (SPY > 20MA + 2ATR) — currently triggered in the regime layer.
- EARNINGS_BLACKOUT — when a candidate has ER inside its horizon and no defined-risk fit.
- LIQUIDITY_FLOOR — when ADV or OI fails the constitution rule 10 thresholds.
- NO_SETUP — when the scanner returned the ticker but no strategy file matched its conditions.
- BREADTH — when the system goes risk-off due to narrow tape.

Suggested call shape from inside Python:
```python
import subprocess
subprocess.run([
    "python3", "regret.py", "log",
    "--ticker", ticker, "--strategy", strategy_name,
    "--reason", "FOMO_INDEX",
    "--entry", str(hypothetical_entry),
    "--stop", str(stop), "--target", str(target),
    "--horizon", str(horizon), "--thesis", thesis_text,
    "--correlation-class", corr_class,
    "--regime", f"spy_rsi_{int(spy_rsi)}_extended",
])
```

**Add benchmark line to brief output.** At the bottom of every brief, before "Pending for next session", emit:
```
=== Benchmark ===
{output of `benchmark.py spy --equal-cap 10000`}
```

This puts the SPY-or-not question on the page every day. Use `--equal-cap` because the system is cash-agnostic now (per CONSTITUTION v2.0).

## runbook.py

When `runbook.py preflight` rejects a trade due to a CONSTITUTION rule, also call `regret.py log` so the rejection is captured even when the ledger entry never happens. The preflight output already has all the fields needed.

## Friday brief (weekly review)

Add a "Regret review" section that runs:
```
python3 regret.py review --days 5,20
python3 regret.py summary --window 30
```

The summary by reason answers the load-bearing question: *if FOMO_INDEX rejections have a hit rate above 50% over 30 days, the gate is costing P&L and should be amended.*

Wire into `morning_broadcast.py` weekday-of-week check (Friday only).

## Monthly amendment review

If `regret.py summary` shows FOMO_INDEX rejections paying out > 40% hit rate at T+20 across two consecutive months, the FOMO rule is a candidate for amendment per the constitution amendment protocol. Reference the backtest output in `state/backtest/` as supporting evidence.

## Backtest ROADMAP

`backtest.py pead` is the first strategy backtest. Initial run on 18 large/mega-caps (2025-05-01 to 2026-04-30) showed:
- PASS book +57.71% total P&L vs GATED book +17.43%
- Same 40% hit rate in both books
- Cost of FOMO gate: ~40 percentage points of cumulative return

To extend:
1. Add S&P 500 + Nasdaq 100 universe via `_universe.py`
2. Add earnings calendar filter (yfinance `Ticker.earnings_dates` or finnhub) to remove non-ER gaps
3. Add second strategy: `breakout_long` (close above 20-day high + volume confirm) under same gate test
4. Add ticker-level FOMO comparison: replace SPY > 20MA+2ATR with `ticker > 20MA+2ATR`
5. Add option-vehicle simulation via delta proxy (same approximation as `shadow.py`)

Each backtest output writes to `state/backtest/<strategy>_<window>.json`. Build a small `backtest.py compare` subcommand later to diff two runs.

## Caveats already known

- yfinance gap detection is a heuristic; not all >5% gaps are earnings. False positives (M&A, secondary, news) drag hit rate. Earnings calendar filter is the obvious fix.
- 40% PEAD hit rate is below the spec target of 55-60%. Two hypotheses: (a) mega-caps don't drift like the spec assumes (the spec says skip mega-caps); (b) target sizing (1ATR * 3) is too aggressive for the horizon.
- Backtest does not simulate option vehicles yet. PEAD spec recommends bull call debit spreads; running the same trades as stock approximates direction but understates option leverage and overstates loss tolerance.
- The shadow ledger and the regret ledger overlap conceptually. Shadow = "we hypothesized this and tracked it as a real trade." Regret = "we considered this and rejected it." Keep them separate so amendment evidence stays clean.
