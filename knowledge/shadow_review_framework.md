# Shadow Review Framework

How shadow-book performance feeds back into CONSTITUTION amendments. Without this loop, shadows are just data. With it, shadows are the engine of rule evolution.

## Categories of shadow trades
Every shadow opens with one of these labels in its strategy tag. The label determines what the trade is testing.

### 1. Rule challenges
Tests an existing CONSTITUTION rule by deliberately violating it in shadow. Strategy tags ending in `_test`:
- `fomo_chase_test` — entries above the FOMO ceiling
- `earnings_blackout_test` — equity through earnings
- `earnings_blackout_defined_risk_test` — defined-risk through earnings
- `cumulative_risk_cap_test` — correlated positions exceeding 6%

### 2. Strategy validations
Tests a named strategy from `knowledge/strategies/`. Strategy tag matches the strategy file name:
- `sector_rotation_pullback`
- `pre_earnings_momentum`
- `pead_long`
- `failed_breakout_reversal`
- `vol_expansion_pre_fomc`
- `defensive_rotation`
- `inverse_leveraged_reversal`
- `earnings_sentiment_directional`
- `regime_mean_revert`

### 3. Discipline counterfactuals
Tracks what would have happened if we had not followed a rule. Strategy tag matches the strategy of the real trade we declined or closed early. Example: SQQQ shadow tracking the counterfactual of holding through FOMC.

### 4. Hypothesis explorations
Tests a market-read hypothesis that is not yet a formal strategy. Tag like `retail_attention_contrarian`. If pattern repeats and pays, promote to formal strategy with a written file.

## Daily quota (binding from CONSTITUTION v2.3, 2026-05-04)
- **≥ 1 shadow OPEN on every weekday morning brief.**
- **≥ 2 shadow opens on candidate-rich days** (≥3 cross-scanner candidates OR a watchlist transition).
- **0 shadow opens allowed only when** the day is a weekend, a US market holiday, OR every scanner returned empty AND no watchlist row changed status. Justification goes in the daily log under `## Shadow-book activity`.
- **Sourcing priority** (pick the highest-value bucket with material today):
  1. Rule challenges (`*_test` strategies on candidates the CONSTITUTION rejected)
  2. Strategy validations (named strategy file, conviction below the bar OR runner-up)
  3. Discipline counterfactuals (paired with a real-trade close or skip)
  4. Hypothesis explorations (fresh patterns; promote to formal strategy after 7/10 wins)
- **No-lazy-shadows rule still applies.** Each shadow needs an explicit thesis + invalidation in one sentence each. If you cannot state both, you should skip the open and document the empty-tape justification rather than open a low-quality shadow.

## Review cadence
- **Daily**: every brief logs the day's shadow opens to the daily log (mandatory, see daily quota).
- **Weekly (Friday close)**: run `shadow.py pnl` and `shadow.py mtm`. Read every open shadow's current P&L and current thesis-validity status.
- **Monthly**: aggregate by category and by strategy. Compute win rate, average win, average loss, R-multiple, and sample size per bucket.
- **On 20+ closed observations per strategy**: full evaluation per the strategy's own retirement criteria.

## Translating shadow performance into amendments

### When to propose a CONSTITUTION amendment
- A `_test` shadow category outperforms the rule it challenges by + 5% over 20+ closed shadows. The rule is too tight.
- A strategy validation has a win rate below 30% over 15+ closed shadows. The strategy is unprofitable as written.
- A discipline counterfactual outperforms the real position by + 3% over 10+ paired observations. The discipline rule is leaving EV on the table.
- A hypothesis exploration has 7 of 10 wins. Promote to formal strategy.

### Amendment proposal template
For each amendment proposal append a section to `knowledge/amendments.md` under **Proposed**:

```
### YYYY-MM-DD — <one-line proposal>

**Trigger:** which review surfaced this (week-ending date, count of observations)
**Current rule:** verbatim quote from CONSTITUTION
**Proposed rule:** new wording
**Evidence:** shadow performance numbers, real-book performance numbers, side-by-side P&L
**Risk of being wrong:** what happens if we change the rule and the prior calibration was correct
**Sunset clause:** the amendment is reversible after N trades or M weeks if performance does not improve
**Rationale:** why this evidence justifies the change
```

### What does NOT justify an amendment
- A single big winning shadow that violated a rule. Sample of 1.
- A single losing strategy validation. Sample of 1.
- Performance over fewer than 10 paired observations. Statistical noise.
- A rule we want to change because it is annoying. Inconvenience is not evidence.

## Shadow hygiene
- **No lazy shadows.** Every shadow must have an explicit thesis and an invalidation condition. If you cannot state both in one sentence each, do not open the shadow.
- **Close shadows on time.** A shadow held past its horizon teaches us nothing about the original hypothesis. Close at horizon expiration regardless of P&L.
- **Mark shadows daily** when the brief runs `shadow.py mtm`. Stale prices give stale P&L which corrupts the review.
- **Pair shadows with real trades** when possible. The most valuable shadow data is the side-by-side comparison.

## Live shadow state
The open shadow book and its P&L are not snapshotted here — they go stale immediately.
The source of truth is the ledger: `shadow.py list` / `shadow.py pnl` / `shadow.py mtm`
(state/shadow_ledger.jsonl). Run those for the current set, categories, and review status.
