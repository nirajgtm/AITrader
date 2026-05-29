# Robinhood after-hours tradability

Verified 2026-05-02 via primary sources (RH help docs, Cboe IR, CME release).

The brief surfaces signals that may fire when regular markets are closed. This file is the canonical reference for whether each signal is actionable on RH at the time it appears.

## Quick reference matrix

| Asset | Hours (ET) | Signal classes that apply |
|---|---|---|
| Equity stocks (extended) | 7:00 AM - 8:00 PM, Mon-Fri | breakout, PEAD entries, position trims |
| RH 24-Hour Market (~900 symbols) | Sun 8 PM - Fri 8 PM | mega-cap entries, watchlist trims |
| Equity options (single-name) | **9:30 AM - 4:00 PM ONLY** | leap_check, option_drawdown rebuy |
| Index options (SPX/VIX/XSP/RUT) | 9:30 AM - 5:00 PM **+ 8:15 PM - 9:25 AM** Sun-Fri | **vix_check** trade, SPX hedges |
| Index options (NDX) | 9:30 AM - 4:15 PM ONLY | NDX hedges (no overnight) |
| Crypto | 24/7 | crypto signals |
| Futures (ES/NQ/BTC/GC/CL) | Sun 6 PM - Fri 5 PM (1hr daily halt 5-6 PM) | macro hedges |

## Per-asset details

### 1. Equity stocks (extended hours)
- Pre-market: 7:00 AM - 9:30 AM ET
- After-hours: 4:00 PM - 8:00 PM ET
- Limit orders only, no market orders
- Wider spreads, partial fills common
- Some securities are whole-share-only in extended hours (no fractionals)

### 2. RH 24-Hour Market
- Sunday 8 PM ET through Friday 8 PM ET (24/5)
- ~900+ symbols as of 2025-2026 (was 43 at May 2023 launch, 226 by Dec 2023)
- Includes all mega-caps + popular ETFs (SPY, QQQ, IWM, TSLA, NVDA, AAPL, AMZN, META, etc)
- Routed through Blue Ocean ATS during 8 PM - 4 AM ET segment
- Limit orders only, whole shares only, GFD or GTC (90 days max)
- ATS price bands prevent trades outside reference percentages

### 3. Equity options (single-name)
- 9:30 AM - 4:00 PM ET only (4:15 PM for select ETF options like SPY)
- NO pre-market, NO after-hours, NO overnight
- Same-day expiry positions cannot be opened after 3:30 PM ET
- Auto-closeout of at-risk expiring positions begins 3:30 PM ET

### 4. Index options on RH (Cboe partnership)
**SPX, VIX, XSP, RUT:**
- Regular: 9:30 AM - 5:00 PM ET (curb extends to 5 PM)
- **Overnight: 8:15 PM - 9:25 AM ET, Sunday-Friday**
- This is the GTH (Global Trading Hours) window enabled via Cboe partnership

**NDX:**
- 9:30 AM - 4:15 PM ET only, no overnight

**All index options:**
- European-style (no early exercise)
- Cash-settled, 100x multiplier (10x for XSP)
- AM-settled on VRO Wednesday for VIX
- Section 1256 60/40 tax treatment
- Level 3 required for spreads

### 5. Crypto
- 24/7 except scheduled maintenance
- Market-order collars: 1% above for buys, 5% below for sells
- Position cost-basis caps: $50M for BTC/ETH/DOGE/XRP/PEPE/SHIB; $20M for others
- Some coins state-restricted
- Not subject to PDT rules

### 6. Futures (Robinhood Derivatives, launched 2025-01-29)
- 6:00 PM ET Sunday through 5:00 PM ET Friday
- Daily 5:00 PM - 6:00 PM ET maintenance halt
- ~23 hours/day, 5 days/week
- CME contracts: ES/NQ/YM/RTY (E-mini + Micro equity index), BTC/ETH crypto, FX majors, GC/SI/HG metals, CL crude, NG nat gas
- Requires RH Derivatives account approval
- PDT-exempt

### 7. ETFs
- Same rules as equity stocks
- 24-Hour Market eligible for major ETFs (SPY, QQQ, IWM, etc)

## Implications for brief signals

| Brief signal | When firable on RH |
|---|---|
| `leap_check` (SPY/QQQ LEAP entry) | Equity option = regular hours only (9:30-4) |
| `vix_check` SHORT_PUT_SPREAD | Index option = regular OR overnight 8:15 PM - 9:25 AM ET |
| `vix_check` HEDGE_LONG_CALL | Index option = regular OR overnight |
| `option_drawdown` rebuy suggestion | Equity option = regular hours only |
| Watchlist breakout entry | Stock = extended OR 24-Hour Market |
| Crypto signals | 24/7 |
| Futures hedges (if added later) | 6 PM Sun - 5 PM Fri |

## Sources
- [RH Extended-hours trading](https://robinhood.com/us/en/support/articles/extendedhours-trading/)
- [RH 24 Hour Market](https://robinhood.com/us/en/support/articles/24hour-market/)
- [RH Options trading hours](https://robinhood.com/us/en/support/articles/options-trading-hours/)
- [RH Index options](https://robinhood.com/us/en/support/articles/index-options/)
- [RH Futures](https://robinhood.com/us/en/about/futures/)
- [Cboe Extended Global Trading Hours](https://www.cboe.com/insights/posts/three-reasons-to-explore-extended-global-trading-hours-for-spx-and-vix-options/)
- [CME + RH futures launch (Jan 29 2025)](https://www.cmegroup.com/media-room/press-releases/2025/1/29/cme_group_futurestolaunchonrobinhoodbringingnewtradingopportunit.html)
