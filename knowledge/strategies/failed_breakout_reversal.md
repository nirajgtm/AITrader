# Strategy: failed_breakout_reversal

## One-line
Short a stock that gapped up on the open with high volume but closed in the lower 25% of the day's range, betting that the supply visible at the highs continues into the next session.

## Regime fit
Any regime. Especially strong in BULL regimes with extended RSI and narrow breadth, where failed breakouts mark local tops.

## Setup
- Day's high > previous close × 1.05 (gap up at least 5%)
- Close < day's high − 0.75 × day's range (closed in lower 25% of range)
- Volume > 1.5x 20d average
- Long upper wick on the daily candle (textbook bearish reversal)

## Filters
- No earnings within 3 sessions (the reversal could be reset by ER catalyst)
- RSI14 > 60 prior to the reversal (need a tape that was extended, not a base)
- Sector RS not in top quartile (don't fight a leadership sector)

## Vehicle selection
Defined-risk only:
- Debit put spread, 14 to 30 DTE, long leg ATM or 1 strike OTM, short leg 2 to 3 strikes below long leg
- Long put if IV rank < 50 and underlying is liquid
- NEVER short the stock outright on a cash account

## Entry
Next session open. Limit entry within 1% of prior close. If gap-down on open is large, wait for retest of prior close before entering (often the failed-breakout shorts get squeezed first thing).

## Exit
- Target: prior consolidation midpoint or 50MA, whichever is closer
- Stop: close above the failed-breakout day's high
- Time: 5 sessions max for spreads, 10 for long puts

## Win condition
Win rate > 50% with avg win >= avg loss. Failed breakouts are a high-conviction pattern; if hit rate drops below 45%, the filter is wrong.

## Open shadows testing this
- QCOM 5/15 $145P/$140P debit put spread (s_de7b97)

## Why this matters
The pattern was caught by accident on QCOM 2026-04-27 ($160.94 high to $149.36 close). Worth codifying so the next instance is caught by scanner, not by chance.

## FOMO treatment
hard_block

## Notes on FOMO treatment
v2.2 amendment 2026-04-30. Read by `runbook.py preflight` and passed to `risk.py --fomo-treatment`. Override only with explicit user approval and a documented amendment.
