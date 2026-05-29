# Trader System — Setup Guide

**Who reads this:** a Claude instance on a fresh machine setting up the trader system from scratch.

**What this sets up:**
- The `/trader` Claude Code skill (morning briefs, research, portfolio tracking)
- The autonomous trading bot with a local dashboard at `http://localhost:8787`
- Loop-based scheduling inside the Claude Code session (no system cron needed)

**What this does NOT set up:** trader-site (separate repo, separate deploy), hourly broadcast
iMessage cron (that requires persistent machine uptime; use loop instead).

**How to run this guide:** read it fully first. Then work through it top to bottom with the user.
Never skip a step. Never ask the user to manually edit a file — write everything programmatically.

---

## GITIGNORED FILES — what they are and where they live

The trader repo commits all code but gitignores all secrets and personal state. On a new machine
none of these exist yet. You will create each one during setup by asking the user for the data.

| File | What it contains | How to recreate |
|------|-----------------|-----------------|
| `.env` | API keys for all data providers and the brokerage | Collect keys from user in Phase 1, Group C |
| `state/portfolio.json` | Open positions the user is tracking | Ask user in Phase 1, Group D |
| `state/ledger.jsonl` | Append-only trade history | Start empty on fresh machine |
| `state/shadow_ledger.jsonl` | Hypothetical (paper) trade history | Start empty |
| `state/regret_ledger.jsonl` | Rejected candidates tracked for review | Start empty |
| `state/research_log.jsonl` | Research session log | Start empty |
| `state/shadow_positions.json` | Open shadow-book positions | Start empty |
| `state/watchlist.json` | Active watchlist with thesis per ticker | Start empty |
| `state/alerts.json` | Active price/level alerts | Start empty |
| `state/last_universe_review.txt` | Date of last universe review | Write `2000-01` (forces review on first brief) |
| `state/cache/` | API response cache | Created automatically by scripts on first run |
| `broadcast_recipients.json` | Phone numbers for iMessage alerts | Ask user in Phase 1, Group E |
| `public_com_autonomous_trading/config.json` | Bot config (account ID, sizing, kill-switch) | Already committed; update account_id and equity |
| `public_com_autonomous_trading/state/` | Bot trade log, hypotheses, approvals | Initialize fresh in Phase 2, Step 10 |
| `public_com_autonomous_trading/state/temperament.json` | Bot's tunable temperament (persona dials) | Auto-defaults from code; created when you save on the Controls tab |
| `public_com_autonomous_trading/state/mind/` | The bot's MIND: `identity.md` (self-model), `memory/` (six indexed conviction buckets + `index.json`), `agenda.json` (open thoughts + held tensions), `monologue/` (per-run ruminations), `directive.json` (the standing directive, set via the mind, no longer a Controls form), `controls_seen.json` (control-change diff), `playbook.md` (the live mind-view the dashboard shows) | Initialized in Phase 2, Step 10: seed `identity.md` from the committed `identity.md.example`; the rest self-create |
| `public_com_autonomous_trading/state/mind/agent_controls.json` (+ `agent_runtime.json`, `install_queue.json`, `installed_plugins.json`) | Agent controls: per-agent enable/disable + cadence (dashboard-written), last-convened runtime (mind-written), and the marketplace install queue + installed-plugin list | Auto-create empty: `{"agents":{}}`, `{"agents":{}}`, `{"queue":[]}`, `{"plugins":[]}` |

---

## PHASE 1: Gather All Inputs

Work through each group in order. Ask the questions, wait for answers, record everything. Do not
touch any files in this phase. At the end, confirm with the user before executing.

---

### Group A: Check the machine

Run these commands silently:

```bash
echo "HOME: $HOME"
uname -s
python3 --version 2>&1 || echo "python3: NOT FOUND"
python3.11 --version 2>&1 || python3.10 --version 2>&1 || echo "no 3.10/3.11"
git --version 2>&1 || echo "git: NOT FOUND"
claude --version 2>&1 || echo "claude: NOT FOUND"
gh --version 2>&1 | head -1 || echo "gh: NOT FOUND"
gh auth status 2>&1 | head -1 || echo "gh: NOT AUTHENTICATED"
```

Report what you found and flag any blockers:

- **Python < 3.9 or not found**: tell the user they need Python 3.9 or higher before continuing.
  On macOS: `brew install python@3.11`. On Ubuntu/Debian: `sudo apt install python3.11`.
  Do not proceed until Python 3.9+ is confirmed.

- **git not found**: on macOS: `brew install git` or install Xcode CLI tools via
  `xcode-select --install`. On Linux: `sudo apt install git`.

- **claude not found**: Claude Code CLI is not installed. Tell the user to install it from
  https://claude.ai/code and log in before continuing. This entire setup runs inside Claude Code,
  so without it nothing works.

- **gh not found or not authenticated**: the GitHub CLI is needed for the plugin marketplace
  (installing and updating agents on the dashboard Agents tab). Install: macOS `brew install gh`;
  Debian/Ubuntu `sudo apt install gh`; else https://cli.github.com. Then run `gh auth login`
  (choose GitHub.com over HTTPS) and verify with `gh auth status`. This is not a hard blocker for
  core trading, but the marketplace will not work without an authenticated `gh`.

Record: `HOME_DIR` (the value of $HOME), `OS` (Darwin = macOS, Linux = Linux),
`PYTHON_BIN` (the python3 binary path that is 3.9+, e.g. `/usr/bin/python3` or
`/opt/homebrew/bin/python3.11`).

---

### Group B: Get the code

Ask the user:

> "Do you have the trader directory already on this machine, or are you transferring it from
> another machine?
>
> (a) Already here — tell me the path
> (b) Transferring now — I'll wait while you copy it
> (c) I have a zip or git bundle to unpack
>
> The trader directory should contain: CONSTITUTION.md, scripts/, knowledge/,
> public_com_autonomous_trading/"

