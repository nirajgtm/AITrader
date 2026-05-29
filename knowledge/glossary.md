# Glossary

Precise definitions. If I used a term in a log and it is not here, add it.

## Core
- **R-multiple** — trade's profit/loss expressed in units of initial risk. A +2R trade = 2× the dollar you risked.
- **Risk per trade** — dollars at loss if stop hits. `shares × (entry − stop)`.
- **Kelly fraction** — optimal bet size given edge: `f* = (p·b − q)/b` where p = win prob, q = 1−p, b = reward/risk. Use fractional Kelly (0.25×) in practice; full Kelly is too volatile.
- **ATR(14)** — average true range over 14 periods. Volatility measure in price units.
- **RSI(14)** — relative strength index. > 70 overbought, < 30 oversold. Trend-breakers.
- **Moving averages** — 20 (short term), 50 (intermediate), 200 (long term / major trend).
- **VWAP** — volume-weighted average price (intraday). Institutional reference.

## Options
- **DTE** — days to expiration.
- **IV** — implied volatility. The option market's forward-looking vol forecast.
- **IV rank** — current IV percentile vs. its own 1-year history. > 70 = expensive, < 30 = cheap.
- **Delta** — sensitivity of option price to $1 move in underlying. Also ≈ probability ITM at expiry.
- **Gamma** — rate of change of delta. High near-the-money short-dated = pin risk.
- **Theta** — time decay. Short-dated options bleed the fastest.
- **Vega** — sensitivity to IV. Long-dated options are vega-heavy.
- **OI (open interest)** — total contracts outstanding. Proxy for liquidity + conviction.
- **Volume > OI** — unusual activity flag. Fresh positioning.
- **Sweep** — aggressive multi-exchange fill at the ask/bid, signals urgency. Proxy for "someone knows".
- **Vertical spread** — long one strike, short another, same expiry. Defined risk.
- **Calendar spread** — short front-month, long back-month, same strike. Vol-crush play.

## Market structure
- **PDT (Pattern Day Trader)** — margin account with > 3 day-trades in 5 rolling days needs $25k equity. Cash accounts exempt but have T+2 settlement.
- **T+2** — trade date plus 2 business days for settlement. Cash account unsettled funds can't be reused freely.
- **Gap** — open meaningfully above/below prior close. Filled = returns to prior close.
- **VIX** — CBOE volatility index. 30-day implied vol on SPX. The "fear gauge".

## Smart money
- **13F** — quarterly filing of long equity positions by institutions > $100M AUM. Lagged up to 45 days.
- **Form 4** — insider transaction filing. Near-real-time.
- **Cluster buy** — 3+ insiders buying within a short window. Bullish signal.
- **STOCK Act** — requires congressional trade disclosure within 45 days.

## Process
- **OODA** — Observe, Orient, Decide, Act. The morning loop.
- **Cooldown** — rule-mandated no-trade day after consecutive losers.
- **Invalidation** — thesis-level condition that says "I was wrong about the setup", distinct from the stop price.
- **Reconciliation** — matching reported balance to expected portfolio state. Drift = missed event.
