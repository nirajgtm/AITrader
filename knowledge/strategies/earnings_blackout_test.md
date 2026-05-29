# Strategy: earnings_blackout_test (and earnings_blackout_defined_risk_test)

## NOT REAL STRATEGIES
These tags exist only for shadow trades that deliberately violate the CONSTITUTION earnings blackout rule, so we can validate or amend the rule.

## Rule under test
> CONSTITUTION: holding a position through an earnings announcement is REJECTED unless the vehicle is defined-risk (debit spread, calendar). `risk.py` checks the underlying earnings calendar at entry.

## Hypothesis to test
Earnings blackout prevents catastrophic losses on average. Holding through earnings has negative expectancy on naked equity and naked options. Defined-risk is the only safe carveout.

## Counter-hypothesis A (naked equity through ER)
Holding a quality long stock through earnings is profitable on average if the company has strong sentiment alignment. The blackout rule overcorrects.

## Counter-hypothesis B (defined-risk through ER)
Defined-risk vehicles through ER are essentially safe. The carveout is correct as written.

## Test design
- Tag shadow trades with `earnings_blackout_test` (naked equity) or `earnings_blackout_defined_risk_test` (defined-risk)
- Track 10+ closed observations of each
- Compare outcomes:
  - If naked equity wins > 50%: rule too tight, propose amendment to allow long stock through ER with sentiment alignment
  - If naked equity loses < 30%: rule validated for naked, no change
  - If defined-risk wins > 60%: carveout works, codify clearer rules for which structures qualify
  - If defined-risk loses > 50%: carveout is wrong, tighten the rule to ban ALL ER exposure

## Open shadows testing this
- AMZN stock @ $261.12 (s_f30cbe) — naked equity through 4/29 ER
- GOOGL long call @ $350.34 premium $8.50 (s_fc0c33) — defined risk through 4/29 ER

## Important
**DO NOT take real trades tagged with these labels.** Tags are shadow-only.

## FOMO treatment
hard_block

## Notes on FOMO treatment
v2.2 amendment 2026-04-30. Read by `runbook.py preflight` and passed to `risk.py --fomo-treatment`. Override only with explicit user approval and a documented amendment.
