# Strategy: defensive_rotation

## One-line
Buy XLP or XLU when the rotation flag fires (sector RS5d > +10 vs prior 20d) and the index is at extended RSI, betting that capital rotates into defensives ahead of an index pullback.

## Regime fit
- BULL regime with index RSI > 75
- Breadth narrowing (RSP/SPY 20d < -3%)
- VIX bucket flipping from low to normal

## Setup
- XLP or XLU 5d rotation score > +10
- XLK or XLY 5d rotation score < -5 (the rotation source)
- Defensive sector RSI < 65 (not yet extended itself)
- FOMO clear on the defensive ETF

## Filters
- Skip if VIX already > 25 (rotation has already happened)
- Skip if 10Y yield is rising fast (utilities especially are rate-sensitive)
- Position must fit concentration cap

## Vehicle selection
- Stock/ETF preferred: defensives have low ATR, options have low premium and low gamma
- Long call only if IV rank < 30 (rare for defensives) and DTE > 45

## Entry
On the day the rotation flag first fires, at market session open. Limit at mid.

## Exit
- Target: 5% gain or breakdown of the rotation flag (defensive RS rolls below leading sector RS)
- Stop: 3% below entry
- Time: 14 sessions max

## Win condition
Win rate > 45% with avg win 1x to 1.5x avg loss. Defensive trades are slow grinders, not big winners.

## Open shadows testing this
- XLP ETF (s_f10d6c)

## FOMO treatment
size_demote

## Notes on FOMO treatment
v2.2 amendment 2026-04-30. Read by `runbook.py preflight` and passed to `risk.py --fomo-treatment`. Override only with explicit user approval and a documented amendment.
