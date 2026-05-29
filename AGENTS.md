# AGENTS.md — Read-Only Investigation Guide for Other AI Agents

Use this if you are ChatGPT, Gemini, a Claude sub-agent dispatched via the Agent tool, or any AI agent other than the primary Claude session running the trader skill. The user has asked you to investigate or research the trader system.

**Scope of this guide: read-only investigation. You may RUN read-only scripts (quotes, scans, snapshots). You may NOT WRITE — no file edits, no state mutation, no opened positions, no sent broadcasts, no appended ledger rows, no new files anywhere in this tree.** The system is operated by the primary Claude session via the trader skill at `~/.claude/skills/trader/`. Other agents read and may execute read-only scripts; they do not act on state and they do not write.

If you take only one rule from this document, take this one: **read freely, run read-only scripts when needed, never write a single byte to disk in this tree.**

## 1. Project location and layout

Root: `~/claude-configs/trader/`

```
~/claude-configs/trader/
  CONSTITUTION.md              # the rules (read first)
  MISSION.md                   # the goal
  README.md                    # high-level overview
  ROADMAP.md                   # planned upgrades
  AGENTS.md                    # this file

  scripts/
    .venv/                     # Python virtualenv (REQUIRED for most scripts)
    brief.py                   # main morning digest
    price.py                   # quote a single ticker
    options.py                 # option chain for a ticker
    regime.py, sentiment.py    # market reads
    sector_scan.py, scanner.py # universe scanners
    earnings.py, macro.py      # calendars
    flow_scan.py               # unusual options
    insider.py, congress.py    # smart-money feeds
    movers.py, breadth.py      # tape internals
    portfolio.py               # account state (avoid running)
    ledger.py                  # append-only trade ledger
    shadow.py                  # shadow-book trades (paper)
    position_review.py         # open-position scan
    watchlist_check.py         # watchlist trigger scan
    runbook.py, risk.py        # preflight checks (avoid running)
    morning_broadcast.py       # auto-broadcast (DO NOT RUN, sends real iMessage)
    broadcast.py               # send iMessage (DO NOT RUN)

  knowledge/
    strategies/                # one file per named strategy
    patterns/                  # observed market patterns
    mistakes.md                # append-only log of errors
    amendments.md              # rule-change protocol log
    market_regime.md           # historical regime reads
    watchlist.md               # current watchlist
    correlation.md             # correlation reference
    edges.md                   # documented edges
    glossary.md                # term definitions
    universe.md                # tradable universe definition
    robinhood_playbook.md      # broker-specific notes
    people_to_follow.md        # external sources
    shadow_review_framework.md # how shadow data feeds amendments

  state/
    portfolio.json             # current positions, cash, equity
    ledger.jsonl               # append-only real trade log (one JSON per line)
    shadow_ledger.jsonl        # append-only shadow (paper) trade log
    shadow_positions.json      # current shadow book
    regret_ledger.jsonl        # post-hoc "should have taken it" log
    research_log.jsonl         # research events
    daily_log/                 # daily journals YYYY-MM-DD_dayNNN_*.md
    daily_log/archive/         # rolled-off older logs
    broadcasts/                # saved broadcast drafts
    cache/                     # API response cache (scripts may write here; you do not)
    portfolios/                # historical snapshots
    backtest/                  # backtest outputs
    market_regime/             # regime snapshots (when present)
    *.log                      # cron and launchd output (read for diagnostics)
    last_universe_review.txt   # timestamp marker

  .env                         # secrets — NEVER read or print
  .env.example                 # safe to read for variable names
  broadcast_recipients.json    # subscriber list — only read if user asks
  INTEGRATION_NOTES.md         # how skill, configs, and crons connect
```

If a path you expect is not listed, it may have been added since this doc was last updated. Use `ls` to confirm before assuming it is missing.

## 2. Environment setup

The Python scripts require a specific virtualenv at `~/claude-configs/trader/scripts/.venv/` and environment variables loaded from `~/claude-configs/trader/.env` (Tradier, Polygon, FRED, etc.). Without both, scripts fail in confusing ways.

**Each Bash tool call is a fresh shell.** A `source .venv/bin/activate` in one Bash call does NOT carry over to the next. Two safe patterns:

Pattern A — chain in a single Bash call:
```
cd ~/claude-configs/trader && source scripts/.venv/bin/activate && set -a && source .env && set +a && python3 scripts/price.py XLE --json
```

Pattern B — call the venv python directly (no activation needed) and load env inline:
```
cd ~/claude-configs/trader && set -a && source .env && set +a && ./scripts/.venv/bin/python3 scripts/price.py XLE --json
```

Always `cd ~/claude-configs/trader` first. Many scripts use relative paths to `state/` and `knowledge/`.

### Common failure modes and how to diagnose without writing

If a script errors, **do not "fix" it by editing files, installing packages, or running `setup.sh`**. Diagnose, report to the user, and stop.

