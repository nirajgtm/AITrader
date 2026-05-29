# Trader Roadmap — Future Improvements

Things deferred from prior sessions. Pick up when relevant. Each item: what it
is, why it matters, when to trigger, rough effort.

Current state: as of 2026-05-03 the system has 53 scripts, 4 API providers
(Finnhub / FMP / AlphaVantage / Polygon), 573-ticker universe.

---

## Tier 0 — SKILL.md v2.0 sweep (overdue, prompt hygiene)

### 0.1 — Strip residual $1k / 2% / 25% / $20 / $100 hard-cap language from `SKILL.md`

**What:** `SKILL.md` was partially updated for CONSTITUTION v2.0 (cash-agnostic) but the body still contains v1.x dollar-denominated and percentage-cap language inherited from the $1k personal-account era. Need a sweep.

**Specific lines / sections to revise (as of 2026-04-29):**
- **Step 3.5 vehicle layer** (~line 173) — "the $1k budget" → "the reader's per-trade risk budget".
- **Step 4 Decide** (~lines 181–185) — "2% risk, 25% concentration, cumulative ≤ 6% / 8%, max 4 open positions" presented as system-level CONSTITUTION gates. v2.0 made these reader-side parameters. Reword as guidance, not enforcement, and remove the "via `runbook.py preflight`" framing where it implies the system will reject reader trades.
- **Step 4 vehicle filter** (~lines 194–197) — "$-risk under $20", "premium × 100 ≤ $100", "almost always preferable to a naked long option at this size" — all $1k-account-specific. Replace with vehicle-selection principles tied to the *thesis horizon and IV environment*, not dollar size.
- **CONSTITUTION guardrails section** (lower in `SKILL.md`) — verify every numeric there matches the live `CONSTITUTION.md` v2.1, not v1.3.
- **Milestone upgrades section** (`$1,500 → Unusual Whales`, etc.) — kill or repurpose. Subscriber tier doesn't have a personal-account milestone path.

**Why:** When the prompt's preamble (cash-agnostic) and body (v1.x dollar caps) disagree, the agent follows the more concrete bottom-of-file instructions. This produced a duplicated `Cash / Equity` balance artifact in the 2026-04-29 evening brief (the PORTFOLIO line showed an identical cash and equity figure). Same pattern will keep firing on every brief until the body is rewritten.

**Trigger:** next dedicated prompt-hygiene session. Not blocking for daily briefs but should be cleared before any new contributor reads `SKILL.md` cold.

**Effort:** ~30–60 min. Multi-section edit, cross-referenced against `CONSTITUTION.md` v2.1.

**Cross-ref:** `knowledge/behavioral_audit.md` 2026-04-29 — PORTFOLIO line root cause.

---

## Tier A — Free API signups (zero cost, user action only)

These are gated on user signup; system already has graceful fallback when the
keys are absent. Adding the key flips on richer/faster paths automatically.

| Provider | Sign-up | Free tier | Why | Effort |
|---|---|---|---|---|
| FRED | https://fredaccount.stlouisfed.org/apikey | unlimited | Faster + cleaner macro data than CSV fallback | 5 min |
| CoinGecko Demo | https://www.coingecko.com/en/developers/dashboard | 30 req/min | 6× rate limit vs unauthenticated; better trending coverage | 5 min |
| Quiver Quant | https://api.quiverquant.com/account/register | limited | Closes congressional-trades data gap (currently only WebFetch fallback) | 5 min |
| Marketaux | https://www.marketaux.com/account/register | 100 req/day | Pre-scored sentiment news (additional signal vs Finnhub) | 5 min |
| Tiingo | https://www.tiingo.com/account/api/token | 50 req/hr | Backup for stocks + news when other providers throttle | 5 min |

**Trigger:** any time. No code changes needed; just add to `.env`.

---

## Tier B — Tier-3 deferred strategic projects (medium effort, high value)

### B.2 — Signal-score aggregator
**What:** Each scanner emits a 0-100 conviction score per name; brief surfaces
ranked candidate list with combined score.

**Why:** Currently candidates are sorted by # of sources hit (1, 2, 3, 4...).
A 4-source cluster on a thin name might be lower conviction than a 3-source
cluster on a clean setup. Score lets us weight differently.

