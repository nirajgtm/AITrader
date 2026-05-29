# Strategy: regime_mean_revert

## One-line
Fade an overextended index/sector when price is materially above its 20MA + 2·ATR ceiling AND breadth/leadership is narrowing — the classical "buyers exhausted" mean-reversion setup.

## Regime fit
- **Bull tape that has stretched**: SPY/QQQ above all MAs, trending up 5d, RSI > 70-75 (extreme on QQQ ≥ 85), price > 20MA + 2·ATR.
- **Narrow-breadth confirmation**: cap-weight beating equal-weight by ≥ 2% over 20d (RSP/SPY divergence), or sector breadth narrowing.
- **VIX rising on tape strength** (`+5%` 5d while index up): vol market sniffing reversal.
- **Avoid in**: clean broad-breadth bull markets, low-VIX coil-and-rip regimes, multi-week consolidation breakouts (those continue).

## Setup
On the underlying index (SPY or QQQ):
- Price > 20MA + 2·ATR(14)
- RSI(14) > 75 (SPY) or > 85 (QQQ — tech extension)
- 5d return > 2%
- VIX has stopped falling (5d VIX change ≥ 0)
- Breadth divergence: RSP underperforming SPY by ≥ 2% over 20d, OR < 50% of S&P-100 basket above 50MA while SPY at 20d high

## Filters (all must pass)
- Liquidity: only on SPY/QQQ/IWM or their inverse leveraged ETFs (SQQQ/SPXS) — never thin names.
- Volatility: prefer to enter when VIX between 15 and 22; below 15 = vol cheap (use long puts), above 22 = signal noisy.
- IV rank context: if IV-rank > 60, strongly prefer **debit put spreads** over outright puts.
- No FOMC / CPI / NFP / mega-cap earnings inside horizon UNLESS using a defined-risk vehicle (debit_spread, calendar).
- Cumulative-risk cap: this trade is class `short_index`. If an existing short_index position is open, additional same-class trades cap at 6% cumulative.

## Vehicle selection
1. **Bear put debit spread on SPY/QQQ** — defined risk, lower vega, clean R:R. Default for short-dated mean-revert.
2. **Long put on SPY/QQQ** — only when IV-rank < 30 (cheap premium) AND horizon is short.
3. **Long inverse ETF (SQQQ/SPXS)** — when options sizing is infeasible OR when extension is on a multi-day timeline. **Caution: SQQQ is -3x DAILY rebalanced. Structural decay 2-5%/week in chop. Never hold > 7 trading days.**
4. **Calendar (sell front, buy back)** — if IV-rank > 70 and the thesis is vol-crush more than direction.

## Entry
Trigger: any of —
1. **Daily close** above 20MA + 2·ATR with intraday RSI > 75 → enter at next-day open or limit at prior-day close × 1.001.
2. **Pre-market gap up** > 1% on already-extended tape → enter at first 30-min open print on cash session.
3. **Topping-pattern confirmation** (lower high after RSI peak, MACD bearish cross on daily) → wider entry window OK.

## Stop (hard invalidation)
- Underlying breaks 0.5% above the prior swing high → thesis invalidated.
- For inverse ETF: own price falls below `(entry - 1.5×ATR(14))`.
- For long puts/spreads: underlying closes 1.5% above entry-day close.

## Target / scale-out
- Primary target: revert to 20MA on the underlying.
- Scale-out plan: take 50% off at +1.5R, trail remainder via 5d-MA crossover or exit at 20MA touch.

## Time horizon
3-7 trading days. Aggressive time-stop: if no progress in 3 sessions, exit.

## Size
Default 1.5% risk (lower than 2% cap because mean-reversion has lower hit-rate than trend trades).

## Earnings & macro overlay (CRITICAL — added 2026-04-25 from SQQQ near-miss)
**Hard rule:** If any mega-cap earnings (NVDA/AAPL/MSFT/META/AMZN/GOOGL/TSLA/AMD/AVGO/NFLX) print within the position's horizon AND vehicle is NOT defined-risk, the trade is REJECTED. Mega-caps gap 5-15% on earnings; that's a binary event you cannot directionally hedge with a delta-1 inverse ETF.

**FOMC overlay:** if FOMC decision falls within horizon, only debit_spread or calendar permitted.

## Historical expectation
Win rate target: 50-55%. Avg R-multiple expected: +1.0R.
**Update as trades close.** Empty until N≥5 closed trades using this playbook.

## Caveats / failure modes
- **Trend-day failure:** mean-reversion shorts get steamrolled in coordinated breadth thrusts (Zweig breadth thrust, oversold-bounce kickoffs). Skip if VIX 1-day move is < -10% from a high level (vol-collapse continuation pattern).
- **OPEX gamma pin:** in the week before monthly OPEX, dealers' hedging can suppress realized vol — mean-reversion shorts work poorly. Wait for post-OPEX.
- **Earnings season:** if 3+ mega-caps print in the next 5 days, use defined-risk vehicles only.
- **SQQQ specific:** DO NOT scale into a losing SQQQ position. The leveraged decay compounds against you. Either it works in 3-5 days or it's wrong.

## Example trades
- A position in this strategy's vein was opened *before* this strategy file existed and before the earnings-blackout rule was codified — a phantom-strategy + earnings-not-checked mistake at entry. Lesson: don't trade a playbook that isn't written down, and always check the earnings calendar before entering.
- `2026-04-23` SPY long put @ $708.45 hypothetical premium $12.50 (shadow `s_a4224f`). Currently underwater (~-$275) as SPY rallied to $713.94.

## Related
- Patterns: `../patterns/vix_mean_revert.md` (TBD), `../patterns/reclaim_of_20ma.md` (TBD)
- Edges: `../edges.md` #4 (volatility regime shifts), #5 (sector rotation)

## FOMO treatment
allow

## Notes on FOMO treatment
v2.2 amendment 2026-04-30. Read by `runbook.py preflight` and passed to `risk.py --fomo-treatment`. Override only with explicit user approval and a documented amendment.
