#!/usr/bin/env bash
# publish_site.sh — redact-check, gate, and publish a brief to market-watch
#
# Usage:
#   publish_site.sh                  interactive publish (grep checks + human checklist + POST + push)
#   publish_site.sh --auto           non-interactive publish (grep checks only, no human prompt)
#   publish_site.sh --check          run redaction grep checks only, no commit
#   publish_site.sh --dry            full flow but skip `git push`
#   publish_site.sh --init [date]    write a fresh staging.json template (no commit)
#
# staging.json lives at ~/claude-configs/trader-site/staging.json and is gitignored.

set -euo pipefail

SITE_DIR="${HOME}/claude-configs/trader-site"
STAGING="${SITE_DIR}/staging.json"
BRIEFS="${SITE_DIR}/briefs.json"
RECIPIENTS="${HOME}/claude-configs/trader/broadcast_recipients.json"

if [[ -t 1 ]]; then
  red=$(tput setaf 1); green=$(tput setaf 2); yellow=$(tput setaf 3); cyan=$(tput setaf 6); dim=$(tput dim); bold=$(tput bold); reset=$(tput sgr0)
else
  red=""; green=""; yellow=""; cyan=""; dim=""; bold=""; reset=""
fi

err()  { printf "%s%s%s\n" "$red" "$*" "$reset" >&2; }
ok()   { printf "%s%s%s\n" "$green" "$*" "$reset"; }
warn() { printf "%s%s%s\n" "$yellow" "$*" "$reset"; }
info() { printf "%s%s%s\n" "$cyan" "$*" "$reset"; }
hr()   { printf "%s%s%s\n" "$dim" "----------------------------------------" "$reset"; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { err "Missing required command: $1"; exit 2; }
}

require_cmd jq
require_cmd git

cmd="${1:-publish}"

# --------------------------------------------------------------------------- #
# --init: write a template staging.json                                       #
# --------------------------------------------------------------------------- #
if [[ "$cmd" == "--init" ]]; then
  date_arg="${2:-$(date +%F)}"
  ts="$(date -Iseconds 2>/dev/null || date +%FT%T%z)"
  mkdir -p "$SITE_DIR"
  cat > "$STAGING" <<JSON
{
  "date": "$date_arg",
  "updated_at": "$ts",
  "regime": "BULL",
  "regime_note": "",
  "vix_bucket": "normal",
  "headline": "",
  "top_actions": [],
  "macro": {
    "indices": [],
    "vol_yields": {},
    "sector_rotation": { "leaders_5d": [], "laggards_5d": [], "read": "" },
    "events_14d": [],
    "earnings_7d": [],
    "action": { "tier": "NO_ACTION", "text": "" }
  },
  "stocks": {
    "watchlist": [],
    "smart_money_clusters": [],
    "wsb_top": [],
    "action": { "tier": "NO_ACTION", "text": "" }
  },
  "options": {
    "unusual": [],
    "earnings_iv": [],
    "leaps": [],
    "action": { "tier": "NO_ACTION", "text": "" }
  },
  "crypto": {
    "status": "coming_soon",
    "note": "Crypto regime tracking begins after skill expansion."
  }
}
JSON
  ok "Wrote template → $STAGING"
  info "Edit it, then run: $0"
  exit 0
fi

# --------------------------------------------------------------------------- #
# Validate staging.json exists + is JSON                                      #
# --------------------------------------------------------------------------- #
[[ -f "$STAGING" ]] || { err "staging.json not found at $STAGING"; info "Run: $0 --init   to create a template."; exit 1; }
if ! jq -e . "$STAGING" >/dev/null 2>&1; then
  err "staging.json is not valid JSON."
  exit 1
fi

