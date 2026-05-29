# Knowledge Palace — Map

The rooms. Each is a stable home for a kind of knowledge that should outlive any single conversation.

## PII rule — this repo is public

These knowledge files ship in a public repo. NEVER write real personal data into any tracked file
here: no real positions, share/contract counts, entry prices, stop/target tied to a live holding,
realized/unrealized P&L (dollars or percent), account balances, or "we hold/own/entered" language.
Examples and observation logs use **shadow/synthetic trades only** (the `s_...` paper series), never
a real position.

Per-machine learning logs are the exception and are **gitignored** (each install regenerates its own
as the mind learns, and they legitimately reference real local activity): `behavioral_audit.md`,
`mistakes.md`, `amendments.md`, `market_regime.md`. Everything else here is shared and must stay
PII-free.

## Rooms

### `market_regime.md`
Current top-down thesis. Bull / bear / chop? Risk-on or risk-off? What does the VIX say? What is
leadership? Updated whenever regime shifts, else appended with a dated snapshot line.

### `watchlist.md`
Names actively being tracked. One block per ticker: thesis, levels, catalysts, trigger conditions.
If a ticker stops being interesting, move its block to an archive section at the bottom.

### `strategies/`
Named, reusable playbooks ("Breakout-Retest-Continuation", "PEAD Long", "IV-Crush Calendar").
Each file is one strategy with: setup, filters, entry, exit, historical win-rate if known, caveats.
Use `TEMPLATE.md` to add a new one.

### `patterns/`
Observed market patterns I exploit. Different from strategies — a pattern is a *thing I saw*,
a strategy is a *playbook that uses patterns*. Example: "Gap-and-go following positive pre-market catalyst".
Use `TEMPLATE.md` to add a new one.

### `people_to_follow.md`
Smart money. Funds (via 13F), congressional trades, insider filings, reliable flow accounts on X/StockTwits.
With each name: where to check them, what they are good at, signal-to-noise score.

### `edges.md`
The inefficiencies I actively hunt. Each edge = a hypothesis about why retail has an angle on
this part of the market. Edges feed strategies. Strategies ride edges.

### `mistakes.md`
**Append-only postmortems.** Every loss gets a dated entry. What I saw, what I did, what broke.
The most important file in the palace. Read it monthly.

### `glossary.md`
Precise definitions of terms I use. Kelly fraction, R-multiple, IV rank, NOPE, delta, gamma, etc.
If I used a term in a daily log and could not define it here → add it.

### `correlation.md`
Mapping of position vehicles to correlation classes (long_index, short_index,
long_semis, energy_long, etc.). Used by `risk.py` to enforce v1.1 cumulative-risk
cap (≤6% correlated / ≤8% uncorrelated).

### `universe.md`
The working tradable universe (~573 tickers): S&P 500 + Nasdaq 100 + sector
ETFs + leveraged/inverse + crypto-equity proxies + commodities/bonds. Built
from FMP constituent endpoints, cached weekly. Scanners filter to this universe
to avoid penny moonshots and obscure listings.

### `amendments.md`
CONSTITUTION change log + proposal protocol. Every rule change is dated, justified,
and ratified.

## Cross-linking rules
- No copy-paste. If strategy X uses pattern Y, strategy file links to `../patterns/Y.md`.
- If a ticker has a written thesis longer than 1 paragraph, it gets a file in `research/` and
  `watchlist.md` links to it.
- Daily logs link backward into knowledge (not forward — knowledge is the stable layer).

## Compaction
When a room's file exceeds ~500 lines, split by theme into a subdirectory (e.g. `patterns/breakouts/`).
