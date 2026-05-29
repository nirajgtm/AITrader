# Strategy: sector_rotation_pullback

## One-line
Buy a sector ETF that is the current 5d rotation leader after it pulls back to its 20MA or 50MA with RSI < 45, riding the dip in established leadership.

## Regime fit
Works best in BULL or CHOP regimes when sector dispersion is high. Avoid in BEAR when leadership flips daily.

## Setup
- Sector ETF rank 1 or 2 by 5d rotation score
- Price has pulled back to 20MA or 50MA (within 1% above or below)
- RSI14 < 45
- FOMO ceiling clear (NOT above 20MA + 2 ATR)
- Underlying driver still intact (commodity for XLE, rates for XLF, etc)

## Filters (all must pass)
- 5d rotation score > +15
- ATR-based stop fits 2% per-trade risk cap
- No FOMC or major macro release within 2 trading days
- Cumulative risk fits 8% uncorrelated cap

## Vehicle selection
- IV rank < 50: long call, 30-45 DTE, ATM or +1 strike OTM
- IV rank > 50: debit call spread, ATM long leg, +2 to +3 strikes short leg
- Stock direct: only if entry × shares fits concentration cap with stop < 3%

## Entry
At market session open after the pullback day. Limit entry within 0.5% of last close.

## Exit
- Target: prior swing high or 1.5 ATR above entry, whichever is closer
- Stop: close below 50MA or 1 ATR below entry, whichever is tighter
- Time: 18 days max for monthly options, 10 days for stock

## Win condition
Win rate > 45% with avg win > 1.5x avg loss over 15+ trades.

## Open shadows testing this
- XLE long call 5/15 $58C (s_d1771c)
- XOP ETF (s_fcf977) — cross-name validation

## FOMO treatment
allow

## Notes on FOMO treatment
v2.2 amendment 2026-04-30. Read by `runbook.py preflight` and passed to `risk.py --fomo-treatment`. Override only with explicit user approval and a documented amendment.