DATE="$(jq -r '.date // ""' "$STAGING")"
[[ -n "$DATE" ]] || { err "staging.json: missing .date"; exit 1; }
[[ "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { err "staging.json: .date must be YYYY-MM-DD (got '$DATE')"; exit 1; }

# --------------------------------------------------------------------------- #
# Automated redaction grep checks                                             #
# --------------------------------------------------------------------------- #
info "${bold}[1/3] Automated redaction checks${reset}"
hr

# Build pattern list. Use ERE. Each pattern is grepped against staging.json (as text).
declare -a PATTERNS=(
  'phone_e164|\+[1-9][0-9]{9,14}'
  'api_key_openai|sk-[A-Za-z0-9_-]{20,}'
  'api_key_bearer|Bearer[[:space:]]+[A-Za-z0-9._-]{20,}'
  'api_key_github|ghp_[A-Za-z0-9]{20,}'
  'api_key_slack|xox[baprs]-[A-Za-z0-9-]{10,}'
  'api_key_aws|AKIA[0-9A-Z]{16}'
  'personal_user_holds|user holds'
  'personal_user_book|user book'
  'personal_my_book|personal book'
  'personal_my_position|my position'
  'personal_i_hold|I hold'
  'personal_shadow_flag|shadow_outperforming'
  'personal_regret|regret_ledger'
  'personal_portfolio_id|portfolio_id'
  'share_count|[0-9]+(\.[0-9]+)?[[:space:]]+sh[[:space:]]+@'
  'pnl_dollars|[+-]?\$[0-9,]+(\.[0-9]+)?[[:space:]]+(unrealized|cost basis|P&L|PnL|cumulative)'
  'path_trader|claude-configs/trader/'
  'path_portfolio|state/portfolio'
)

# Add E.164 phone numbers from broadcast_recipients.json (literal match, exact digits)
if [[ -f "$RECIPIENTS" ]]; then
  while IFS= read -r phone; do
    [[ -z "$phone" ]] && continue
    # escape '+' for ERE (not strictly needed but safer)
    esc="${phone//+/\\+}"
    PATTERNS+=("recipient_phone_${phone}|${esc}")
  done < <(jq -r '.recipients[]?.phone // empty' "$RECIPIENTS")
fi

findings=0
for p in "${PATTERNS[@]}"; do
  name="${p%%|*}"; pat="${p#*|}"
  if matches=$(grep -niE "$pat" "$STAGING" 2>/dev/null); then
    err "  ✗ $name"
    while IFS= read -r line; do printf "    %s%s%s\n" "$dim" "$line" "$reset"; done <<<"$matches"
    findings=$((findings+1))
  fi
done

if (( findings > 0 )); then
  hr
  err "${bold}REDACTION CHECK FAILED ($findings pattern(s) matched)${reset}"
  err "Edit $STAGING and re-run."
  exit 1
fi
ok "  ✓ No redaction patterns matched."

(( findings == 0 )) || { err "Structural checks failed."; exit 1; }

# Content-sufficiency gate: every tab has tab_intro + minimum content depth.
PY="$(dirname "$0")/.venv/bin/python3"
[[ -x "$PY" ]] || PY="python3"
if ! "$PY" "$(dirname "$0")/validate_brief.py" "$STAGING"; then
  err "Content-sufficiency check failed. Fix the gaps above and re-run."
  exit 1
fi

if [[ "$cmd" == "--check" ]]; then
  ok "${bold}Check-only mode: no commit. Exiting.${reset}"
  exit 0
fi

# --------------------------------------------------------------------------- #
# Human checklist (skipped in --auto)                                         #
# --------------------------------------------------------------------------- #
if [[ "$cmd" != "--auto" ]]; then
  echo
  info "${bold}[2/3] Human checklist${reset}"
  hr
  cat <<EOF
Confirm each item before publishing. Anything unsure → cut the section.

  1. ${bold}No personal P&L${reset}: no \$ amounts tied to your holdings; no realized/unrealized profit; no account size.
  2. ${bold}No share counts${reset}: no specific position sizes anywhere.
  3. ${bold}No identity${reset}: no phone numbers, recipient names, or "user / me / my book" references.
  4. ${bold}No secrets${reset}: no API keys, .env values, paths under ~/claude-configs/trader/.
  5. ${bold}Numbers verified${reset}: every price/RSI/VIX/score matches today's daily log, nothing recalled or estimated.
  6. ${bold}Acronyms handled${reset}: every acronym is in the glossary or expanded inline on first use.
  7. ${bold}Action callouts clear${reset}: each tab has tier (ACTION/WATCH/NO_ACTION) + one-sentence text.
  8. ${bold}Headline is plain-English${reset}: <= 90 chars, beginner-readable.
  9. ${bold}Date is today${reset}: $DATE
 10. ${bold}No personalized advice${reset}: nothing reads as a buy/sell recommendation for an individual.
EOF
  hr
  echo

  info "Headline:        $(jq -r '.headline' "$STAGING")"
  info "Regime:          $(jq -r '.regime + (if .regime_note != \"\" then \" / \" + .regime_note else \"\" end)' "$STAGING")"
  info "Top actions:"
  jq -r '.top_actions[]? | "  - " + .' "$STAGING"
  echo
  info "Macro action:    $(jq -r '.macro.action.tier + " - " + (.macro.action.text // \"\")' "$STAGING")"
  info "Stocks action:   $(jq -r '.stocks.action.tier + " - " + (.stocks.action.text // \"\")' "$STAGING")"
  info "Options action:  $(jq -r '.options.action.tier + " - " + (.options.action.text // \"\")' "$STAGING")"
  echo

  read -r -p "Type ${bold}POST${reset} to publish, anything else to abort: " confirm
  if [[ "$confirm" != "POST" ]]; then
    warn "Aborted. Nothing committed."
    exit 1
  fi
else
  info "${bold}--auto: skipping human checklist (grep gate already enforced)${reset}"
fi

# --------------------------------------------------------------------------- #
# Merge into briefs.json (newest first, dedup by date)                        #
# --------------------------------------------------------------------------- #
echo
info "${bold}[3/3] Merging + committing${reset}"
hr

[[ -f "$BRIEFS" ]] || echo '{"version":1,"briefs":[]}' > "$BRIEFS"

tmp="$(mktemp)"
jq -s '
  .[0] as $existing | .[1] as $new
  | ($existing.briefs + [$new])
  | group_by(.date)
  | map(max_by(.updated_at // ""))
  | sort_by(.date) | reverse
  | { version: ($existing.version // 1), briefs: . }
' "$BRIEFS" "$STAGING" > "$tmp"

if ! jq -e . "$tmp" >/dev/null 2>&1; then
  err "Merge produced invalid JSON. Aborting."
  rm -f "$tmp"
  exit 1
fi
mv "$tmp" "$BRIEFS"
ok "  ✓ Merged $DATE into briefs.json"

# --------------------------------------------------------------------------- #
# Enrich briefs.json with sparks, movers, wsb_top, unusual flow, market_status
# --------------------------------------------------------------------------- #
ENRICH="${HOME}/claude-configs/trader/scripts/enrich_briefs.py"
TPY="${HOME}/claude-configs/trader/scripts/.venv/bin/python3"
if [[ -x "$ENRICH" || -f "$ENRICH" ]]; then
  if [[ -x "$TPY" ]]; then
    info "  Enriching briefs.json (sparks, movers, wsb, flow, market_status)..."
    if "$TPY" "$ENRICH" 2>&1 | grep -E '^\[' | tail -3; then
      ok "  ✓ Enriched."
    else
      warn "  ! Enrichment had issues; brief is published with what the skill produced."
    fi
  else
    warn "  ! enrich_briefs.py present but venv python missing at $TPY; skipping enrichment."
  fi
fi

cd "$SITE_DIR"
git add briefs.json
git add -A index.html styles.css SCHEMA.md README.md 2>/dev/null || true

if git diff --cached --quiet; then
  warn "  ! No changes staged — brief was identical to previous version. Nothing to commit."
  exit 0
fi

git commit -m "publish $DATE brief" >/dev/null
ok "  ✓ Committed."

if [[ "$cmd" == "--dry" ]]; then
  warn "  ! --dry: skipping git push."
  exit 0
fi

if git remote get-url origin >/dev/null 2>&1; then
  git push origin main
  ok "  ✓ Pushed to origin/main."
  ok "${bold}Live in ~30s at your GitHub Pages URL.${reset}"
else
  warn "  ! No 'origin' remote configured yet. Commit landed locally."
  info "    Configure with:  gh repo create market-watch --public --source=. --remote=origin --push"
fi
