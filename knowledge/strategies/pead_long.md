# Strategy: pead_long

## One-line
Post-Earnings-Announcement Drift — names that beat earnings AND raised guidance AND gapped up >5% on the report tend to drift higher for 2-4 weeks. Buy after Day 1, ride the drift.

## Regime fit
- Works in any regime, but best when broad market is bullish or neutral.
- Strongest in mid-caps (less analyst coverage = slower price discovery).
- Weak in extreme bear regimes (gap-ups get sold the next day).

## Setup (all required)
1. **Earnings beat** on EPS (actual > estimate).
2. **Guidance raise** OR strong forward outlook (read from press release / news).
3. **Gap up ≥ 5%** on the report day (intraday open vs. prior close).
4. **Volume confirmation**: report-day volume ≥ 2× ADV(20).
5. **Price > 50-day MA** at gap.
6. **In universe** (S&P 500 / Nasdaq 100 / sector ETFs); skip thin names.

## Filters
- Liquidity: ADV ≥ 1M shares, price ≥ $10.
- IV rank check: post-earnings IV typically crashes (vol crush). Use stock or
  bull call DEBIT spread; **avoid long calls** (theta + vega bleed kills).
- Skip if mega-cap (NVDA/AAPL/MSFT/etc.) — drift priced in faster on names with deep coverage.
- Skip if upcoming binary catalyst within horizon (FDA, secondary offering, court ruling).

## Entry
Day 1 close (post-report) → enter at next-session open or limit at +0.5% above gap close.

Reason for Day-1 entry (not gap day): the gap-day close is the cleanest signal that buyers held through the day. Day-2 momentum-followers extend.

## Stop
- Hard: gap fill (return to prior-close level).
- Tight: low of report day (more aggressive).

## Target / scale-out
- Primary: prior 52-week high or +1ATR(20) above gap close × 3.
- Scale-out: take 50% off at +1.5R, trail remainder via 10MA.

## Time horizon
2-4 weeks (10-20 trading days). Drift typically completes ~3-4 weeks; momentum names extend.

## Vehicle selection
1. **Stock/ETF** when ADV ≥ 5M. Most direct expression of the drift.
2. **Bull call debit spread** when capital is tight or IV is elevated post-print.
3. **LEAP call** for the rare PEAD that aligns with a multi-quarter structural thesis.
3. **NEVER long calls outright on PEAD** — IV crush kills theta-positive trades.

## Size
2% per-trade risk; 25% concentration cap.

## Earnings & macro overlay
- This IS the earnings trade — entry is post-print.
- Avoid if next FOMC / CPI / NFP is within 5 days (macro can override drift).

## Historical expectation
Win rate target: 55-60%. Avg R-multiple expected: +1.3R (drift trades have asymmetric upside).
**Update as trades close.** Empty until N≥5 closed PEAD trades.

## Caveats / failure modes
- **Buy-the-rumor, sell-the-news fade**: if the name had a 5%+ pre-earnings run-up, the gap is often given back. See `pre_earnings_runup` scanner — names flagged there should be SKIPPED for PEAD long.
- **Mega-caps fade fast**: NVDA/AAPL/MSFT have analyst-driven price discovery; drift priced in same day. Skip them for PEAD.
- **Sector divergence**: if XLK is rolling and an XLK name beats, the drift fights the sector. Cross-check sector_scan.py rotation_in/out.
- **Friday earnings**: weekend gives time for headline noise; Monday can erase the gap. Be cautious on Friday-AMC reports.

## Scanner output
`scanner.py --pead` (Phase 3.3): scans last 5 trading days for names where
gap-day return ≥ 5% AND volume ≥ 2× ADV AND close > 50MA. Then surfaces
candidates with at least the technical pattern; user must verify guidance-raise
manually from news/transcript.

## Related
- Edges: `../edges.md` #3 (PEAD)
- Patterns: `../patterns/pead_gap.md` (TBD)
- Anti-pattern: pre-earnings run-up + gap up = fade candidate

## FOMO treatment
size_demote

## FOMO treatment when leadership
allow

## Notes on FOMO treatment
v2.2 amendment 2026-04-30: `size_demote` set as default for PEAD across the broad universe (rule 5 three-tier).

v2.3 amendment 2026-05-02: `## FOMO treatment when leadership` carve-out added. When the candidate ticker is in `scripts/leadership.py LEADERSHIP_TIER`, runbook.py preflight uses `allow` instead of `size_demote`. Justification: backtest evidence (state/backtest/pead_ndx_*.json) shows the gate costs ~28pp on tech-heavy mega-cap PEAD, and the active edge above SPY lives in those leadership entries. The default `size_demote` still applies to non-leadership names.

Read by `runbook.py preflight` and passed to `risk.py --fomo-treatment`. Override only with explicit user approval and a documented amendment.