**Trigger:** when we have ~10+ closed trades and can backtest "did high-score
candidates actually outperform?"

**Effort:** 1 session. Schema-only change; each scanner already has the raw
metrics needed (vol_x_avg, gap_pct, RS5d, etc.).

---

### B.3 — Daily-log auto-generation from digest JSON
**What:** Generator that reads `brief.py` JSON output and emits the daily-log
markdown skeleton (macro layer / regime / sector / watchlist / flow / book
comparison sections), pre-filled.

**Why:** Daily logs are mandatory but currently hand-typed. Auto-gen frees
Claude tokens for actual decision prose vs boilerplate.

**Trigger:** any time. Trivially additive.

**Effort:** 1 short session. Feed the digest to a Haiku subagent for prose
generation, or template-render in Python.

---

### B.4 — Global macro expansion
**What:** Add ECB / BoJ / BoE / RBA / OPEC meeting dates + commodity calendars
+ conference calendar (Davos, Jackson Hole, JPM healthcare, CES) to `macro.py`.

**Why:** XLE position management ignores OPEC; FX-sensitive trades ignore
ECB. The current macro calendar is US-only.

**Trigger:** when first non-US macro miss costs us a position (e.g., XLE held
through OPEC surprise) — log it in mistakes.md, then build this.

**Effort:** 1 short session. Mostly static lists with FMP `economic_calendar`
filtered by country.

---

### B.5 — FDA PDUFA / biotech catalyst tracker
**What:** Scrape biopharmcatalyst.com or use FDA RSS for upcoming drug
approval decision dates. Surface biotech names with PDUFA in horizon.

**Why:** Biotech is mentioned as Edge candidate in Day-0 brainstorm but
completely uncovered. PDUFA-driven moves are 20-40% binary events, perfect
for defined-risk vol plays.

**Trigger:** when subscriber demand or research bandwidth supports a biotech sub-feed. Biotech tail bets are defined-risk only.

**Effort:** 1 medium session. Scraping is brittle; biopharmcatalyst paid is
$30/mo if free path breaks.

---

### B.7 — Forensic Alpha-Gap retro tool
**What:** Weekly retrospective: pull top 20 universe movers (5d %), check
which our brief surfaced vs which we missed entirely. For each miss,
investigate where the signal lived — existing API we ignored, alt-data
source we don't have, social board (Reddit/Twitter), or genuinely
unknowable. Output: ranked "missing source" list with $-impact per source.

**Why:** Drives Tier-A signups and Tier-D paid upgrades empirically. Today
we add a data source on intuition; B.7 turns it into "this source would
have caught $X of missed alpha last quarter."

**Trigger:** monthly cadence after 30+ trading days of universe data
exists. Pairs with G.4's weekly review (B.7 = blind-spot forensics piece).

**Effort:** 1 medium session. Mostly orchestration: movers fetch +
news/flow lookup per missed name + structured summary.

---

## Tier C — Strategy file completion

Three strategy stubs exist but aren't written:

### C.1 — `breakout_retest_continuation.md`
Classic BO/RT/C on liquid leaders. Required for systematic use of `scanner.py
--breakouts`.
**Effort:** 1 short session. Template + entry/stop/target/horizon mechanics.

### C.2 — `iv_crush_calendar.md`
Sell front-month / buy back-month at same strike before earnings. Profits
from IV crush regardless of direction. **Defined risk.**
**Trigger:** when we have IV-rank > 70 candidate AND earnings is the catalyst.
**Effort:** 1 short session.

### C.3 — `flow_followthrough.md`
Piggyback on unusual call sweeps with the same direction (defined risk).
Currently `flow_scan.py` finds flow but no playbook tells us when to act.
**Effort:** 1 short session.

---

## Tier D — Premium API integrations (gated by edge-per-dollar, not equity milestones)

The original $2.5k / $5k / $10k milestone gates were retired on 2026-04-28 (CONSTITUTION v2.0). The system is now a research feed; subscriptions are an operating expense decision, not an account-equity unlock.

