# Strategy: retail_attention_contrarian

## One-line
HYPOTHESIS UNDER TEST. Short a name when retail attention spikes (Robinhood top trending, social media velocity > threshold), betting that retail attention peaks near tops.

## Status
**Not a validated strategy.** This file exists only because a shadow trade was tagged with this name. The hypothesis is being tested via shadow before being promoted to a real strategy.

## Hypothesis
Retail attention is a contrarian signal. When a name appears in the Robinhood top trending list, it has likely already moved, and the retail buying pressure is the last marginal flow.

## Test setup
- Ticker appears in Robinhood crypto trending or stock movers list
- 5d return already > +20% before the attention shows up
- No clean fundamental catalyst driving the move

## Test vehicle
- Crypto: long-only via spot for now (tracking the long thesis to test if the LONG works, contrary to the contrarian hypothesis)
- Stock: long puts or debit put spreads if pattern repeats

## Open shadows testing this
- SOL crypto long (s_f3addb) — testing the LONG side. If SOL pays, the contrarian hypothesis is wrong and "RH trending" is a momentum signal, not a top signal.

## Promotion criteria
After 10 paired observations (long shadow vs short shadow on RH-trending names), if the short side wins more than 60% of the time, promote to a real short strategy with full setup, filters, and exit rules. If the long side wins more than 60% of the time, retire this hypothesis and tag RH-trending as a momentum signal in `knowledge/edges.md`. If the result is between 40% and 60%, the signal is noise and we stop tagging trades with it.

## FOMO treatment
allow

## Notes on FOMO treatment
v2.2 amendment 2026-04-30. Read by `runbook.py preflight` and passed to `risk.py --fomo-treatment`. Override only with explicit user approval and a documented amendment.
