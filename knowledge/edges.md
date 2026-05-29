# Edges I Hunt

Inefficiencies where a disciplined retail trader has an angle. Each edge feeds one or more
strategies. Review quarterly — edges decay when too many people exploit them.

## 1. Options flow lead-lag
Large unusual call/put activity on a name often front-runs catalysts. Retail can't see dark-pool
institutional positioning, but can see option sweeps. Lag between flow print and spot move is minutes
to days — a tradable window.
**Implementation:** `flow_scan.py --majors / --from-watchlist` (yfinance option-chain volume>2×OI).
**Strategy:** `strategies/flow_followthrough.md` (TBD — written when first opportunity arises).

## 2. Smart-money clustering
When 2+ independent signals (13F, insider, congress) hit the same name within 60 days, the base rate
of the name outperforming the market 3–6 months later is well above chance.
**Implementation:**
- Insider via SEC EDGAR (`insider.py`) + Finnhub MSPR per ticker (`insider.py --ticker NVDA`)
- Congress via Quiver if `QUIVER_API_KEY` set, else capitoltrades scrape (`congress.py`)
- 13F not yet implemented (would require WhaleWisdom paid tier or direct EDGAR Form 13F parsing)

## 3. Post-earnings drift (PEAD)
Academic anomaly: stocks that beat earnings + raise guidance + gap up continue for 2–4 weeks.
Works best on mid-caps (less analyst coverage). Mega-caps priced in faster.
**Implementation:** `scanner.py --pead --days 60` finds gap-up + vol-confirmed names above 50MA;
**Strategy:** `strategies/pead_long.md` (live).

## 4. Volatility regime shifts
VIX crossings (15, 20, 25, 30) change the statistical distribution of returns. Sizing must follow.
VIX > 25 = defensive sizing + mean-revert strategies. VIX < 15 = complacency; sell premium or
buy cheap IV (vega long).

## 5. Sector rotation
Relative strength rankings turn before the index. When XLE starts leading for 2+ weeks, rotate into
energy leaders. When XLK rolls over, cut tech exposure first.
**Implementation:** `sector_scan.py` (5d/20d RS vs SPY; rotation_in/rotation_out signals);
**Strategy:** `strategies/sector_rotation_bottoming.md` (live; XLE candidate as of 2026-04-25).

## 6. Liquidity squeezes
High short interest (> 20% float) + small float (< 100M) + fresh catalyst = asymmetric upside.
Rare setup; when it appears, size small but swing wide.

## 7. Earnings volatility crush
IV rank typically > 70% into earnings, collapses after. Defined-risk structures (iron condor,
calendar) can monetize the crush without directional bet.
**Strategy:** `strategies/iv_crush_calendar.md` (TBD — not yet written).

## 8. Retail-extreme contrarian
When WSB / StockTwits sentiment hits extremes (top of hype cycle, or capitulation), the short-term
countertrade has positive expectancy. Hard to time, size small.

## Edge candidates (unconfirmed — N < 15 in target regime)

### C1. Regime-conditional momentum chase (2026-05-21, 25 obs)
In BULL_TRENDING tape (SPY momentum intact, VIX falling), FOMO-blocked chases reached a near-term directional target at a high hit rate in shadow tracking. Outside that regime the hit rate drops sharply and tail risk is real. Edge appears regime-specific, not universal. R:R evidence incomplete (shadows set at R=0.5 targets; real trades require 2:1+). Tracking via the `fomo_chase_trending` shadow series with R=1.5 targets in BULL_TRENDING only. Promote to confirmed edge at 15 BULL_TRENDING observations with avg R > 0.5.

## Edge hygiene
- Assume every edge is decaying. Track base rate over time.
- New edges go here only after N≥5 observations.
- Retired edges get a dated note in `mistakes.md` explaining why they stopped working.