### Highest priority (closes the biggest blind spots)
| Service | $/mo | Buys | Replaces |
|---|---|---|---|
| **Unusual Whales Standard** | $48 | Real-time options flow, dark pool, congress, ATS, alerts | brittle capitoltrades scrape, partial flow_scan, partial sentiment |
| **Polygon Options Starter** | $79 | True IV rank, real chains, Greeks, real-time stocks | options.py IV proxy, yfinance delay |
| **Reddit API + scraper** | free | Forum sentiment for r/wallstreetbets, r/options, r/investing, r/stocks | nothing (current gap) |

### Tier 2 (after Tier 1 lands and produces measurable edge)
| Service | $/mo | Buys |
|---|---|---|
| **Benzinga Pro** | $97 | Real-time news squawk, ratings, M&A leaks |
| **Quiver Quantitative** | $10 to $30 | Clean congress + lobbying + government contracts data |

### Tier 3 (specialized, only if asset class becomes core)
| Service | $/mo | Buys |
|---|---|---|
| **CryptoQuant or Santiment** | $29 to $49 | Crypto on-chain flows, exchange reserves, whale moves |
| **FRED API** | free | St Louis Fed canonical macro data |
| **SEC EDGAR API** | free | 13F, Form 4, 8-K direct from source |

### Skipped
- Bloomberg Terminal ($24k/yr), Refinitiv Eikon ($20k+/yr), AlphaSense (enterprise) - overkill
- TradingView Premium ($30/mo) - charts only, no data edge over Polygon
- FinViz Elite ($40/mo) - screener overlap with current scanners

### Future autonomy
- **Alpaca paper-then-live** - autonomous order execution if user wants to step out of manual entry. Triggered by user decision, not account size.

---

## Tier E — Universe expansion (gated by data tier, not account size)

Current universe: 573 tickers (S&P 500 + Nasdaq 100 + ETFs).

| Add when | What | Why |
|---|---|---|
| Polygon Starter or paid yfinance equivalent | S&P 400 mid-caps (~400 names) | More PEAD candidates; mid-caps drift longer |
| Same data + Reddit scraper online | Russell 2000 leaders filtered by ADV > 1M | Small-cap setups tractable when retail-attention data exists |
| Polygon Advanced or international data | Select international ADRs (TSM, BABA, ASML, NVO, etc.) | Currency-driven plays, ADR/HOM divergence |
| Specialized biotech catalyst calendar | Biotech sub-universe (PDUFA-tagged names) | Tail-risk plays with defined-risk vehicles |

**Trigger:** when subscribing to a data tier, run `_universe.py` with the extended layer and validate scanners and brief.py still complete within their timing budgets.

---

## Tier F — Coverage gaps with no clean free path

These are real holes; document so we don't keep re-discovering them.

### F.1 — 13F institutional position tracking
**Status:** No free programmatic source. WhaleWisdom paid is $30/mo;
direct SEC EDGAR Form 13F parsing is doable but tedious (XML wrangling).
**Workaround:** quarterly manual review of top funds via WhaleWisdom free tier.

### F.2 — Real-time stops / intraday stop-trigger validation
**Status:** yfinance is 15-min delayed; Polygon free is delayed. We rely on
RH's GTC stop-limit orders (executed by broker, not us).
**Workaround:** good enough; the stop is a broker order, not a script trigger.
Becomes a script concern only if we go fully autonomous via Alpaca.

### F.3 — Crypto trading volume from major exchanges
**Status:** yfinance crypto pairs only show consolidated trade tape, not
exchange-specific volumes. CoinGecko free has aggregated only.
**Workaround:** acceptable for our scale.

### F.4 — Pre-market gap data with reliable volume
**Status:** Yahoo screener has it but is brittle; Polygon free has snapshots
locked behind paid tier.
**Workaround:** `movers.py --premarket` per-ticker fallback works but is slow.

---

## Tier G — Engineering quality of life

### G.1 — Watchlist YAML frontmatter
**What:** Replace free-text watchlist blocks with YAML frontmatter so
`watchlist_check.py` doesn't need regex scraping.

**Why:** Brittle parsing; user types "Entry trigger:" with varying punctuation.

**Effort:** 1 short session. Migration script + regex parser → YAML loader.

