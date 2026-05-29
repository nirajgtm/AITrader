# Strategy: inverse_leveraged_reversal

## One-line
Short a leveraged ETF (3x bull) via long puts when the underlying is at extreme RSI > 85 and the leveraged ETF has FOMO ceiling distance > -$15, betting on mean reversion exacerbated by daily-reset decay.

## Regime fit
BULL regime that has been melting up for 5+ sessions, RSI > 85 on the underlying.

## Setup
- Leveraged bull ETF (TQQQ, SOXL, SPXL, FNGU, etc) RSI > 85
- FOMO ceiling distance > -$15 (deeply above the FOMO threshold)
- Daily-reset decay has been compounding (5 or more flat-to-up sessions)
- Underlying index also at RSI > 80

## Filters
- Skip if there is a binary catalyst (FOMC, major earnings) within the put expiration window — the reversal can be delayed by event risk
- Liquidity: leveraged ETF options must have OI > 500 at the strike
- IV rank check: leveraged ETF options often have IV 80%+. Defined-risk via spread is preferred to naked put.

## Vehicle selection
- Debit put spread preferred: long ATM put, short 5 to 10% OTM put. Defined risk, less vega exposure.
- Long put only if IV rank < 70 (rare for leveraged ETFs)
- Avoid the 0DTE and weekly options on leveraged products (gamma risk)

## Entry
Day of FOMO trigger or next session open. Limit at mid.

## Exit
- Target: 50% gain on the spread, OR underlying index drops to 20MA + 1 ATR, whichever first
- Stop: leveraged ETF makes new high above entry day's high
- Time: 14 sessions max

## Win condition
Win rate > 45%. Daily-reset decay should provide tailwind even on flat tape, so a high win rate is expected. If win rate drops below 40%, the entry filter (RSI 85+ requirement) is too loose.

## Open shadows testing this
- SOXL 5/15 long put (s_fbb66a) — SOXL down 6% today but RSI 89.8 still extreme

## Cross-reference
Builds on the v1.2 inverse-vehicle FOMO rule (see `mistakes.md` 2026-04-25 entry).

## FOMO treatment
allow

## Notes on FOMO treatment
v2.2 amendment 2026-04-30. Read by `runbook.py preflight` and passed to `risk.py --fomo-treatment`. Override only with explicit user approval and a documented amendment.