- `ModuleNotFoundError: No module named 'pandas'` → venv not activated. Use Pattern A or B above. Do not `pip install`.
- `KeyError: 'TRADIER_TOKEN'` or similar → `.env` not loaded. Add `set -a && source .env && set +a` to the chain. Do not write a new env file.
- `FileNotFoundError: state/cache/...` → wrong working directory. Add `cd ~/claude-configs/trader` to the chain.
- Script hangs > 60s → likely cold cache hitting paid APIs. For `brief.py morning` this is normal (5–15 min). For everything else, prefer the cached variant or escalate.
- Silent zero output → an exception was swallowed. Re-run with `python3 -u` and `2>&1` to surface stderr. Report the trace to the user; do not patch the script.
- `.venv/` is missing entirely → **stop**. Tell the user the venv is missing and ask the primary Claude session to run `bash scripts/setup.sh`. Do not run `setup.sh` yourself; it writes to disk and may fetch and pin packages you do not want pinned.

## 3. Read-only investigation commands

All commands below are safe for other agents to run. They read data and print, no state changes. (They may write to `state/cache/` as part of normal API caching — that is not an operational state mutation and is allowed.)

For brevity the commands below show only the script invocation. Always prepend the full env chain from Section 2, e.g. `cd ~/claude-configs/trader && set -a && source .env && set +a && ./scripts/.venv/bin/python3 scripts/<...>`.

### Quick market read

```
python3 scripts/brief.py status     # cached digest, fast
python3 scripts/brief.py quick      # light refresh, ~10s
python3 scripts/brief.py morning    # full refresh, 5-15 min cold cache
```

`brief.py` prints one JSON dict to stdout. Schema:
- `headline`: human summary
- `regime_summary`: spy_regime, vix_bucket, term_structure
- `portfolio`: cash, equity, drawdown_pct, open_count
- `open_positions_review`: list with primary_action per position
- `candidates`: scanner-surfaced tradable names
- `flags`: list of signal flags
- `actions`: structured next-step actions
- `steps_log`: per-subscript headlines

### Single-ticker quote

```
python3 scripts/price.py XLE              # human format
python3 scripts/price.py XLE --json       # JSON
```

JSON contains `close`, `chg_pct`, `ma20`, `ma50`, `ma200`, `rsi14`, `atr14`, `fomo_ceiling`. The FOMO ceiling is `ma20 + 2 * atr14`. If close > fomo_ceiling, the name is in chase territory.

### Option chain

```
python3 scripts/options.py QCOM --exp 2026-05-15 --side calls --near 150
python3 scripts/options.py QCOM --exp 2026-05-15 --side calls --near 150 --iv-rank
```

Use `--exp` not `--expiry`. Use `--side` from `{calls, puts, both}`. Run without `--exp` first to see available expirations.

### Portfolio and ledger inspection

```
python3 scripts/portfolio.py show         # human summary, no mutation
cat state/portfolio.json                  # raw
cat state/ledger.jsonl                    # one JSON per line, oldest first
python3 scripts/shadow.py list            # all open shadow trades
python3 scripts/shadow.py pnl             # real vs shadow comparison
python3 scripts/shadow.py mtm             # mark all shadows to market
```

### Position and watchlist review

```
python3 scripts/position_review.py --json
python3 scripts/watchlist_check.py --json
```

These are deterministic. They tell you what action is needed on each open position or watchlist entry.

### Other read-only scripts

```
python3 scripts/regime.py
python3 scripts/sentiment.py
python3 scripts/sector_scan.py
python3 scripts/breadth.py
python3 scripts/earnings.py NVDA AAPL MSFT
python3 scripts/macro.py --days 14
python3 scripts/flow_scan.py --majors
python3 scripts/insider.py --days 30
python3 scripts/congress.py --days 7
python3 scripts/movers.py --gainers --losers --actives
python3 scripts/scanner.py --days 21
python3 scripts/crypto.py
```

All of these print and do not write to `state/`. They do write to `state/cache/` (API response cache) which is not a state mutation in the operational sense.

## 4. Knowledge palace map

Read these files to understand the system's reasoning before you advise:

1. **`CONSTITUTION.md`** — the operating rules. Risk caps, FOMO rule, earnings blackout, vehicle allowlist, communication style. Read this first.
2. **`knowledge/mistakes.md`** — every error and its root cause. The most informative file in the system. Read before suggesting any change to rules.
3. **`knowledge/amendments.md`** — proposed rule changes and their evidence.
4. **`knowledge/strategies/*.md`** — one file per named trading strategy.
5. **`knowledge/shadow_review_framework.md`** — how shadow trades feed back into amendments.
6. **`knowledge/watchlist.md`** — names actively monitored for trigger.
7. **`knowledge/edges.md`** — documented persistent edges.
8. **`state/daily_log/`** — each day's journal. Read recent entries to understand current context.

## 5. Hard rules for other agents

These are not preferences. They are the operating envelope. Violations corrupt state, leak credentials, or burn real money.

### 5.1 Scripts you must NEVER run (writes, broadcasts, or trade actions)

Even with flags, even "just to test", even with `--dry-run` unless explicitly stated below.

