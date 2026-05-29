# Strategy: breakout_long

## One-line
Buy a 20-day high breakout on volume confirmation, ride the continuation. Stop at failed-breakout invalidation, target +1ATR(20) * 3.

## Regime fit
- Best in BULL or trending CHOP regimes when leadership is established and breadth is supportive.
- Weak in narrow-breadth tapes where breakouts are concentrated in 5 names; selectivity matters.
- Avoid in BEAR regimes (failed breakouts dominate, signal flips to bearish).

## Setup (all required)
1. **Daily close > prior 20-bar high** of the underlying.
2. **Volume confirmation**: report-day volume >= 1.5x ADV(20).
3. **In universe**: S&P 500 / Nasdaq 100 / sector ETFs / leadership tier (see `scripts/leadership.py`).
4. **Liquidity**: ADV >= 1M shares, price >= $10.
5. **No upcoming binary catalyst within horizon** (FDA, secondary, court ruling).

## Entry
At next-session open after the breakout day. Limit at +0.25% above the breakout high.

Reason for next-session entry: same logic as PEAD. The breakout-day close confirms buyers held through the day; next-day extension is the trigger that separates real breakouts from intraday spikes.

## Stop
- Hard: breakout high - 0.5 * ATR(20) (failed-breakout invalidation).
- Tight: breakout day low (more aggressive, higher stop-out rate).

## Target / scale-out
- Primary: entry + 1ATR(20) * 3.
- Scale-out: take 50% off at +1.5R, trail remainder via 10MA.

## Time horizon
2-4 weeks (10-20 trading days). Continuation trades that go past 20 days usually keep going; trail wider stop and let it run.

## Vehicle selection
1. **Stock/ETF** when ADV >= 5M. Most direct.
2. **Bull call debit spread** when capital is tight or IV rank > 50.
3. **LEAP call** for breakouts in leadership-tier names with multi-quarter structural thesis.
4. **NEVER long calls outright on extended names** -- IV crush or theta decay kills.

## Size
2% per-trade risk; 25% concentration cap.

## Earnings & macro overlay
- Skip if next earnings within horizon (use defined-risk if needed).
- Avoid if FOMC / CPI / NFP within 2 trading days (macro overrides technicals).

## Historical expectation
Backtest 2025-05-01 to 2026-04-30:
- S&P 500 universe (503 names): 1896 trades, 28.5% WR, +0.74% avg per trade
- 10-mega-cap subset: 40 trades, 30% WR, +0.74% avg
- Win rate target: 30-35%. Edge comes from asymmetric R:R (target 3R vs stop 1R).
**Update as live trades close.** Empty until N>=5 closed breakout trades.

## Caveats / failure modes
- **Narrow-breadth tape**: breakouts in 5 names while everything else fails are usually false starts. Cross-check breadth (RSP/SPY ratio, 50MA basket).
- **Sector divergence**: a name breakout fighting its sector rotation usually fails. Cross-check sector_scan.py rotation_in/out.
- **Friday breakouts**: weekend headline risk can erase the move. Be cautious on Friday-AMC catalysts.
- **Mega-cap breakouts in late-cycle bull**: when the index FOMO rule fires AND the breakout is in a leadership name, the signal is real but the gate must be overridden via leadership tier (see FOMO treatment below).

## Scanner output
`scanner.py` (universe-wide breakouts section): emits items where close > 20-bar high AND volume >= 1.5x ADV. Sorted by vol_x_avg descending.

## Related
- Edges: `../edges.md` #1 (trend continuation)
- Anti-pattern: `failed_breakout_reversal.md` (bullish-breakout-that-failed = SHORT setup, not long entry)

## FOMO treatment
size_demote

## FOMO treatment when leadership
allow

## Notes on FOMO treatment
v2.3 amendment 2026-05-02. Default `size_demote` for non-leadership names (broad S&P 500 backtest shows the gate is approximately neutral on average: PASS +1396% vs INDEX-FOMO +1331% across 1896 trades; gate keeps win rate slightly higher).

When the candidate is in `scripts/leadership.py LEADERSHIP_TIER`, override to `allow`. Justification: 10-mega-cap backtest shows index-FOMO flips +30% to -20% by removing exactly the leadership-driven trades that move the index. Active alpha above SPY lives in those entries.

Read by `runbook.py preflight` and passed to `risk.py --fomo-treatment`.
