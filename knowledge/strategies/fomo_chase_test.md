# Strategy: fomo_chase_test

## NOT A REAL STRATEGY
This is a **rule challenge test**. It exists only as a shadow-only label for trades that deliberately violate the CONSTITUTION FOMO ceiling rule, so we can validate or amend the rule based on outcomes.

## Rule under test
> CONSTITUTION: entries above the FOMO ceiling (price > 20MA + 2 ATR on traded vehicle, or on underlying for inverse vehicles per v1.2) are REJECTED by `runbook.py preflight`.

## Hypothesis to test
The FOMO rule prevents real losses. Chasing extended moves loses money on average over a sample.

## Counter-hypothesis
In strong-momentum regimes (BULL with breadth concentrated, RSI > 80 across indices), FOMO chases pay because the trend persists longer than the rule expects.

## Test design
- Open shadow long stock or long call on names that fail the FOMO check by > $5 of distance
- Track over 20+ closed observations
- Decision rule:
  - If shadow win rate > 50% AND average win > average loss: rule is too tight in this regime, propose amendment
  - If shadow win rate < 40%: rule validated, no change
  - If 40% to 50%: more data needed, expand sample to 30

## Open shadows testing this
- INTC stock @ $84.99, FOMO -$13.97 (s_4c5687)
- NVDA stock @ $216.61, FOMO -$14.51 (s_deadb0)

## Important
**DO NOT take real trades tagged with `fomo_chase_test`.** This tag exists exclusively for shadows. Real trades that pass FOMO must use a real strategy tag. Real trades that fail FOMO must not be opened.

## FOMO treatment
hard_block

## Notes on FOMO treatment
v2.2 amendment 2026-04-30. Read by `runbook.py preflight` and passed to `risk.py --fomo-treatment`. Override only with explicit user approval and a documented amendment.