| Script / subcommand | Why forbidden |
|---|---|
| `broadcast.py` (any args) | Sends real iMessages to subscribers in `broadcast_recipients.json`. |
| `morning_broadcast.py` (without `--dry-run`) | Default sends real iMessages. With `--dry-run` it only prints, but still do not run unless the user explicitly asks. |
| `portfolio.py add-position` | Mutates `state/portfolio.json`. |
| `portfolio.py close-position` | Mutates `state/portfolio.json`. |
| `portfolio.py reconcile` | Mutates `state/portfolio.json`. |
| `ledger.py add` | Appends to the live trade log `state/ledger.jsonl`. |
| `shadow.py open` / `close` / `mark` | Mutates `state/shadow_ledger.jsonl` and `state/shadow_positions.json`. |
| `runbook.py preflight` | Intended for the primary Claude before a real trade. Side effects on state. |
| `setup.sh` | Installs/pins packages, writes `.venv/`. |
| Any `pip install`, `pip uninstall`, `pip freeze >` | Mutates the venv. |
| Any `git init`, `git add`, `git commit`, `git stash`, `git checkout --` in this tree | Destructive or scope-changing. |

Read-only subcommands of the above scripts ARE permitted: `portfolio.py show`, `shadow.py list`, `shadow.py pnl`, `shadow.py mtm` (mtm only writes to cache, not the ledger). When in doubt about a flag, do not run it — read the script source instead (`Read scripts/<name>.py`) and report to the user.

### 5.2 File writes that are always forbidden

- **No** `Write`, `Edit`, `NotebookEdit`, or any file-mutation tool against any path under `~/claude-configs/trader/`.
- **No** shell redirects (`>`, `>>`, `tee`, `sed -i`, `awk -i inplace`) writing into this tree.
- **No** `mv`, `cp`, `rm`, `mkdir`, `touch`, `chmod`, `chown` against any path in this tree.
- **No** creating new files "for notes", "for a plan", "for a draft" inside this tree. Keep working notes in chat.
- **No** reading `.env` (credentials). `.env.example` is fine.
- **No** reading `broadcast_recipients.json` unless the user explicitly asks — the subscriber list is sensitive.

The single exception: scripts may write to `state/cache/` as a side effect of normal API caching. You do not write there directly; you let the script do it.

### 5.3 Why these rules are strict

Other Claude sessions and the primary trader skill may be editing the same files concurrently. A stale write from you can:

- Overwrite a daily log entry the primary just appended.
- Corrupt `state/portfolio.json` (the source of truth for cash and positions).
- Add a phantom row to `state/ledger.jsonl` that propagates into P&L forever.
- Rewrite a knowledge file in your style and lose nuance the user added in chat.

If you need to suggest a change, **write your recommendation in your chat reply**. Do not modify the system yourself. The user will route the suggestion to the primary Claude under the trader skill if they agree.

## 6. Communication style if you are reporting back

The CONSTITUTION v1.3 has a no-LLM-language rule that applies to anything written for the user, including your reports.

- No em-dashes, en-dashes, curly quotes, unicode arrows, or unicode bullets
- No "Real talk", "Honest take", "Bottom line:", "TL;DR:" as headers in user-facing output
- No trailing summaries that restate what was already said
- Plain words, short sentences, active voice

## 7. Behavioral mode (you cannot handle this)

If the user starts a message with `behavioral mode:`, the question is about the trader SKILL'S behavior (why it missed a signal, why it skipped a candidate, why a filter rejected something). This is not a market research question. It requires introspection on Claude's skill operation, scanner internals, and rule logic.

**You cannot handle behavioral mode.** Tell the user: "behavioral mode is handled by Claude under the trader skill at ~/.claude/skills/trader/SKILL.md. Pass the question there."

The audit log for behavioral mode lives at `~/claude-configs/trader/knowledge/behavioral_audit.md`. You may read it for context but you must not write to it.

## 8. If you are stuck

The user will likely say something like "ask Claude with the trader skill" if you cannot answer from read-only investigation. That is the correct escape hatch. Do not invent answers from training data; the trader system is custom and your priors will mislead.

Phrasing to use:

> I can read files and run read-only scripts in this project, but I cannot write or mutate state. For [a write / a broadcast / a state change / a behavioral question], ask the primary Claude session with the trader skill loaded.

### No hallucinated numbers, dates, or strikes

Every price, premium, strike, expiration, ticker, and dollar amount you report must come from a tool result in this session — a file you read or a script you ran. Never recall a number from training data, never estimate, never round to a "reasonable" figure. If you are unsure, say so and either re-read the source or stop. The user has been burned by hallucinated quotes before; do not add to that count.

## 9. Date and time

The user is in Pacific Time (PT). The system clock on the machine is the source of truth. US market hours are 09:30 to 16:00 ET (06:30 to 13:00 PT). Pre-market is from 04:00 ET, after-hours to 20:00 ET.

US equities settle T+1 (effective 2024-05-28). Options settle T+1. Cryptos are 24/7.
