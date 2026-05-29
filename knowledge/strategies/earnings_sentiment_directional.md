# Earnings Sentiment Directional

A calculated-risk setup for taking directional bets on earnings prints. Acknowledged as a bet, not an edge play. The goal is to read the dominant sentiment correctly and use a defined-risk vehicle so a wrong call caps the loss.

## Why this strategy exists
The CONSTITUTION blocks holding stocks or naked options through earnings. That rule is correct on average. But it leaves real opportunity on the table when sentiment is heavily lopsided one way and the market is positioned wrong. This strategy is the carveout: defined-risk only, sentiment-aligned, sized as a calculated bet not a swing trade.

## When to use
All of the following must hold:
1. Underlying reports earnings within 5 trading days
2. We can read the dominant sentiment with at least 4 of 7 signals aligned (see below)
3. A defined-risk vehicle is available with reasonable liquidity (OI > 100 each leg, b/a < 15% of mid)
4. Position fits within the 2% per-trade risk cap and 8% cumulative-risk cap
5. We have written down what would invalidate the sentiment read BEFORE entry

## Sentiment signals (need 4 of 7 aligned)
For each name reporting in the next 5 days, compute these and record alignment direction (bullish or bearish):

1. **Pre-earnings price drift (5d return)**: > +5% bullish, < -5% bearish, between is neutral
2. **Volume trend (last 3 sessions vs 20d avg)**: > 1.5x and price up bullish, > 1.5x and price down bearish
3. **Implied move from straddle (front expiry ATM)**: > 2x avg implied move suggests crowd expects fireworks, contrarian read on direction
4. **Skew (25-delta call IV vs put IV)**: calls premium > puts bullish positioning, puts premium > calls bearish positioning
5. **Sector relative strength (5d RS rank)**: top quartile bullish, bottom quartile bearish
6. **Analyst revision direction (last 30d)**: net upgrades bullish, net downgrades bearish
7. **Insider activity (last 90d)**: net buying bullish, net selling weak signal at best

If 4 or more align in the same direction, you have a sentiment read. If 3 or fewer align, no trade.

## Vehicle selection
Defined-risk only. Pick by IV environment:

- **IV rank > 70 + sentiment bullish**: debit call spread, ATM long leg, short leg at +1 to +1.5 implied moves out
- **IV rank > 70 + sentiment bearish**: debit put spread, mirror image
- **IV rank < 50 + sentiment bullish**: long call ATM or slightly OTM, 30-45 DTE so post-ER theta is manageable
- **IV rank < 50 + sentiment bearish**: long put, same rules
- **Avoid calendars and naked legs through ER**. Spreads only for the high-IV case; long premium only when IV is cheap.

## Sizing
- Calculated risk, not edge play. Size at half the normal swing-trade allocation.
- Max risk per earnings trade: 1% of equity (vs the 2% per-trade cap)
- No more than 2 concurrent earnings trades, regardless of correlation. Earnings volatility is its own correlation cluster.

## Entry timing
- Entry window: 1 to 3 trading days before the print, NOT day-of
- Reason: too close to print, IV crush risk on entry timing alone is high. We want a 24 to 48 hour window where the spread can build delta before the binary
- If multi-leg fill is available, take the worst-case fill at limit. If not, leg in at the long leg first.

## Exit rules
- **Pre-print exit if invalidated**: if any 2 of the 7 sentiment signals flip BEFORE the print, close the trade for whatever it shows
- **Hold through print only if 4+ signals still align at end of session before ER**
- **Post-print exit**: close the next session open, or set a sell-stop at 50% of max profit if directionally right
- **Never let a long-premium trade through ER expire** — close by the next Friday at latest

## What invalidates this strategy
Track the following over a 20-trade sample. If any of these become persistent, the strategy needs revision or retirement:
- Win rate < 35% (binary outcome distribution suggests a 40-50% win rate is needed for positive expectancy on 1.5x to 2x payoff structures)
- Average loss > average win (sizing or vehicle selection is wrong)
- Most losses come from sentiment signals being wrong (the read is the problem)
- Most losses come from IV crush (the vehicle was wrong even though direction was right)

## Live shadow tests (as of 2026-04-27)
Two tests open for Wed 4/29 mega-cap quad-print:
- **MSFT bullish debit call spread** (s_441ba2): Azure/AI narrative + RSI 75 less extended + less crowded long. 4 of 7 signals bullish.
- **META bearish debit put spread** (s_2cbd67): AI capex worry + RSI 79.7 + crowded long. 4 of 7 signals bearish.

Outcome of these two trades plus the AMZN long stock and GOOGL long call rule-challenge shadows will give a 4-trade earnings cluster sample to read calibration from.

## Cross-references
- CONSTITUTION earnings blackout rule: `CONSTITUTION.md` Hard Limits section
- Defined-risk carveout rationale: `knowledge/mistakes.md` 2026-04-25 SQQQ near-miss entry
- Amendment protocol if shadow data invalidates: `knowledge/amendments.md`

## FOMO treatment
size_demote

## Notes on FOMO treatment
v2.2 amendment 2026-04-30. Read by `runbook.py preflight` and passed to `risk.py --fomo-treatment`. Override only with explicit user approval and a documented amendment.
