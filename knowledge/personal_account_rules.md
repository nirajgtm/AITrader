# Personal Account Rules

Discipline rules for running a personal account in parallel with the research feed. The CONSTITUTION (v2.0) no longer codifies dollar-denominated risk caps because the system publishes ideas to subscribers of varying account sizes. This file is where the original v1.x discipline rules live, for use by the user when sizing their own positions.

## Status
Optional. Apply these rules to your personal account. The research feed itself is unconstrained by personal cash.

## Position sizing
- **Max risk per trade: 2% of personal equity.**
- **Max single-name concentration: 25%** of personal equity (capital deployed, not risk).
- **Max open positions: 4** at once on the personal account.
- **Max options premium per contract: 10%** of personal equity.
- Shares sizing formula: `shares = floor(equity * risk_pct / (entry - stop))`.

## Cumulative-risk cap
- Total dollars at risk across all open + proposed positions:
  - Less than or equal to 6% of equity when positions are correlated (same direction on same underlying category)
  - Less than or equal to 8% of equity when positions are uncorrelated
  - Correlation classification is judgment. When in doubt, use 6%.

## R:R minimum
- R:R must be at least 2:1 for any swing trade. Reward at least 2x the risk.
- Long-term holds and LEAPs may have higher R:R targets (5:1 or more) but the framework still applies: estimate reward, estimate risk, ensure ratio.

## Cooldown
- Two consecutive losing personal trades on the same strategy = cooldown one day on that strategy.
- Three consecutive losing personal weeks = one-week halt on personal trading. Reassess regime, then resume.

## Drawdown warnings
- Drawdown reaches 25% from high-water = WARN. Reassess before opening new positions.
- Drawdown reaches 50% from high-water = personal halt. Postmortem. User decides whether to continue.
- Single trade losing more than 5% of personal equity = breach of these rules. Log in `mistakes.md` even if it works out.

## Vehicle limits at small account size
- Cash-secured puts only on strikes the account can fully collateralize.
- Covered calls only on stock the account already holds in 100-share lots.
- Iron condors and wide credit spreads tie up too much collateral; prefer debit structures.

## Tax considerations
- Track wash sales: do not rebuy a name within 30 days of a realized loss.
- Track holding period: short-term (under 1 year) taxed as ordinary income; long-term taxed at lower rates.
- Crypto-to-crypto trades are taxable events on US Robinhood.

## When these rules conflict with research output
The research feed publishes ideas regardless of personal cash. If a published idea cannot fit the personal account under these rules, the user has three choices:
1. Skip the idea on the personal account; track it in the shadow book.
2. Take a smaller-sized version that fits.
3. Adjust the rules above (with sign-off, logged in `knowledge/amendments.md`).

These rules are not the constitution. They are personal discipline. They evolve at the user's call, not the system's.