If the user says already here, run:
```bash
ls <their_path>/CONSTITUTION.md 2>/dev/null && echo "found" || echo "not found"
```

If found, set `TRADER_DIR` to their path. If not found, help them locate it.

If transferring, suggest:
```
rsync -av user@oldmachine:~/claude-configs/trader/ ~/claude-configs/trader/
```
Wait for them to confirm it is done, then verify:
```bash
ls ~/claude-configs/trader/CONSTITUTION.md && echo "found"
```

If a zip or bundle:
```bash
mkdir -p ~/claude-configs
# For zip:
unzip trader.zip -d ~/claude-configs/trader/
# For git bundle:
git clone trader.bundle ~/claude-configs/trader/
```

Once the directory is confirmed, set `TRADER_DIR=~/claude-configs/trader`.

**Skills check.** Two skills ship IN this repo under `skills/` and get linked into
`~/.claude/skills/` during setup (Step 6):
- `skills/trader/`            -> `/trader` (research analyst)
- `skills/trader-autonomous/` -> `/trader-autonomous` (the autonomous run)

```bash
ls -l ~/.claude/skills/trader ~/.claude/skills/trader-autonomous 2>/dev/null || echo "NOT LINKED YET (do Step 6)"
```

On a fresh machine they will not be linked yet, which is expected: Step 6 creates the symlinks. The
SKILL.md files are already in the repo, so there is nothing to transfer from another machine. Record:
`SKILLS_OK` (true/false).

---

### Group C: API keys

Tell the user:

> "I'll ask for your API keys one at a time. Each takes 2-3 minutes to get if you don't have it
> yet — I'll give you the exact signup link for each. Your keys will be written directly to a
> local .env file that is gitignored and chmod 600. I will never echo them back.
>
> Type the key and press enter, or type `skip` to leave it blank. Required keys are needed for
> core features; optional keys improve coverage."

Ask each key below in sequence. After the user responds to one, ask the next.

---

**1. PUBLIC_BROKER_API** — REQUIRED for market data and trading

What it does: real-time quotes, options chains, account positions, and order placement on
public.com. This is the single most important key. Without it, nearly every script fails.

How to get it:
1. Sign up at https://public.com if you don't have an account
2. Log in, go to Account Settings
3. Find "API" or "Individual Trader API Program"
4. Generate a secret key (32 characters)

> "Paste your Public.com API secret key:"

---

**2. PUBLIC_COM_ACCOUNT_ID** — REQUIRED, but DO NOT ask the user for this. Derive it from the secret.

What it does: identifies which brokerage account to trade. All API endpoints are account-scoped.

The account ID is discoverable from the token, so do not make the user hunt for it. Using the
secret collected in step 1, mint a short-lived JWT, list the profile's trading accounts, and pick
the one whose `tradePermissions` is `BUY_AND_SELL`. (`GET /trading/account` needs only the JWT, no
account ID, so this works before anything else is configured.)

```bash
SECRET='<the PUBLIC_BROKER_API secret from step 1>'
JWT=$(curl -s -X POST https://api.public.com/userapiauthservice/personal/access-tokens \
  -H "Content-Type: application/json" \
  -d "{\"validityInMinutes\":30,\"secret\":\"$SECRET\"}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin).get('accessToken',''))")
curl -s https://api.public.com/userapigateway/trading/account \
  -H "Authorization: Bearer $JWT" \
  | python3 -c "import sys,json;[print(a['accountId'],a.get('accountType'),a.get('tradePermissions')) for a in json.load(sys.stdin).get('accounts',[]) if a.get('tradePermissions')=='BUY_AND_SELL']"
```

Use the printed `accountId` as `PUBLIC_COM_ACCOUNT_ID`. If exactly one BUY_AND_SELL account prints,
use it silently. If more than one prints, show the list and ask which to use. If none prints, the
secret is wrong or the account has no trading permission, so surface that and re-collect the secret.

---

**3. FINNHUB_API_KEY** — REQUIRED

What it does: news headlines, earnings calendar, insider sentiment, analyst recommendations.
Sign up (free): https://finnhub.io/dashboard — click "Get free API key". Free tier is 60 req/min.

> "Paste your Finnhub API key:"

---

**4. FMP_API_KEY** — REQUIRED

What it does: economic calendar (Fed meetings, CPI, jobs), financial ratios, DCF data.
Sign up (free): https://site.financialmodelingprep.com/developer/docs — "Get your API key".
Free tier: 250 req/day.

> "Paste your Financial Modeling Prep (FMP) API key:"

---

**5. ALPHAVANTAGE_API_KEY** — recommended

What it does: fundamentals and technical indicators as a fallback data source.
Sign up (free): https://www.alphavantage.co/support/#api-key — instant, no credit card.
Free tier: 25 req/day. Scripts cache aggressively so this covers most use cases.

> "Paste your AlphaVantage API key (or `skip`):"

---

**6. COINGECKO_DEMO_API_KEY** — recommended if you trade crypto

What it does: crypto prices, BTC dominance, trending coins. Without it the rate limit is ~5
req/min which causes timeouts during any crypto scan.
Sign up (free): https://www.coingecko.com/en/developers/dashboard — get the "Demo" key, not Pro.

> "Paste your CoinGecko Demo API key (or `skip`):"

---

**7. QUIVER_API_KEY** — recommended

What it does: congressional trades and insider sentiment data. Cleaner and more reliable than
scraping Capitol Trades directly.
Sign up (free): https://api.quiverquant.com/account/register

> "Paste your Quiver Quant API key (or `skip`):"

---

**8. FRED_API_KEY** — optional

What it does: US macro data — CPI history, jobs numbers, interest rate history.
A keyless fallback exists so this is not blocking.
Sign up (free): https://fredaccount.stlouisfed.org/apikey — unlimited free tier, instant.

