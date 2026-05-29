# Robinhood L3 + Cash Account — Trade Permissions Reference

Reference account profile this playbook assumes:
- Broker: **Robinhood**
- Account type: **Cash** (no PDT — no day-trade limit)
- Options level: **3**
- Extended hours: **enabled** (pre-market + after-hours)

Read this before publishing any options idea. Cash-account collateral rules and settlement mechanics shape what is actually executable on Robinhood, regardless of the subscriber's account size.

## What Level 3 + Cash actually permits

| Structure | Allowed | Notes |
|---|---|---|
| Long calls | ✓ | Pay premium, no extra collateral. Core directional-long tool. |
| Long puts | ✓ | Pay premium, no extra collateral. Core directional-short tool (since cash account can't short stock). |
| Debit spreads (bull call / bear put) | ✓ | Only net debit tied up. **The go-to for defined-risk directional plays.** |
| Credit spreads (bull put / bear call) | ✓ but costly | Cash account requires full **width x 100** collateral (not margin-based). A $5-wide spread ties up $500. Avoid for accounts that cannot afford the lockup. |
| Calendar spreads | ✓ | Sell near / buy far, same strike. Vol-crush play. |
| Diagonal spreads | ✓ | Different strikes + different expirations. |
| Iron condors | ✓ but collateral-heavy | Two credit spreads = 2x collateral. Defined-risk but ties up significant capital. |
| Covered calls | ✓ | Need to own 100 shares first. Reasonable on any name the subscriber already holds. |
| Cash-secured puts | ✓ | Need **strike x 100** in cash. Income strategy on names worth owning at the strike. |
| Short / naked calls | ✗ | Not on retail Robinhood at any level. |
| Stock shorting | ✗ | Cash accounts cannot short. Use puts or inverse ETFs (SQQQ, SPXS, SQQQ, SOXS, etc.). |
| Futures | ✗ | RH doesn't offer. |
| Crypto | ✓ | Limited list (BTC, ETH, DOGE, SOL, LINK, AVAX, etc.). No derivatives. 24/7 trading. |

## Vehicle preference (cash-account, any size)

Ranked by ease of execution and risk profile, not by account size:

1. **Long call or put** — 30 to 60 DTE, ATM or slightly OTM. Max loss = premium paid. Subscriber sizes to their own risk budget.
2. **Debit vertical spread** — cheapest defined-risk directional. Buy ATM, sell 2 to 5 strikes out. Better theta/IV resilience than naked long premium.
3. **Long calendar** — when IV rank > 70 and reversion is the thesis. Defined debit.
4. **Stock/ETF with tight stop** — when stop is < 3% away. Direct expression, no decay.
5. **LEAP call** — 12 to 30 month directional bet on quality name with structural tailwind.
6. **Cash-secured put** — income strategy on names the subscriber wants to own at the strike.
7. **Covered call** — income on long stock the subscriber already holds.

**Skip on Robinhood cash accounts regardless of size:**
- Iron condors (collateral overhead beats most subscriber benefit unless account is large)
- Wide credit spreads (full collateral lockup)
- Naked anything (not allowed)

## Settlement mechanics (critical on cash accounts)

**Trade date → settlement:**
- Options: T+1
- Stocks/ETFs: T+2
- Crypto: near-instant

**Good Faith Violation (GFV) — avoid at all costs:**
A GFV occurs when you:
1. Buy security X with unsettled cash from a prior sale, and
2. Sell X before the prior cash has settled.

**3 GFVs in 12 months → 90-day restriction** (pre-settled cash trading only).

**How to avoid on this system:**
- Track every sale's settlement date. If I close a position on Monday, proceeds settle Wed (stock) or Tue (options).
- Don't open a new position using those proceeds until settled.
- When in doubt, reconcile cash *including only settled funds* before the next buy.
- The `ledger.py` entries carry the trade date; the skill must flag if a proposed buy relies on unsettled proceeds.

## Extended hours

Pre-market: **07:00–09:30 ET**
After-hours: **16:00–20:00 ET**

**Rules on Robinhood:**
- Limit orders only (no market orders).
- Lower liquidity → wider spreads. Typical spread penalty: 0.5–2% vs. regular hours.
- Some ETFs and low-volume names basically don't trade pre-market. Check volume before submitting.
- Options **do not trade in extended hours** on retail Robinhood. Only equities + some ETFs.

**When to use:**
- React to earnings reports released after close.
- Position into a catalyst before 9:30 crowd arrives.
- Exit a losing overnight position if the news is bad and I can't wait for the open.

**When not to:**
- Thin names with 2%+ spreads — give up too much edge.
- First reaction to macro news — let first 30 min of regular hours find the level.

## Order types available

| Type | Use |
|---|---|
| Market | Avoid. Spreads on options can eat 5%-plus of premium on entry. Always use limits. |
| Limit | Default. Specify max buy / min sell price. |
| Stop (stop-market) | Triggers a market order at stop — can slip badly. Use sparingly. |
| Stop-Limit | Triggers a limit at stop. Safer than stop-market, but can fail to fill if price gaps through. |
| Trailing stop (%) | Dynamic stop by percentage. Good for locking gains on winners. |
| Trailing stop ($) | Dynamic stop by dollars. Same idea. |

**Preferred daily playbook:**
- Entry: **Limit order**, Good-for-day (or GTC if multi-day trigger).
- Stop on long stock/ETF: **Stop Loss** or **Trailing Stop**. One stop order per position.
- Target on long stock/ETF: **Price alert** at target, manual limit-sell when it fires. Robinhood does NOT allow two simultaneous sell orders on the same position (stop + limit target at the same time is impossible).
- Short option exit (CSP, covered call): **Buy to Close, Limit only**. No stop orders exist. Protection = price alert on the underlying.
- **Confirmed on 2026-04-24:** stop + target as two simultaneous orders is **not** possible on the same share lot. Three workarounds:
  1. **Stop + price alert** (default): keeps full R:R, needs user reaction when alert fires.
  2. **Split position** (e.g. 2 shares on stop, 2 shares on target limit): fully automated, halves exposure on both sides.
  3. **Trailing stop** replacing fixed stop: dynamic exit, no fixed target.

## Short option positions (CSP, covered call, any Sell to Open)

**Confirmed by user 2026-05-26.** When you are short an option (you sold it to open), Robinhood gives you exactly ONE way to exit:

- **Buy to Close** with a **Limit** or **Market** order. That is it.

The following order types do NOT exist on a short option position in Robinhood:
- Stop Loss -- not available
- Stop Limit -- not available
- Trailing Stop -- not available

**How to protect a short option position:**
1. Set a **price alert** on the underlying stock/ETF at the level where you want to reassess (e.g. if selling SLV 65P, set alert at $67).
2. When the alert fires, manually evaluate and place a **Buy to Close Limit** order if needed.

Never write Robinhood steps that include a stop order on a short option. It does not exist.

## Options-specific order tips

- Always use **limit orders**. Options bid-ask spreads can be 10–30% of mid — a market order is a gift to the market maker.
- For spreads: submit as a **single spread order** (not two legs). RH supports this. Limit the net debit/credit.
- Before entry, check bid-ask spread: if spread > 10% of mid, the name is too illiquid — skip.
- Check OI on both legs of a spread. Low OI = hard to exit.

## Crypto on Robinhood (notes for when regime flips)

- 24/7, no settlement delay, no PDT, no options.
- Available: BTC, ETH, DOGE, SOL, LINK, AVAX, MATIC, LTC, BCH, ETC, UNI, AAVE, COMP, XLM (list varies).
- **Can transfer in/out via Robinhood Wallet** — but this is a manual flow, don't optimize for it.
- Use for: overnight-catalyst plays when equity markets closed, BTC-leadership regime trades.
