# Strategy: pre_earnings_momentum

## One-line
Ride the price drift of a stock with strong 5-day momentum into its earnings print, exiting before the announcement to avoid the binary.

## Regime fit
BULL or CHOP regimes. Avoid in BEAR (drift typically reverses).

## Setup
- 5d return > +5% (bullish drift) OR < -5% (bearish drift)
- Earnings within 3 to 7 trading days
- Volume rising into the print (last 3 sessions > 20d avg)
- No FOMC or macro event between entry and exit
- Sector RS aligned with drift direction

## Filters
- Skip if stock has already had an earnings catalyst within the prior 90d (PEAD already played out)
- Skip if implied move > 2x average implied move (crowd is positioned, edge is gone)
- Avoid if RSI > 85 on bullish drift or < 15 on bearish (mean-revert risk)

## Vehicle selection
- Bullish drift: long call 30 DTE ATM or debit call spread if IV rank > 60
- Bearish drift: long put or debit put spread, same logic

## Entry
3 to 5 trading days before ER. Limit at mid.

## Exit
**HARD RULE: close before the ER announcement.** Not at, not during, before. Exit by close of the session prior to ER.

- Target: 1.5x premium gain or +1 ATR move on underlying, whichever first
- Stop: close below entry day's low
- Time: ER day minus 1 session, regardless of P&L

## Win condition
Win rate > 50% over 15+ trades. Pre-ER drift is a well-known pattern; if win rate drops, the entry filter (stocks that have ALREADY drifted) is the problem.

## Open shadows testing this
- TER stock long (s_efcfb6) — TER ER 4/28, currently testing the bearish drift case (TER -3.85% today)

## Cross-reference
Distinct from `pead_long`. Pre-earnings is about drift INTO the print. PEAD is about drift AFTER the print.

## FOMO treatment
hard_block

## Notes on FOMO treatment
v2.2 amendment 2026-04-30. Read by `runbook.py preflight` and passed to `risk.py --fomo-treatment`. Override only with explicit user approval and a documented amendment.
