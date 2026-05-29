# Arsenal

Working Python tools. All run against the local venv (see `setup.sh`).

## First-time setup
```bash
cd ~/claude-configs/trader/scripts
bash setup.sh
cp ~/claude-configs/trader/.env.example ~/claude-configs/trader/.env
chmod 600 ~/claude-configs/trader/.env
# add your API keys to .env
```

## Invocation
```bash
~/claude-configs/trader/scripts/.venv/bin/python3 ~/claude-configs/trader/scripts/<name>.py [args]
```

## Tools

### Orchestrator (start here)
| Script | Purpose |
|---|---|
| `runbook.py` | Single-command morning walk: `morning` / `quick` / `status` / `preflight`. Auto-logs research session. |

### State + risk
| Script | Purpose |
|---|---|
| `portfolio.py` | Portfolio show / set-cash / reconcile / add-position / close-position / set-cooldown / mtm-sync. Auto-cooldown on 2 consecutive losers. |
| `mtm.py` | Mark-to-market all positions; `mark-option` to set a current option premium manually. |
| `ledger.py` | Append-only event log: INTENT / OPEN / SCALE / CLOSE / NOTE. |
| `risk.py` | Pre-trade gate. Enforces v1.2 rules: 2% / 25% / 4 open / R:R 2:1 / FOMO / cumulative cap / earnings blackout. |
| `position_size.py` | What-if calculator: shares from risk budget. |
| `shadow.py` | Shadow book — hypothetical trades + real-vs-shadow P&L compare. |

### Market scanners (mostly yfinance + cached)
| Script | Purpose |
|---|---|
| `regime.py` | Top-down: SPY/QQQ/IWM/VIX/yields/DXY/oil/gold/BTC vs MAs + 5d %. |
| `sector_scan.py` | 11 S&P sectors, 5d/20d RS, rotation signals. |
| `sentiment.py` | VIX9D/VIX/VIX3M term-structure, SKEW, VVIX, SPY PCR. |
| `breadth.py` | 60-name basket above 50/200MA, sector breadth, RSP/SPY narrow-leadership flag. |
| `flow_scan.py` | Unusual options activity (vol > 2× OI). |
| `options.py` | Option chain snapshot, IV, OI, unusual; `--iv-rank` for proxy IV percentile. |
| `price.py` | Per-ticker quote + OHLC + MAs + RSI(14) + ATR(14). |
| `movers.py` | Top gainers/losers/most-active + premarket gap scan. |
| `earnings.py` | Per-ticker next earnings + ATM-straddle expected move. |

### Catalysts / smart-money
| Script | Purpose |
|---|---|
| `macro.py` | Macro calendar (FRED + maintained FOMC + scheduled releases). |
| `news.py` | Per-ticker news via yfinance, cached 1h. |
| `insider.py` | Insider Form-4 via SEC EDGAR (free, official). |
| `congress.py` | Congressional trades — Quiver if key set, capitoltrades fallback. |

### Research log + caching
| Script | Purpose |
|---|---|
| `research.py` | Log a research session + freshness gate (FRESH-FULL / FRESH-QUICK / STALE). |
| `_cache.py` | TTL-based JSON file cache (CLI: `list / clear / get`). |
| `_apikeys.py` | Loads `.env`, exposes get_key/has_key/require_key. CLI: shows key presence (never values). |
| `_common.py` | Portfolio/ledger I/O helpers shared across scripts. |

### Provider clients (`_providers/`)
| Class | Provider | Free tier | Best for |
|---|---|---|---|
| `Finnhub` | finnhub.io | 60 req/min | News, earnings calendar (universe-wide), insider sentiment, recommendations |
| `FMP` | financialmodelingprep.com `/stable` | 250 req/day | Economic calendar, S&P/Nasdaq constituents, treasury rates, earnings/movers |
| `AlphaVantage` | alphavantage.co | 25 req/day, 5/min | Top movers, news+sentiment, company overview (high-cost — cache hard) |
| `Massive` | massive.com | unverified | Web-unblock for sites with anti-bot WAF (capitoltrades, finviz pages) |

`_providers/_base.py` provides shared rate limiter, cache integration, retry/backoff, 429 handling.
Status: `python3 -m _providers --status`

## Notes
- yfinance is 15-min delayed. Acceptable for swing decisions, not stops.
- All scripts respect the cache layer (`_cache.py`) — repeated calls within TTL are free.
- API keys live in `~/claude-configs/trader/.env` (never committed).