---

### G.2 — Volatility-scaled position sizing
**What:** Implement the VIX-bucket-based sizing currently mentioned in
CONSTITUTION narrative but not in `risk.py`:
- VIX < 15 → standard 2% risk
- VIX 15-22 → 1.5% risk
- VIX > 22 → 1% risk

**Why:** Constitution mentions it but doesn't enforce.

**Effort:** 1 short session. Add VIX query to `risk.py`; multiply max risk by
bucket factor.

---

### G.4 — Weekly shadow-vs-real review + Constitutional Amendment Advisor
**What:** Scheduled Friday-close agent runs `shadow.py pnl`, compares
divergence over rolling 20-trade window, posts a weekly review. **And** —
when shadow ≥ real by $50+ over 20+ closed trades, auto-drafts an amendment
proposal in `knowledge/amendments.md` under **Proposed**, citing the
specific rule(s) most likely responsible (FOMO gate, concentration cap,
cumulative-risk cap) and a candidate relaxation with sunset clause.

**Why:** Constitution amendment protocol triggers on shadow-outperforms-real.
Currently we'd notice manually; automating closes the feedback loop *and*
removes the friction of writing the proposal by hand. User still
approves/amends/rejects — no silent rule drift.

**Trigger:** when 5+ closed trades exist on real or shadow book. Amendment
auto-draft activates only at the 20-trade + $50-divergence threshold.

**Effort:** 1 short session for the review; +1 short session for the
amendment-drafter (Haiku subagent reading G.10 stats + last 20 ledger
entries → templated proposal).

---

### G.6 — Proxy-Vehicle Suggester
**What:** When a scanner signal triggers on a leader priced > $300 (NVDA,
META, GOOG, AVGO, etc.), `brief.py` auto-surfaces affordable expressions:
matching leveraged ETF (NVDL/NVDU for NVDA, TQQQ for QQQ leaders), debit
spread sketch with strikes/cost, and the relevant sector ETF.

**Why:** SKILL.md already mandates "compare 2 expression vehicles" before deciding. G.6 systematizes the comparison so it's never skipped, and makes affordable alternatives a default for subscribers with smaller accounts, not an afterthought.

**Trigger:** any time.

**Effort:** 1 short session. Static map of leader → leveraged ETF +
spread template generator using existing `options.py` chain data.

---

### G.8 — `audit.py` meta-audit tool
**What:** Diagnostic suite — dead code (functions defined but never
called across scripts/), API overlap (3+ providers hitting same endpoint
type), README-vs-script drift (script exists, no README entry, or
vice-versa), and strategy-file vs CONSTITUTION cross-reference coverage.

**Why:** As the system grew to 19 scripts + 4 providers, latent rot
accumulates. Cheap monthly hygiene catches bit-rot before it bites.

**Trigger:** monthly, or whenever a refactor lands.

**Effort:** 1 short session. AST walk + grep + diff against
`scripts/README.md`. Output is a single ranked report.

---

### G.9 — Protocol-level `scripts/README.md` upgrade
**What:** Per script in README, add a structured block: CLI signature
(positional args + flags), required vs optional flags, JSON output schema
(top-level keys + types), and exit-code meanings. Replace the current
"Purpose" one-liner with this richer per-tool contract.

**Why:** Today the agent often re-reads source code (`risk.py`,
`portfolio.py`) just to remember CLI shape — burns tokens on every
invocation. A protocol README means the agent reads ~3KB once and never
needs to open the source for invocation purposes.

**Trigger:** any time. Compounds with every future morning brief.

**Effort:** 1 medium session — one-time write-up across all 19 scripts.
Keep in sync via G.8 (audit detects drift).

---

### G.10 — `state/stats_summary.json` rolling stats
**What:** Pre-computed JSON written after every closed trade: WR over
last 10/20/50, avg R, real-vs-shadow $-delta, current cooldown state,
days since last loser, $-at-risk currently deployed. Brief reads this
instead of re-walking `ledger.jsonl`.

**Why:** Token economy + faster brief boot. Also gives G.4's
amendment-drafter a clean numerical input.

**Trigger:** any time. Pairs with G.4.

