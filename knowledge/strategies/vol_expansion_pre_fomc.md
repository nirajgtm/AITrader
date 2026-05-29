# Strategy: vol_expansion_pre_fomc

## One-line
Buy a vol-long position 3 to 5 trading days before FOMC when VIX is in the normal or low bucket and term structure is in steep contango, betting on at least a partial flattening into the meeting.

## Regime fit
- VIX in 12 to 22 range (normal or low bucket)
- VIX/VIX3M < 0.95 (steep contango)
- FOMC within 7 trading days
- No prior vol spike already in the cycle

## Setup
- VIX < 22
- Term structure ratio < 0.95
- FOMC scheduled within 3 to 7 trading days
- UVXY or VXX RSI < 30 (oversold)

## Filters
- Skip if VIX already > 25 (the move has already happened)
- Skip if there has been a vol spike + retracement in the prior 30 days (asymmetry weaker)
- Lottery sizing only: max 1% of equity per attempt

## Vehicle selection
- **Robinhood**: UVXY long calls 30-45 DTE, ATM or +1 strike. VIX options not available on RH.
- **Other brokers**: VIX calls 14 to 21 DTE, ATM or 25% above current VIX. Cleaner expression than UVXY because no contango drag.
- Avoid VXX (less liquid options than UVXY at retail)
- Avoid SVXY long puts (inverse vol, complex)

## Entry
Entry window opens 5 trading days before FOMC, closes 2 days before. Day-of-FOMC entries are too late.

## Exit
- Target: VIX > 23 or UVXY +20% from entry, whichever first. Close immediately.
- Stop: UVXY breaks below entry-day low after 2 sessions
- Time: close by FOMC + 2 sessions regardless. Vol decays fast post-event.

## Win condition
Win rate > 30% acceptable because payoff structure is asymmetric (3x to 6x on wins). Need 6+ wins in 20 trades to be positive expectancy.

## Open shadows testing this
- UVXY long call 5/15 $40C (s_cad33c)

## Caveat
This is explicitly a lottery strategy. Hit rate is mid-30s by design. The edge is the asymmetric payoff, not the win rate.

## FOMO treatment
hard_block

## Notes on FOMO treatment
v2.2 amendment 2026-04-30. Read by `runbook.py preflight` and passed to `risk.py --fomo-treatment`. Override only with explicit user approval and a documented amendment.