> "Paste your FRED API key (or `skip`):"

---

**9. MARKETAUX_API_KEY** — optional

What it does: pre-scored financial news with sentiment labels. Supplements Finnhub on busy days.
Sign up (free): https://www.marketaux.com/account/register — 100 req/day free.

> "Paste your Marketaux API key (or `skip`):"

---

**10. TIINGO_API_KEY** — optional

What it does: clean stock price data and news, alternate to yfinance.
Sign up (free): https://www.tiingo.com/account/api/token — 50 req/hr free.

> "Paste your Tiingo API key (or `skip`):"

---

### Group D: Robinhood / personal portfolio

Ask:

> "Do you want to load your current Robinhood portfolio so the morning brief tracks your actual
> holdings and gives you personalized stop/target/exit suggestions?
>
> If yes, tell me each position you currently hold:
> - Ticker (e.g. NVDA)
> - Number of shares
> - Your average cost per share
> - Your stop price (the price where you'd cut the loss)
> - Your target price (where you'd take profit)
> - Your thesis in one sentence (why you bought it)
>
> List them one per line like: `NVDA 10 shares @ $875 stop $820 target $950 bought on earnings beat`
>
> Type `skip` to set up an empty portfolio and add positions later via the /trader skill."

If the user provides positions, record them as `POSITIONS` array for Phase 2.
If skip, record `POSITIONS = []`.

---

### Group E: Broadcast recipients (optional, iMessage, macOS only)

Skip this group entirely if `OS = Linux`.

Ask:

> "The system can send morning trade ideas and intraday alerts to phone numbers via iMessage
> (macOS only). Do you want to set this up?
>
> At minimum, add yourself so you get alerts on your phone. Subscribers get generic buy ideas
> only — they never see your personal positions or account details.
>
> Type `yes` to add recipients, or `skip`:"

If yes:

> "Add yourself first. What is your phone number in E.164 format (e.g. +12125551234)?
> You'll be set up as `primary` — meaning you get position-specific alerts and intraday heads-up
> on your holdings."

Record as `RECIPIENT_SELF` (name: "Me", phone, role: primary, hourly_enabled: true).

> "Any other recipients (subscribers who should get generic buy ideas)?
> List them as: `Name, +phone`
> Or type `done` if just yourself."

Record as `RECIPIENTS_EXTRA` array (role: subscriber, hourly_enabled: false for each).

---

### Group F: Autonomous trading bot (optional)

Ask:

> "The autonomous bot trades a real public.com account on its own schedule — it analyzes
> candidates, manages positions, and places orders (with your approval or in live mode).
> It runs as a reasoning mind, not a fixed script: it carries memory and convictions across
> runs, holds an inner monologue, debates the hard calls, and you can peek into what it's
> thinking on the Playbook.
>
> It maintains itself: it makes and logs its own ordinary changes (its watchlist, its memory,
> even its own scanner logic) without waiting on you, and only escalates changes to its control
> knobs and risk guardrails (position caps, stops, the kill-switch, arming, the persona dials,
> your directives) to the Decisions tab for approval. Anything you approve there, it implements
> on its next run. You just monitor Activity, Decisions, Evolution, and the Playbook. The full
> governance rule is in public_com_autonomous_trading/SELF_MAINTENANCE.md (and
> MIND_MAINTENANCE.md for how it keeps the Playbook fresh and honest).
>
> To use it you need:
> - A public.com cash account for the bot (ideally a separate one, if you have more than one)
> - Your Public.com API key (already collected above)
>
> Do you want to set up autonomous trading? Type `yes` or `skip`:"

If yes:

Do NOT ask the user for the bot's account ID. Reuse the step-2 derivation (mint a JWT from the
secret, then `GET https://api.public.com/userapigateway/trading/account`) to list the
`BUY_AND_SELL` accounts. If exactly one prints, use it as `BOT_ACCOUNT_ID` (the bot trades the same
account as research). If more than one prints, show the list and ask which to dedicate to the bot.
Record the chosen `accountId` as `BOT_ACCOUNT_ID`.

> "What is the starting cash balance in that account (in USD)? I'll use this to calculate
> position sizing and the kill-switch threshold."

Record: `BOT_STARTING_EQUITY` (number).

Tell the user: "The bot will start in dry-run mode — it will reason about trades and log exactly
what it would do, but place no real orders. Once you've reviewed a few dry-run sessions, you can
flip it to live mode by changing one line in config.json."

---

### Group F2: How you want the bot to operate (optional)

Ask this group ONLY if the user said yes to autonomous trading (Group F). Every question is
optional. If the user has no opinion, record the default and move on. Say up front:

> "A few optional questions to shape how the bot thinks and what it watches. Skip any and I will
> use a sensible default. You can change all of these later on the dashboard or just by telling the
> bot, so nothing here is permanent."

**1. Your goal.**
> "In a sentence or two, what do you want this bot to do for you? For example: grow aggressively
> with asymmetric bets; steady compounding while protecting capital; or just surface ideas I would
> miss. Type `skip` to use the default."

Record `PREF_GOAL`. Default: `Make money with calculated risk: asymmetric setups, sized sensibly, with a stop on every position.`

**2. Risk posture.**
> "How much risk should it take?
>   (a) Cautious: smaller size, only high-conviction setups, very patient
>   (b) Balanced: calculated risk, asymmetric upside, a stop on everything (recommended)
>   (c) Aggressive: bolder, sizes up on conviction, more active
> Type a, b, c, or `skip`."

Record `PREF_POSTURE` (cautious, balanced, or aggressive). Default: balanced. It maps to the
temperament dials in Step 10d via these presets (each 0 to 100):

| Dial       | Cautious | Balanced | Aggressive |
|------------|----------|----------|------------|
| boldness   | 40       | 60       | 80         |
| skepticism | 75       | 65       | 50         |
| patience   | 75       | 65       | 45         |
| greed_fear | 40       | 55       | 70         |
| curiosity  | 65       | 70       | 80         |
| bluntness  | 80       | 80       | 80         |

(greed_fear: lower leans toward protecting gains, higher leans toward capturing upside.)

**3. Temperament fine-tune (advanced, optional).**
> "Want to set its personality directly instead? Six dials, 0 to 100. Tell me only the ones you
> care about; the rest follow your risk posture:
>   Boldness: how readily it commits
>   Skepticism: how hard it doubts a setup
>   Patience: how long it waits for the right pitch
>   Greed vs Fear: capturing more upside vs protecting gains
>   Curiosity: how eagerly it hunts novel ideas
>   Bluntness: how direct it is with you
> Type `skip` to use the posture preset."

Record any overrides in `PREF_DIALS` (dial name to a 0-100 value). Default: none.

**4. Tickers to avoid.**
> "Any symbols it must NEVER trade, in any form? The most common reason is your employer's stock
> (employee trading rules), or names you simply do not want it touching. List symbols separated by
> spaces, or type `skip`."

Record `PREF_RESTRICTED` (list of uppercase symbols). Default: empty.

**5. Tickers to follow.**
> "Any names you want it watching from day one? It builds its own thesis and watches for an entry,
> so you do not need prices or targets, just the symbols and a word on why if you like (for example
> `CCJ uranium` or `ASML AI pick-and-shovel`). One per line, or type `skip`."

Record `PREF_FOLLOW` (a list of ticker plus a short why). Default: empty.

**6. Sectors or themes to watch.**
> "Any sectors or themes you care about? For example AI infrastructure, uranium and nuclear,
> biotech, energy, defense, or crypto. I will bias the bot's attention there and seed a leading name
> or two to watch. List them, or type `skip`."

Record `PREF_SECTORS` (list of sector phrases). Default: empty (broad market).

---

### Group G: Scheduling with /loop

Both loops are DYNAMIC and self-paced (no fixed interval): the assistant decides each tick how long
to wait, stretching to hours when idle and tightening when something is live. Ask the user:

> "Both the research brief and the autonomous bot self-pace by market hours, so you do not set a
> fixed timer. Do you have a cadence preference, or should I use sensible defaults?
>
> Defaults (if you have no preference):
>  - Autonomous bot (/trader-autonomous): about every 30 minutes during market hours, much less
>    often (a few hours) off-hours, and quicker (about 10-15 min) when it is managing a position
>    or sitting near a trigger.
>  - Research brief (/trader): about every 2 hours during market hours, much less often off-hours.
>  Both flex faster or slower with the situation; these are the resting cadence, not a fixed timer.
>
> Type a preference (for example 'bot every 15 min', 'brief hourly'), or `default`, or `skip` to
> skip the loops."

Record `LOOP_CADENCE` (the user's words, or "default"). It is woven into the loop prompts in
Step 13 so the assistant paces to it.

---

### Group H: Confirmation before executing

Summarize everything collected:

> "Here is what I am about to set up. Review and type `go` to proceed or tell me what to change.
>
> Machine: `<OS>`, home at `<HOME_DIR>`
> Code: trader directory at `<TRADER_DIR>`, skill file: `<present | will install>`
>
> API keys provided: `<list keys NOT skipped>`
> API keys skipped: `<list>` — note: `<which features degrade>`
>
> Portfolio: `<N positions loaded | empty, add later>`
> Broadcast: `<N recipients | skipped>`
> Autonomous bot: `<yes, account <BOT_ACCOUNT_ID>, $<BOT_STARTING_EQUITY> | skipped>`
> Bot preferences: posture `<PREF_POSTURE>`; avoid `<PREF_RESTRICTED or none>`; following `<PREF_FOLLOW tickers or none>`; sectors `<PREF_SECTORS or none>`
>
> Scheduling (dynamic, self-paced loops):
>   Cadence: `<LOOP_CADENCE>` (default: bot ~30m, brief ~2h during market hours; much sparser off-hours; flexing with the situation) | skipped
>
> Nothing has been changed yet. Type `go` to start."

---

## PHASE 2: Execute

Run every step in order. Each step ends with a verification check. If a check fails, stop and
fix before continuing. Never skip a verification.

---

### Step 1: Python venv

```bash
cd ~/claude-configs/trader/scripts
bash setup.sh
```

This creates `scripts/.venv/` and installs requirements.txt. Takes 1-3 minutes cold.

Verify:
```bash
~/claude-configs/trader/scripts/.venv/bin/python3 -c "import yfinance, pandas; print('venv: ok')"
```

Set the canonical interpreter path for the rest of setup:
```
TPY = ~/claude-configs/trader/scripts/.venv/bin/python3
```

Use `$TPY` for every script call below. Never use the system `python3` — it lacks the deps and
fails silently.

---

### Step 2: Git init and gitignore

Check if git is already initialized:
```bash
git -C ~/claude-configs/trader rev-parse --git-dir 2>/dev/null && echo "git: already init" || echo "git: needs init"
```

If needs init:
```bash
cd ~/claude-configs/trader
git init
git add .
git commit -m "Initial commit"
```

Verify the .gitignore is protecting sensitive files:
```bash
cd ~/claude-configs/trader
git check-ignore -v .env state/portfolio.json broadcast_recipients.json
```

Each line should show `.gitignore:N:<pattern>  <file>`. If any file is NOT ignored, add it to
`.gitignore` with the Edit tool before continuing.

The gitignore must cover at minimum:
```
.env
*.env
!.env.example
state/
broadcast_recipients.json
scripts/.venv/
__pycache__/
*.pyc
.DS_Store
```

---

### Step 3: Write the .env file

Use the Write tool to write `~/claude-configs/trader/.env` with the keys collected in Group C.
For keys the user provided: write `KEY=value`. For skipped keys: write `KEY=` (blank).

Write order (copy from .env.example structure):
```
PUBLIC_BROKER_API=<value>
PUBLIC_COM_ACCOUNT_ID=<derived BUY_AND_SELL accountId from step 2>
ALPHAVANTAGE_API_KEY=<value>
FINNHUB_API_KEY=<value>
FMP_API_KEY=<value>
MASSIVE_API_KEY=
FRED_API_KEY=<value>
COINGECKO_DEMO_API_KEY=<value>
QUIVER_API_KEY=<value>
MARKETAUX_API_KEY=<value>
TIINGO_API_KEY=<value>
```

After writing:
```bash
chmod 600 ~/claude-configs/trader/.env
```

Verify keys loaded (shows present/absent counts, never values):
```bash
cd ~/claude-configs/trader && set -a && source .env && set +a
$TPY scripts/_apikeys.py
```

---

### Step 4: Initialize state directory

```bash
cd ~/claude-configs/trader
mkdir -p state/daily_log state/cache state/broadcasts state/portfolios state/backtest
echo '{"user_positions": [], "last_updated": null}' > state/portfolio.json
touch state/ledger.jsonl state/shadow_ledger.jsonl state/regret_ledger.jsonl state/research_log.jsonl
echo '{"open_positions": [], "cumulative_pnl": 0, "last_updated": null}' > state/shadow_positions.json
echo '{"active": [], "archived": []}' > state/watchlist.json
echo '[]' > state/alerts.json
echo "2000-01" > state/last_universe_review.txt
```

Verify:
```bash
ls ~/claude-configs/trader/state/portfolio.json && echo "state: ok"
```

---

### Step 5: Load portfolio positions (if provided in Group D)

For each position the user listed, run:

```bash
cd ~/claude-configs/trader && set -a && source .env && set +a
$TPY scripts/portfolio.py add-position \
  --ticker TICKER \
  --kind stock \
  --qty N \
  --entry COST_PER_SHARE \
  --stop STOP_PRICE \
  --target TARGET_PRICE
```

Repeat for each position. After all positions are loaded:
```bash
$TPY scripts/portfolio.py show
```

Should list every position. Confirm with the user that it looks right.

---

### Step 6: Install the /trader and /trader-autonomous skills

Skip if SKILLS_OK = true from Group B. Both skill files already live in this repo, so installing
them is just symlinking the two repo dirs into `~/.claude/skills/`:

```bash
mkdir -p ~/.claude/skills
for s in trader trader-autonomous; do
  src="$HOME/claude-configs/trader/skills/$s"; dst="$HOME/.claude/skills/$s"
  if [ -L "$dst" ]; then echo "  $s: already linked, skipping"
  elif [ -e "$dst" ]; then echo "  $s: a real (non-symlink) path exists, leaving it untouched"
  else ln -s "$src" "$dst" && echo "  $s: linked"; fi
done
```

This is idempotent: re-running skips skills that are already linked and never clobbers a real
(non-symlink) path from an older install. To switch such a path to the repo-linked skill, remove or
rename it yourself first, then re-run.

Verify both resolve into the repo:
```bash
ls -l ~/.claude/skills/trader ~/.claude/skills/trader-autonomous
wc -l ~/.claude/skills/trader/SKILL.md ~/.claude/skills/trader-autonomous/SKILL.md
```

Restart Claude Code so it picks up the skills. `/trader` is the research analyst; `/trader-autonomous`
is the autonomous run.

**Publish URL (optional, research skill).** If you publish the market-watch site, set
`MARKET_WATCH_URL` in `.env` to your own GitHub Pages URL (used only in the publish success message;
placeholder is in `.env.example`).

**Schedule the runs (optional).** Both the research brief and the autonomous run fire from a
DYNAMIC, self-paced `/loop` in a kept-open session (not system cron); they are set up in Step 13.
The loop takes no fixed interval. The assistant paces each tick by market hours and the live
situation, stretching to hours when idle and tightening when a position or trigger is active.

---

### Step 7: Configure Claude Code permissions

Read existing settings:
```bash
cat ~/.claude/settings.local.json 2>/dev/null || echo "{}"
```

Merge the following into `permissions.allow` (preserve any existing entries). Use the Edit tool:

```json
"Bash(~/claude-configs/trader/scripts/.venv/bin/python3 *)",
"Bash(cd ~/claude-configs/trader && ./scripts/.venv/bin/python3 scripts/* *)",
"Bash(bash ~/claude-configs/trader/scripts/*)",
"Bash(chmod 600 ~/claude-configs/trader/.env)",
"Bash(python3 ~/claude-configs/*)",
"Bash(~/claude-configs/trader/scripts/.venv/bin/pip install *)",
"WebSearch"
```

If `~/.claude/settings.local.json` does not exist, create it:
```json
{
  "permissions": {
    "allow": [
      "Bash(~/claude-configs/trader/scripts/.venv/bin/python3 *)",
      "Bash(cd ~/claude-configs/trader && ./scripts/.venv/bin/python3 scripts/* *)",
      "Bash(bash ~/claude-configs/trader/scripts/*)",
      "Bash(chmod 600 ~/claude-configs/trader/.env)",
      "Bash(python3 ~/claude-configs/*)",
      "Bash(~/claude-configs/trader/scripts/.venv/bin/pip install *)",
      "WebSearch"
    ]
  }
}
```

---

### Step 8: Write broadcast_recipients.json

If the user skipped Group E, write an empty list:
```json
{"_comment": "Add recipients here. Phone must be E.164 (+12125551234). portfolio_id: primary for yourself, null for subscribers.", "recipients": []}
```

If the user provided recipients, write the full JSON. Schema per entry:

```json
{
  "name": "Me",
  "phone": "+12125551234",
  "active": true,
  "portfolio_id": "primary",
  "hourly_enabled": true
}
```

For subscribers (non-primary): `"portfolio_id": null`, `"hourly_enabled": false`.

Use the Write tool to write `~/claude-configs/trader/broadcast_recipients.json`.

Verify:
```bash
python3 -m json.tool ~/claude-configs/trader/broadcast_recipients.json > /dev/null && echo "recipients: ok"
```

---

### Step 9: Smoke test — brief and quote

```bash
cd ~/claude-configs/trader && set -a && source .env && set +a
$TPY scripts/brief.py status
```

Should return a JSON object with a `headline` key. Empty `candidates` is normal on first run
(scanner cache is cold).

```bash
$TPY scripts/price.py SPY --json | python3 -m json.tool | grep '"close"'
```

Should return a close price for SPY. If this fails with a 401/403, the PUBLIC_BROKER_API key is
wrong or expired. If it returns no data, the API may need a moment to authenticate — retry once.

---

### Step 10: Set up autonomous trading bot (if Group F = yes)

Skip this step entirely if the user said no to autonomous trading.

#### 10a. Update config.json

Read the current config:
```bash
cat ~/claude-configs/trader/public_com_autonomous_trading/config.json
```

Update these fields using the Edit tool:
- `account_id`: set to `BOT_ACCOUNT_ID`
- `starting_equity_usd`: set to `BOT_STARTING_EQUITY`
- `enabled`: keep as `false` (dry-run to start)
- `risk.kill_switch_drawdown_usd`: set to `round(BOT_STARTING_EQUITY * 0.20)` (20% drawdown limit)
- `risk.max_position_usd`: set to `round(BOT_STARTING_EQUITY * 0.25)` (25% concentration cap)
- `risk.min_position_usd`: set to `max(25, round(BOT_STARTING_EQUITY * 0.05))` (5% floor or $25)

#### 10b. Initialize bot state

```bash
mkdir -p ~/claude-configs/trader/public_com_autonomous_trading/state
mkdir -p ~/claude-configs/trader/public_com_autonomous_trading/state/history

touch ~/claude-configs/trader/public_com_autonomous_trading/state/trade_log.jsonl
touch ~/claude-configs/trader/public_com_autonomous_trading/state/equity_history.jsonl

# These shapes MUST match each module's load() default exactly (verified against code).
echo '{"settled_cash": 0.0, "pending": [], "seeded": false}' \
  > ~/claude-configs/trader/public_com_autonomous_trading/state/settlement.json
echo '{}' \
  > ~/claude-configs/trader/public_com_autonomous_trading/state/hypotheses.json
echo '[]' \
  > ~/claude-configs/trader/public_com_autonomous_trading/state/approvals.json
echo '[]' \
  > ~/claude-configs/trader/public_com_autonomous_trading/state/autonomous_watchlist.json
echo '{"updated": null, "watching": []}' \
  > ~/claude-configs/trader/public_com_autonomous_trading/state/watching.json
echo '{"changes": [], "rejected": []}' \
  > ~/claude-configs/trader/public_com_autonomous_trading/state/change_log.json
echo '{"working": [], "not_working": []}' \
  > ~/claude-configs/trader/public_com_autonomous_trading/state/reflections.json

# The autonomous MIND: six-bucket indexed memory, the open-thought agenda, the
# self-model, and the per-run monologue. memory.py / agenda.py self-create their files,
# but seed the dirs + index + agenda + identity here so the first run starts clean.
mkdir -p ~/claude-configs/trader/public_com_autonomous_trading/state/mind/memory/{tickers,patterns,decisions,self,market_regime,sources}
mkdir -p ~/claude-configs/trader/public_com_autonomous_trading/state/mind/monologue
echo '{"cards": []}' \
  > ~/claude-configs/trader/public_com_autonomous_trading/state/mind/memory/index.json
echo '[]' \
  > ~/claude-configs/trader/public_com_autonomous_trading/state/mind/agenda.json
cp ~/claude-configs/trader/public_com_autonomous_trading/identity.md.example \
   ~/claude-configs/trader/public_com_autonomous_trading/state/mind/identity.md

# risk_state.json is intentionally NOT created here: risk_state.py seeds it from
# config.json on first run (a versioned stack of risk params). Hand-writing it would
# corrupt that shape. These also self-initialize, no init needed: temperament.json
# (code defaults until you save on Controls), state/mind/playbook.md (the bot writes
# the live mind-view each run), state/mind/directive.json (written when you save a
# directive on Controls), state/mind/controls_seen.json (written each run to diff the
# control changes), and each memory body file under state/mind/memory/<bucket>/.
```

#### 10c. Test dry run

```bash
cd ~/claude-configs/trader && set -a && source .env && set +a
$TPY public_com_autonomous_trading/run_autonomous.py context 2>&1 | python3 -m json.tool | grep -E '"action"|"armed"|"crypto_open"'
```

Should return JSON with `action` and `gate` keys, plus the mind-state the bot orients on:
`mind` (identity, memory index, agenda, open tensions), `controls_diff`, and `regime`. With
`enabled: false`, it will show dry-run mode and place no orders. If it crashes, check the error
and verify the account ID and API key.

---

#### 10d. Apply trading preferences (from Group F2)

Skip any sub-part the user skipped (the defaults are already sensible). Run from
`~/claude-configs/trader/public_com_autonomous_trading` with `$TPY`, so the bare script names below
resolve:
```bash
cd ~/claude-configs/trader/public_com_autonomous_trading
```

**Temperament.** Resolve the six dials from `PREF_POSTURE` using the preset table in Group F2, then
apply any `PREF_DIALS` overrides on top. `set` takes one dial at a time as positional `key value`
(keys: boldness, skepticism, patience, greed_fear, curiosity, bluntness; values 0 to 100). Confirm
with `$TPY temperament.py set --help`, then set each dial, for example (Balanced):
```bash
$TPY temperament.py set boldness 60
$TPY temperament.py set skepticism 65
$TPY temperament.py set patience 65
$TPY temperament.py set greed_fear 55
$TPY temperament.py set curiosity 70
$TPY temperament.py set bluntness 80
```
This writes `state/temperament.json`.

**Directive (your goal, plus any sector emphasis).** Write a one-line standing directive from
`PREF_GOAL` (append a sector emphasis if the user named sectors) to `state/mind/directive.json`:
```bash
$TPY - <<'PY'
import json, datetime
text = "PREF_GOAL goes here"  # if PREF_SECTORS: append " Bias attention toward: <sectors>."
json.dump({"text": text, "updated": datetime.datetime.now().astimezone().isoformat(timespec="seconds")},
          open("state/mind/directive.json", "w"), indent=2)
print("directive:", text)
PY
```

**Tickers to avoid.** With the Edit tool, set `restricted_tickers` in `config.json` (gitignored)
from `PREF_RESTRICTED`. The key already exists (default `[]`). For example, if the user works at a
public company, put its ticker here: `"restricted_tickers": ["<your employer ticker>"]`. This is the
per-machine avoid list the trading ban reads from.

**Seed the watchlist (from follows and sectors).** These are monitor entries: no target or stop
needed, the bot establishes the thesis and a level on its first runs. Confirm flags with
`$TPY autonomous_watchlist.py watch --help`, then:
- For each `PREF_FOLLOW` ticker:
  ```bash
  $TPY autonomous_watchlist.py watch TICKER --for "owner follows this; build a thesis and an entry level" --notes "seeded at setup: <why>"
  ```
- For each `PREF_SECTORS` theme: pick one or two leading, liquid names or a sector ETF and seed each
  the same way with `--for "owner wants the <sector> theme watched"`. Use your judgment for the
  representative names; one or two per sector is plenty.

Verify:
```bash
$TPY temperament.py show
$TPY -c "import json; print('directive:', json.load(open('state/mind/directive.json'))['text'])"
$TPY -c "import json; print('restricted:', json.load(open('config.json')).get('restricted_tickers'))"
$TPY autonomous_watchlist.py list | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d), 'watchlist entries:', ', '.join(e['ticker'] for e in d))"
```

#### 10e. Agent controls and the plugin marketplace

The bot's subagents are controllable from the dashboard Agents tab, and new agents install from a
shared plugin marketplace. None of this is required to run the bot; it matters when you want to turn
agents on or off, set their cadence, or add new ones.

**What ships (committed):** `public_com_autonomous_trading/agent_controls.py` is the enable/disable
plus cadence gate. The mind convenes a subagent only if it is enabled and due; a disabled agent is
never convened, even if a step asks. Cadence-controlled agents run `every run` or `every N hours`
(N set on the tile). Control state is gitignored and auto-creates empty (see the table above):
`state/mind/agent_controls.json` (your toggles, dashboard-written), `agent_runtime.json`
(last-convened, mind-written), `install_queue.json` and `installed_plugins.json` (marketplace).

**Agents tab:** one card per agent with a corner on/off switch; disabled agents gray into a Disabled
section. Click an enabled card for its spec and cadence. A Downloaded section lists installed
plugins (each with Remove); a Marketplace section lists installable plugins by author.

**Marketplace:** a Claude Code plugin marketplace, by default
`https://github.com/nirajgtm/AiTrader-plugins` (the reference currently lives in `SKILL.md` and
`dashboard.py`; moving it into `config.json` is an open item). Clicking Install records the plugin
at once (a virtual install, so it is immediately controllable), queues the real
`claude plugin install` for the next run, and the mind convenes it by reading its spec from GitHub
until the native plugin loads on the next session restart.

**REQUIRED for the marketplace: the GitHub CLI (`gh`), installed and authenticated** (see Group A).
The marketplace repo is public so anonymous reads work, but an authenticated `gh` is what makes
`claude plugin marketplace add` / `claude plugin install` reliable and is required for any private
marketplace. For unattended plugin updates inside the loop, set `GITHUB_TOKEN` (or `GH_TOKEN`) in the
environment so it does not prompt.

---

### Step 11: Start the local dashboard

```bash
cd ~/claude-configs/trader && set -a && source .env && set +a
$TPY public_com_autonomous_trading/dashboard.py serve --port 8787 &
DASH_PID=$!
sleep 2
curl -s http://localhost:8787 | grep -q "Autonomous Trader" && echo "dashboard: ok" || echo "dashboard: FAILED"
```

If ok, tell the user:
> "Dashboard is running at http://localhost:8787. Open it in a browser. It's a redesigned,
> server-rendered light UI with tabs: Home (portfolio, equity curve, invested-vs-cash),
> Mind (the live mind-view of what it's thinking, watching, and doubting), Watch (the
> watchlist), Decide (where the bot queues anything needing your sign-off), Log (activity),
> Evolve (how its thinking has changed -- belief flips, applied rule changes, rejected
> proposals), and Agents (control tiles to enable or disable each agent, set its cadence, and install plugins from the marketplace). A Controls overlay holds the tunable
> temperament (persona dials) plus accent and density; an 'Instruct the mind' composer takes
> free-form instructions and pasted images (this is how you set a standing directive now).
> The process is running in the background (PID `<DASH_PID>`). It will stop when you close
> this terminal session. To restart it later, run:
>   `~/claude-configs/trader/scripts/.venv/bin/python3 ~/claude-configs/trader/public_com_autonomous_trading/dashboard.py serve --port 8787`"

---

### Step 12: Final verification

Run all checks:

```bash
cd ~/claude-configs/trader && set -a && source .env && set +a
export TPY=~/claude-configs/trader/scripts/.venv/bin/python3

echo -n "1. venv:         " && $TPY -c "import yfinance, pandas; print('ok')"
echo -n "2. .env loaded:  " && $TPY scripts/_apikeys.py 2>&1 | grep -c "present" | xargs echo "keys present"
echo -n "3. brief runs:   " && $TPY scripts/brief.py status > /dev/null 2>&1 && echo "ok" || echo "FAILED"
echo -n "4. quote works:  " && $TPY scripts/price.py SPY > /dev/null 2>&1 && echo "ok" || echo "FAILED"
echo -n "5. portfolio:    " && $TPY scripts/portfolio.py show > /dev/null 2>&1 && echo "ok" || echo "FAILED"
echo -n "6. skills linked:" && test -L ~/.claude/skills/trader && test -f ~/.claude/skills/trader/SKILL.md && test -L ~/.claude/skills/trader-autonomous && test -f ~/.claude/skills/trader-autonomous/SKILL.md && echo "ok (both)" || echo "MISSING (see Step 6)"
echo -n "7. env perms:    " && ls -l ~/claude-configs/trader/.env | grep -q "rw-------" && echo "ok (600)" || echo "fix: chmod 600 .env"
echo -n "8. gitignore:    " && git -C ~/claude-configs/trader check-ignore .env state/ > /dev/null 2>&1 && echo "ok" || echo "WARN: verify .gitignore"
```

For the autonomous bot (if enabled):
```bash
echo -n "9. bot context:  " && $TPY public_com_autonomous_trading/run_autonomous.py context > /dev/null 2>&1 && echo "ok" || echo "FAILED"
echo -n "10. dashboard:   " && curl -s http://localhost:8787 | grep -q "Autonomous Trader" && echo "ok" || echo "not running (restart manually)"
```

All checks must print `ok` before proceeding to scheduling.

---

### Step 13: Set up /loop scheduling

If `LOOP_CADENCE` is a specific preference, weave the user's wording into each loop prompt below in
place of the default parenthetical (keep it as flexible guidance, never a hard "every N min", which
would turn the loop into a fixed cron and lose the dynamic pacing). Otherwise use the defaults
shown. Tell the user:

> "The system uses Claude Code's /loop command for scheduling, no system cron needed. Both loops
> are DYNAMIC: no fixed interval, the assistant self-paces each tick by market hours and what is
> actually live (it stretches to hours when idle and tightens when something needs attention). Run
> these in your Claude Code session to keep everything going:
>
> **Research brief loop** (one Claude Code terminal tab):
> `/loop /trader dynamic by market hours (~2h during market hours, much sparser off-hours, quicker if something is breaking)`
>
> **Autonomous bot loop** (a separate tab):
> `/loop /trader-autonomous dynamic by market hours (~30m during market hours, much sparser off-hours, tighten to ~10-15 min when managing a position or near a trigger)`
>
> Both loops run inside your active Claude Code session and stop when you close it; restart with the
> same commands. To stop one: press Escape or Ctrl+C in its tab."

If the user said `skip` to autonomous bot loop, only show the brief loop instruction.

---

### Step 14: Tell the user they're done

> "Setup complete. Here is your quick-start cheat sheet:
>
> START EACH SESSION:
>   1. cd ~/claude-configs/trader && source .env (loads API keys)
>   2. Brief loop in one tab:  /loop /trader dynamic by market hours (~2h during market hours, sparser off-hours)
>   3. Bot loop in another tab: /loop /trader-autonomous dynamic by market hours (~30m during market hours, sparser off-hours, quicker when active)
>   4. Open http://localhost:8787 for the dashboard (start it first if not running)
>
> FIRST BRIEF:
>   Type `/trader` once manually now — it will run a full morning refresh (5-15 min, cold cache).
>   After that, the looped runs reuse the warm cache and are faster.
>
> ADDING POSITIONS:
>   Report fills to /trader as: `filled NVDA 10 shares at $875`
>   The skill updates your portfolio automatically.
>
> ENABLING LIVE TRADING (autonomous bot):
>   Review 3-5 dry-run sessions in the dashboard. When confident, edit
>   `~/claude-configs/trader/public_com_autonomous_trading/config.json`
>   and change `"enabled": false` to `"enabled": true`.
>
> SKIPPED KEYS — features that are degraded:
>   <list each skipped key and what it affects, e.g.:
>   - ALPHAVANTAGE_API_KEY: fundamentals fallback missing
>   - COINGECKO_DEMO_API_KEY: crypto scans rate-limited (5 req/min instead of 30)>
>
> You can add missing keys later by editing ~/claude-configs/trader/.env"

---

## Troubleshooting

**`ModuleNotFoundError`**: always use the venv python, never the system one.
Full path: `~/claude-configs/trader/scripts/.venv/bin/python3`

**`KeyError: 'PUBLIC_BROKER_API'`**: .env not sourced. Run:
`cd ~/claude-configs/trader && set -a && source .env && set +a`

**Brief returns empty candidates**: normal on cold cache. Run
`$TPY scripts/brief.py quick` to warm it, then `$TPY scripts/brief.py morning` for full refresh.

**Dashboard not running**: restart with:
`$TPY ~/claude-configs/trader/public_com_autonomous_trading/dashboard.py serve --port 8787 &`

**`/trader` or `/trader-autonomous` not recognized by Claude Code**: the skill symlink is missing.
Confirm: `ls -l ~/.claude/skills/trader ~/.claude/skills/trader-autonomous` (both should point into
`~/claude-configs/trader/skills/`). If missing, re-run the `ln -s` commands in Step 6, then restart
Claude Code.

**Bot dry-run returns error**: usually wrong account ID. Verify `account_id` in `config.json`
matches the BUY_AND_SELL account from public.com.

**API key rejected (401)**: key copied incorrectly (watch for leading/trailing spaces). Edit `.env`
directly to fix: `nano ~/claude-configs/trader/.env`