**Effort:** 1 short session. Hook into `portfolio.py close-position` to
recompute on each close.

---

## Tier H — Architectural debt

### H.1 — Crypto symbol overrides are fragile
Yahoo renamed MATIC→POL, UNI→UNI7083, COMP→COMP5692. Hardcoded in
`crypto.py`. Will break again on next rebrand.
**Better:** lookup from CoinGecko name → exchange-symbol mapping; cache 7d.

### H.2 — Polygon free-tier 5/min is the cold-start bottleneck
Scanner cold = 4 minutes. Fine for daily morning, painful for ad-hoc.
**Mitigation:** rate limiter waits the full window (already implemented).
Long-term fix: Polygon Stocks Starter ($29/mo) or Options Starter ($79/mo). See Tier D for the gating logic.

### H.3 — runbook.py is legacy
`brief.py` is canonical for morning/quick/status. `runbook.py` retains
`preflight` only. Eventually fold preflight into brief.py and delete runbook.

### H.4 — No transaction-cost modeling
SEC/TAF fees on stock sells are roughly $0.03 per $1k notional. Negligible per trade but compounds over hundreds of trades for sized accounts. Worth adding to the personal-account tracker when the user is running a sized portfolio.

---

## Tier I — Distribution / open-source readiness

Only relevant once the system is stable AND the user wants others (subscribers,
collaborators) to self-host. Until then, this entire tier is parked.

### I.1 — Personal-state isolation
**What:** Move all personal artifacts (`state/portfolio.json`,
`state/ledger.jsonl`, `state/daily_log/`, `state/shadow_*`,
`broadcast_recipients.json`, `.env`) behind a `TRADER_STATE_DIR` env var
(default `~/claude-configs/trader/state/` for current user, but
configurable). Add `.gitignore` covering all of the above.

**Why:** A clean fork should pull the engine + scripts + strategies +
roadmap, NOT the maintainer's portfolio or recipient list.

**Effort:** 1 medium session. Mostly path refactoring across `_common.py`,
`portfolio.py`, `ledger.py`, `shadow.py`, `broadcast.py`.

---

### I.2 — Zero-leakage hardening
**What:** Pre-commit hook that scans staged diff for: API key patterns
(per-provider regex), absolute home paths, phone numbers, brokerage
account references. Fail commit on hit. Pair with a one-time `git filter`
sweep before first public push.

**Why:** Mathematical guarantee that `.env`, recipient phones, and
account-specific data never reach a public remote, even by mistake.

**Effort:** 1 short session. Existing tools (`gitleaks`, `git-secrets`)
do most of the work; we add the trader-specific patterns.

---

### I.3 — `INSTALL.md` + generic `README.md` for "clone & trade"
**What:** New-user onboarding doc: prereqs (macOS/Linux, Python 3.11+),
clone steps, `.env` setup walkthrough, free-API signup links (already in
Tier A), first-run smoke tests, "your first morning brief" worked
example. Generic `README.md` rewritten to be project-first, not
user-first (current README assumes single operator).

**Why:** Without this, "clone the repo" turns into a 2-hour support
session per new user.

**Effort:** 1 medium session. Largely transcription of what already lives
in `.env.example` + `scripts/README.md` + `SKILL.md` into a new-user
narrative.

---

**Tier I trigger:** account stable for 60+ days (no halts, no rule
breaches, brief running daily without intervention) AND user has decided
to make the system available to outside operators. If both conditions
aren't met, working on Tier I is premature optimization.

---

## Quick prioritization heuristic

When picking the next item, weight:

1. **Has a real-money reason** — has a recent loss, near-miss, or missed
   opportunity been traceable to this gap? → highest priority.
2. **Cheap to build** — Tier G items take 1 short session each.
3. **Compounds** — items that unblock other items (e.g., G.10 stats feeds G.4 amendment drafts) earn priority.
4. **Avoid premature optimization** - Tier H.4 (commission tracking) only matters once a personal account is sized enough that $0.03/$1k notional accumulates. Below that, useless complexity.

Rule of thumb: never work on this list unless current trades + open positions
are clean and the morning brief is running well. The roadmap is for slack time,
not active-trading time.
