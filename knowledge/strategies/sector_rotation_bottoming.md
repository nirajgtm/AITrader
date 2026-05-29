# Strategy: sector_rotation_bottoming

## One-line
Buy a sector that has shown rotation strength (RS5d − RS20d > +10) AND has built a price base, on the 20MA reclaim — riding the rotation in a sector that's transitioning from laggard to leader.

## Regime fit
- Works in any market regime, but best when overall index is choppy or topping (rotation = where capital flows when index leadership tires).
- Especially strong when leading sector is rolling (RS20d > RS5d > 0 → RS5d falling) and the new leader's RS5d is rising.

## Setup
On a sector ETF (XLE/XLF/XLV/XLI/XLU/XLP/XLY/XLB/XLRE) — usually NOT XLK or XLC since those are mega-cap-driven and follow the index:
- Sector RS5d − RS20d > +10 (rotation accelerating in)
- Sector has built a price base (3-5 day consolidation) at or near a key MA
- Confirming setup: 50MA reclaim, or 20MA reclaim with rising volume
- Underlying commodity or thematic driver (e.g., for XLE: oil price 5d > +5%)

## Filters (all must pass)
- Sector ETF only — never thin sub-sector names without scanning their own RS.
- Cumulative risk: this trade is `<sector>_long` correlation class. Verify against open positions.
- No mega-cap earnings within horizon for sectors that DO carry mega-caps (XLK/XLC/XLY).
- Avoid XLE on Friday before weekend if there's geopolitical risk (open gap risk).
- Liquidity: sector ETFs generally fine; check ADV > 5M shares.

## Vehicle selection
1. **Long stock/ETF with tight stop** — when stop is < 3% away. Best risk/reward when capital allows.
2. **Bull call debit spread** — when capital is tight or IV-rank > 60.
3. **Long call** — only if IV-rank < 30. 30-60 DTE.
4. **LEAP call** — for multi-quarter rotation thesis with structural tailwind (e.g. energy CapEx cycle).
5. **NEVER inverse ETFs of the sector** — that's a different strategy (bearish).

## Entry
Trigger: any of —
1. **Limit-buy at or below prior close** during the rotation window — doesn't chase, lets you skip if it gaps.
2. **Reclaim of 20MA on rising volume** — limit entry within 0.5% of the reclaim level.
3. **Pullback to 50MA in an uptrending sector** — for the cleaner version.

## Stop
- Hard: close below the bottom of the consolidation base.
- For ETFs: typically 2-3% below entry, sized to the 2% account-risk rule.

## Target / scale-out
- Primary target: prior swing high or 1.5x ATR(14) above 20MA (typical rotation-trade range).
- Scale-out: take 50% off at +1R, trail remainder via 5MA crossover.

## Time horizon
5-15 trading days. Rotation themes typically run 2-4 weeks; we capture the mid-portion.

## Size
Default 1.5-2% risk depending on conviction (RS divergence strength).

## Earnings & macro overlay
- Sector ETFs: check whether the sector's biggest holdings have earnings in horizon. For XLE, no — energy supermajors mostly print on a quarterly cadence away from mega-cap weeks. For XLF, watch big-bank earnings windows. For XLV, watch UNH/JNJ.
- FOMC: sectors react to rates differently — XLF benefits from steeper curve; XLU/XLRE hurt by higher long rates. Note before entry.

## Historical expectation
Win rate target: 55-60% (rotation persistence is statistically positive).
Avg R-multiple: +1.2R.
**Update as trades close.** Empty until N≥5 closed trades using this playbook.

## Caveats / failure modes
- **Single-day RS spikes mean reverse fast.** Require RS5d − RS20d > +10 sustained over 2 sessions before trusting.
- **News-driven RS jumps** (e.g., oil shock) often fade in 3 days — exit fast if commodity rolls back.
- **Sector ETF IV is usually low**, so debit-spread economics often favor outright stock.

## Example trades
- *(none yet — XLE setup as of 2026-04-25 is the candidate for Monday 2026-04-27.)*

## Related
- Edges: `../edges.md` #5 (sector rotation)
- Patterns: `../patterns/reclaim_of_20ma.md` (TBD), rotation-divergence pattern (TBD)

## FOMO treatment
size_demote

## Notes on FOMO treatment
v2.2 amendment 2026-04-30. Read by `runbook.py preflight` and passed to `risk.py --fomo-treatment`. Override only with explicit user approval and a documented amendment.
