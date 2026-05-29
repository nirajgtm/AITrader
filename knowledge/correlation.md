# Correlation Classification

## Why this exists
CONSTITUTION v1.1 introduced a cumulative $-at-risk cap: ≤ 6% if positions are
correlated, ≤ 8% if uncorrelated. "Correlated" was left as a judgment call —
this file makes the classification explicit and machine-readable.

`scripts/risk.py` reads `correlation_class` from each open position and
matches against the proposed trade's `--correlation` argument. Same class →
correlated cap (6%); different classes → uncorrelated cap (8%).

## Classes

| Class | Description | Members (examples) |
|---|---|---|
| `long_index` | Long broad market | SPY, QQQ, DIA, IWM, RSP, TQQQ, SPXL, UPRO, MDY |
| `short_index` | Short broad market via inverse ETF or long puts on indices | SQQQ, SPXS, SPXU, SDOW, SH, PSQ, SPY puts, QQQ puts |
| `long_tech` | Long tech-sector / mega-cap tech | XLK, NVDA, MSFT, AAPL, GOOGL, AMZN, META, AVGO, AMD, ORCL, CRM, TQQQ |
| `short_tech` | Short tech / inverse-tech / tech puts | SQQQ, SOXS, NVDA puts, mega-cap tech puts |
| `long_semis` | Long semiconductors | NVDA, AMD, AVGO, INTC, MU, TSM, ASML, QCOM, AMAT, KLAC, SMH, SOXL |
| `short_semis` | Short semis | SOXS, semi puts |
| `energy_long` | Long oil/gas/energy | XLE, XOM, CVX, COP, MPC, OXY, SLB, USO, BNO |
| `energy_short` | Short energy | DUG, energy puts |
| `financials_long` | Long banks/financials | XLF, JPM, BAC, WFC, GS, MS, C, KRE |
| `defensive_long` | Long staples/healthcare/utilities | XLP, XLV, XLU, JNJ, PG, KO, UNH, MRK |
| `cyclicals_long` | Long industrials/materials/discretionary | XLI, XLB, XLY, CAT, DE, BA, HD |
| `crypto_long` | Long crypto | BTC, ETH, SOL, MARA, RIOT, COIN, MSTR, IBIT |
| `crypto_short` | Short crypto | BITI, crypto puts |
| `vol_long` | Long volatility | UVXY, VXX, VIX calls |
| `vol_short` | Short volatility | SVXY, VIX puts, short straddles |
| `china_long` | Long China | FXI, KWEB, BABA, JD, PDD |
| `china_short` | Short China | YANG, China puts |
| `unknown` | Unclassified — treated as uncorrelated for cap purposes (use with care) | — |

## How to choose at trade time
1. **Pick the most specific class that applies.** SQQQ short = `short_index` (not just `short_index`). NVDA long = `long_semis` (which is more specific than `long_tech`).
2. **Document in the brief.** Always state which class you tagged and why.
3. **Cross-check against open positions.** If the proposed trade's class matches any open position's class → cumulative cap is 6% (correlated).

## Soft-correlation note (judgment)
Some pairs are not formally same-class but move together:
- `long_tech` ↔ `long_semis` — semis lead tech ~70% of the time.
- `short_index` ↔ `short_tech` ↔ `short_semis` — all roll together in risk-off.
- `vol_long` ↔ `short_index` — VIX up usually = SPY down.
- `crypto_long` ↔ `long_tech` — historically positive correlation.

When you have an open `long_semis` and propose a `long_tech`, treat as **correlated** even though the labels differ. The 6% cap applies. Document the override in the brief.

## Examples
- Open SQQQ (`short_index`). Propose XLE (`energy_long`). → uncorrelated. Cap: 8%.
- Open SQQQ (`short_index`). Propose SPY puts (`short_index`). → correlated. Cap: 6%.
- Open NVDA long (`long_semis`). Propose AVGO long (`long_semis`). → correlated. Cap: 6%.
- Open NVDA long (`long_semis`). Propose XLK long (`long_tech`). → soft-correlated → treat as 6% per judgment override.
