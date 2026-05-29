# Strategy: <NAME>

## One-line
<What this strategy exploits, in one sentence.>

## Regime fit
When is this strategy live? (bull/bear/chop, high-VIX/low-VIX, earnings season, etc.)

## Setup
The visual / statistical pattern on the chart or tape.

## Filters (all must pass)
- Liquidity: <min avg vol>
- Float: <min float>
- Price: <range>
- Volatility: <IV rank or ATR range>
- Fundamental: <optional>
- Related pattern: [`../patterns/<name>.md`](../patterns/<name>.md)

## Entry
Trigger condition + order type + price reference.

## Stop
Hard invalidation, in price or condition.

## Target / scale-out
Primary target + scale-out plan if any.

## Time horizon
Typical days held.

## Size
Default risk % (within CONSTITUTION limits).

## Historical expectation
Win rate / avg R-multiple / notes. Update as trades close.

## Caveats / failure modes
Where this breaks. Don't use when <condition>.

## Example trades
List closed trades that used this playbook. This file is public: use **shadow/synthetic** trades
only (the `s_...` series) — never a real position, size, entry, or P&L.
