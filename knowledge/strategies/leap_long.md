# Strategy: leap_long

## One-line
Deep-ITM (0.80 delta) LEAP calls on broad-market ETFs, 18-month horizon, monthly DCA + opportunistic vol-timed entries, roll at 90 DTE before theta acceleration. Stock replacement with embedded leverage.

## Provenance
Synthesized 2026-05-02 from convergent practitioner research (Bogleheads HFEA threads, Ayres/Nalebuff *Lifecycle Investing*, Spintwig PMCC backtests, Lorintine Anchor strategy, r/options + r/thetagang post-mortems). Backtested via `scripts/backtest_leap.py` over 2019-2024.

## Regime fit
- BULL or trending CHOP regimes. The strategy is structurally long.
- Avoid initiating new positions in extreme high-vol regimes (VIX > 30); premiums become punitively expensive and the IV crush on recovery hurts.
- Survives moderate corrections via the -25% stop on the underlying. Does NOT survive extended bears like 2022 cleanly; expect deep drawdowns.

## Underlyings
- **SPY**: primary. Lowest IV, smoothest tape, ~17% IV at average VIX.
- **QQQ**: alternative. Higher beta, ~22% IV, larger alpha but larger DD.
- **TQQQ**: caveat-only. 65%+ IV (Leung/Sircar: LETF IV scales ~leverage^2), brutal vol decay on rebounds. Backtest shows highest absolute return but -88% max DD in 2022. Use only with hard 40% underlying stop and small (3% per LEAP) sizing if at all.

## Setup (all required for entry)
1. **Underlying** in tier above.
2. **VIX < 30** (skip if higher; vol regime is too expensive for new long premium).
3. **Either** monthly DCA (1st trading day of month) **OR** opportunistic trigger:
   - VIX < 18 (vol-cheap entry) **OR**
   - Underlying RSI(14) < 40 (pullback entry).
4. **Concurrency**: have a free slot below `max_concurrent`.
5. **Sizing room**: 5% of equity available for the entry.

## Strike & DTE
- **Strike**: 0.80 delta. Computed via Black-Scholes from spot, IV (VIX-derived), and 18mo TTM.
- **DTE at entry**: ~540 calendar days (18 months).

## Sizing
- 5% of total equity per LEAP entry.
- Max 6 concurrent LEAPs (~30% notional in LEAPs when filled).
- Equivalent leverage when filled: ~1.7x SPY, ~2.0x QQQ, ~3.5x TQQQ on the deployed portion.
- Cash NOT in LEAPs sits in the underlying ETF (the SPY-overlay rule). Without this, cash drag eats the alpha; with this, the strategy converges to "underlying when no signal, leveraged when signal fires."

## Roll
- Close + reopen when DTE drops to 90 days.
- New LEAP at 0.80 delta, 18mo DTE, sized off current equity.

## Stop
- Close if underlying drops `--stop-pct` (default 25% for SPY/QQQ, 40% for TQQQ) from the LEAP's entry-day spot.
- The stop is on the UNDERLYING, not the option premium. Premium-based stops trigger too early on theta + vega bleed during normal pullbacks.

## Profit target
- None by default. Roll discipline is the exit. Hard targets cap winners, which is where the strategy's alpha lives.
- Optional `--profit-target` flag can be set if the user wants to trim winners (e.g., +200% premium).

## Vehicle selection
- Stock-equivalent LEAP only. NOT spreads, NOT covered-call overlays.
- PMCC variant rejected per Spintwig research (short calls add 1-6% of total return on SPY; cap upside in bull tapes).

## Backtest results (2019-01-01 to 2024-12-31)

Run via `python3 scripts/backtest_leap.py --underlying X --start 2019-01-01 --end 2024-12-31 --cash-in-underlying`.

| Underlying | Strategy return | Buy/hold return | Alpha (pp) | Max DD | Stops triggered |
|---|---|---|---|---|---|
| SPY  | +223%  | +157% | +66pp  | -51% (Mar 2020) | 0 / 30 |
| QQQ  | +447%  | +243% | +204pp | -55% (Dec 2022) | 6 / 36 |
| TQQQ | +932%  | +774% | +158pp | -88% (Dec 2022) | 17 / 43 |

Caveats:
- IV synthesized from VIX (no skew, no fit to historical surface).
- 2% round-trip slippage assumed; real bid/ask on liquid SPY/QQQ LEAPs is tighter on close-to-the-money strikes, wider on deep ITM. Real cost likely ~2-4% round trip on 0.80 delta LEAPs.
- Dividends (~1.3% on SPY) ignored; the LEAP forfeits these vs holding stock outright. In a high-rate regime this matters more.
- Concurrency cap was binding heavily (851-868 skips per run); strategy could be tuned for more concurrent positions or less aggressive entries.
- One historical regime sample. 2022 bear was punishing; a longer or different bear (2008, 2000-02) would likely be worse.

## Earnings & macro overlay
- Index ETFs have no single earnings event; quarterly mega-cap reporting moves the index but doesn't trigger blackout.
- Avoid initiating in the 5 days before FOMC if VIX is already elevated (compound vol risk).

## FOMO treatment
allow

## FOMO treatment when leadership
allow

## Notes on FOMO treatment
The FOMO rule (rule 5, three-tier) exists to prevent chasing extended individual names. LEAPs on broad ETFs are a structural long position, not a chase trade. The rule does not apply meaningfully here; strategy gates entries on VIX (vol cost) instead.

## Risk warnings
1. **The path matters as much as the destination.** TQQQ -88% drawdown is mathematically "still profitable end-state" but few real traders hold through it. Honest sizing for TQQQ LEAPs is 0% unless this is play money you can write off.
2. **2022-style bears wreck the strategy.** Stops did fire 6 times on QQQ and 17 times on TQQQ. Each stop is a -25%-to-40% loss on the position. Multiple stops in a year compound.
3. **IV regime shift.** If structural IV moves higher (sustained VIX > 25 environment), LEAP premiums get expensive enough that the strategy's expected return falls. Reassess sizing.
4. **No leverage on leverage.** Don't add margin. Don't buy on top of existing leveraged positions. The implied leverage in deep-ITM LEAPs is the leverage; layering more is how blow-ups happen.

## Related
- Edges: `../edges.md` #8 (LEAP value setups)
- Anti-pattern: PMCC, OTM LEAP speculation, TQQQ LEAPs without stops
- Backtest scripts: `scripts/backtest_leap.py`, `scripts/_bs.py`
