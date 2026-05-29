# Working Universe

The universe is the set of tickers we consider tradable / scannable. It is
**not** the set we research every day — it's the haystack that scanners
(movers, breakouts, vol expansion) filter to find tradable needles.

## Composition (auto-built by `scripts/_universe.py`)

| Layer | Source | Count |
|---|---|---|
| S&P 500 constituents | FMP `/sp500-constituent` (cached 7d) | ~503 |
| Nasdaq 100 constituents | FMP `/nasdaq-constituent` (cached 7d) | ~101 |
| 11 S&P sector ETFs | static | 11 |
| Index ETFs (SPY, QQQ, IWM, DIA, MDY, RSP, VOO, VTI) | static | 8 |
| Leveraged + inverse ETFs (TQQQ/SQQQ/UPRO/SPXS/SOXL/SOXS/UVXY/...) | static | 30 |
| Crypto-equity proxies (MARA/RIOT/COIN/MSTR/IBIT/...) | static | 11 |
| Commodity + bond ETFs (GLD/SLV/USO/UNG/TLT/HYG/...) | static | 11 |
| **Total (deduped union)** | | **~573** |

## Why not "all ~12,000 US stocks"?

Polygon `grouped_daily` returns ~12k tickers for a single date in one call. We *use* that for breadth/scanner computation, but we **filter to the universe** when surfacing candidates. The 12k include:

- Penny stocks under $5 (low signal, high noise)
- Foreign ADRs and obscure listings
- ETFs we don't trade (single-stock 2x ETFs, niche thematic ETFs)
- Recent IPOs without 200d history

The universe is the "tradable for this account" filter.

## What's NOT included (yet)

- Russell 2000 small-caps below S&P 500 (would 4x the universe size; signal density is lower)
- Mid-cap S&P 400 (Polygon grouped_daily already covers them; lift the filter when ready)
- ADRs / foreign primary listings (currency complications, after-hours mismatch with US sessions)
- OTC pink sheets (low liquidity, high manipulation risk)
- Biotech sub-universe (PDUFA-tagged names, options markets often illiquid)

## Membership API

```python
from _universe import get_universe, is_in_universe, ticker_metadata

if is_in_universe("AMD"):
    ...

meta = ticker_metadata("NVDA")  # → {"sector": "Technology", "industry": "Semiconductors", "name": "NVIDIA Corp"}
```

## CLI

```bash
_universe.py                  # show stats
_universe.py --refresh        # force re-pull constituents
_universe.py --check NVDA     # is NVDA in the universe?
_universe.py --meta NVDA      # show metadata
```

## Refresh cadence

- Constituent lists cached 7 days. Auto-refresh on first call after expiry.
- Static layers don't change without a code edit.
- After major index rebalances (S&P quarterly: Mar/Jun/Sep/Dec; Nasdaq annual: Dec) — manually run `--refresh`.

## Expansion path (gated by data tier and signal density, not account size)

The original $1k-account milestone gates were removed in CONSTITUTION v2.0 (2026-04-28). The new gating is data quality and noise-to-signal ratio.

| Add when | What | Notes |
|---|---|---|
| Polygon Starter or paid yfinance equivalent | S&P 400 mid-caps | More PEAD candidates; mid-caps drift longer than mega-caps |
| Same data tier + Reddit scraper integrated | Russell 2000 leaders filtered by ADV > 1M | Small-cap setups become tractable when retail-attention data is available |
| Polygon Advanced or paid international data | Select international ADRs (TSM, BABA, ASML, NVO, etc.) | Currency-driven plays, watch ADR/HOM divergence |
| Specialized biotech catalyst calendar | Biotech sub-universe (PDUFA-tagged names) | Tail-risk plays, defined-risk vehicles only |

The driver is: more data and better tooling unlock more universe coverage. Account size is no longer a meaningful gate because the system publishes ideas to subscribers, not to a single small account.
