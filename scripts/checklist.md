# Morning Checklist — MANDATORY WALK

**v1.2 update (2026-04-25):** the checklist is now executed by `runbook.py`. The skill's job is to *invoke* the runbook and *interpret* the output, not to run each step manually. Manual walking was the failure mode that produced the SQQQ near-miss.

## Paths referenced
- Venv Python: `~/claude-configs/trader/scripts/.venv/bin/python3`
- All scripts below live in `~/claude-configs/trader/scripts/`.

## 0. Date / time / research-freshness gate (FIRST, before any other step)
- [ ] **Note current local date and time** at the top of the response (e.g. "Now: Mon 2026-04-27 09:42 ET").
- [ ] Run `research.py freshness`. It returns one of:
  - **FRESH-FULL** (< 4h, same day) → `runbook.py status` only. Surface cached summary; ask user before any re-run.
  - **FRESH-QUICK** (4–12h, same day) → `runbook.py quick`. Re-pulls regime, sectors, flow, sentiment.
  - **STALE** (different day or > 12h) → `runbook.py morning`. Walks every step.

## 1. Run the appropriate runbook mode

```bash
~/claude-configs/trader/scripts/.venv/bin/python3 ~/claude-configs/trader/scripts/runbook.py <morning|quick|status>
```

`runbook.py morning` walks (in order):
1. **STATE LOAD** — portfolio.py show, mtm.py, shadow.py list/mtm/pnl
2. **REGIME** — regime.py (SPY/QQQ/IWM/VIX/yields/DXY/oil/gold/BTC vs MAs)
3. **SENTIMENT / VOL STRUCTURE** — sentiment.py (VIX9D/VIX/VIX3M term-structure, SKEW, VVIX, SPY PCR)
4. **BREADTH** — breadth.py (basket above 50/200MA, sector breadth, RSP/SPY narrow-leadership flag)
5. **SECTOR ROTATION** — sector_scan.py (5d/20d RS, rotation signals)
6. **EARNINGS** — earnings.py mega-caps + watchlist (30d window)
7. **MACRO / FED** — macro.py (FOMC + scheduled releases 14d, FRED prints)
8. **OPTIONS FLOW** — flow_scan.py majors + watchlist
9. **CONGRESS** — congress.py (last 7d) — falls to data-gap if free source unavailable
10. **INSIDER** — insider.py via SEC EDGAR (Form 4 cluster activity 30d)
11. **MOVERS** — movers.py (gainers/losers/most-active)
12. **CRYPTO** — price.py BTC-USD + ETH-USD
13. **WATCHLIST NEWS** — news.py --from-watchlist (48h)
14. **WATCHLIST PRICE SCAN** — price.py per ticker
15. **OPEN-POSITION REVIEW** — earnings + horizon expiry + invalidation re-check per open position
16. **AUTO-LOG** — research.py log written automatically at end with all step labels and gaps

The runbook writes the auto-log on completion. If you forget to invoke it, `research.py freshness` will say STALE next time.

## 2. Reconciliation (when user reports balance)
- [ ] `portfolio.py reconcile --reported <X>`. Drift > $1 → STOP and investigate before any trade.
- [ ] After investigation, if reported balance is correct, re-run with `--trust-reported` to align cash.

## 3. Multi-layer thinking walk (per candidate)
For each candidate trade, walk the layers (macro → regime → sector → ticker → catalyst → vehicle → timing → size → exit) in the brief.

## 4. Pre-trade gating (MANDATORY before proposing)
For each candidate trade:
```bash
runbook.py preflight \
  --ticker XXX --kind stock|etf|crypto|option --side LONG|SHORT \
  --vehicle stock|etf|long_call|long_put|debit_spread|calendar \
  --entry E --stop S --target T --size N --premium P \
  --underlying QQQ                  # for inverse ETFs / options
  --correlation short_index|energy_long|long_tech|...   # see knowledge/correlation.md
  --horizon-days N \
  --strategy STRATEGY_NAME          # must match knowledge/strategies/STRATEGY_NAME.md
```

`runbook.py preflight` runs:
- `risk.py` (cooldown, open-positions cap, per-trade risk %, R:R, concentration, FOMO,
  **cumulative-risk cap** with correlation lookup, **earnings blackout**)
- Strategy-file existence check (the file must exist with content; phantom tags rejected)

A REJECTED preflight → either rework the trade OR demote to shadow book. Never silently override.

## 5. Decide
- [ ] Propose **0 to N** trades, each independently APPROVED by preflight and within cumulative caps.
- [ ] Record INTENT in ledger for each approved candidate before user confirms.
- [ ] Demote any rule-blocked high-conviction setup to shadow book.

## 6. Daily log (mandatory, even no-trade days)
- [ ] Write `state/daily_log/YYYY-MM-DD_dayNNN_<tag>.md`.
- [ ] All sections per skill template.
- [ ] Include "Data gaps" section if `runbook.py morning` reported any.

## Recurring failure modes (resist actively)
- ❌ Walking checklist steps manually instead of invoking `runbook.py` (re-introduces skip risk)
- ❌ Skipping preflight on a candidate ("the numbers look obvious")
- ❌ Tagging a strategy without its playbook file existing
- ❌ Ignoring cumulative-risk cap when stacking trades
- ❌ Re-running full research when same-day cached data is fresh (wastes time)
- ❌ Forgetting to log session at end (runbook auto-logs; don't sidestep)
- ❌ Treating earnings calendar as advisory — it's a HARD gate now
