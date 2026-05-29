# Pre-publish redaction checklist

Run before every push to `market-watch`. The `publish_site.sh` script automates the grep checks and prompts the human checklist; this doc is the source of truth for *why* each rule exists.

## Automated grep checks (script enforces)

| # | Pattern | Why |
|---|---------|-----|
| 1 | E.164 phone numbers (`\+[1-9]\d{9,14}`) | Recipients in `broadcast_recipients.json` are private contacts. |
| 2 | API key shapes (`sk-…`, `Bearer …`, `ghp_…`, `xoxb-…`, `AKIA…`) | `.env` secret leak protection. |
| 3 | Personal language tokens: `user holds`, `user book`, `personal book`, `my position`, `i hold`, `shadow_outperforming`, `regret_ledger`, `portfolio_id` | These phrases only exist in the private daily log; their presence in staging.json means the redactor copied a private section. |
| 4 | Share-count syntax: `<n> sh @` or `<n> sh\b` | Position-size leak. |
| 5 | P&L dollar phrases: `[+-]?$<n> (unrealized\|P&L\|cost basis)` | Account-size leak. |
| 6 | File path leaks: `claude-configs/trader/`, `state/portfolio` | Implementation-detail leak. |

A failed automated check → script exits non-zero, prints findings, no commit.

## Human checklist (script prompts)

Every item must be acknowledged before typing `POST`:

- [ ] **No personal P&L**: no dollar amounts tied to my holdings, no realized/unrealized profit numbers, no account size.
- [ ] **No share counts**: no "I own X shares", no specific position sizes.
- [ ] **No identity**: no phone numbers, no recipient names, no references to "user / me / my book".
- [ ] **No secrets**: no API keys, `.env` values, file paths under `claude-configs/trader/`.
- [ ] **All numbers verified**: every price, RSI, VIX, sector score in staging.json matches today's daily log; nothing recalled or estimated.
- [ ] **Acronyms handled**: every acronym is either in the page glossary or expanded inline on first use within the same field.
- [ ] **Each tab has an action**: `macro.action`, `stocks.action`, `options.action` (and `crypto.action` if `status: live`) each have `tier ∈ {ACTION, WATCH, NO_ACTION}` and a one-sentence `text`.
- [ ] **Headline is plain-English**: ≤ 90 chars, no jargon a beginner couldn't follow.
- [ ] **Date is correct**: `date` field is today's date in `YYYY-MM-DD`.
- [ ] **Disclaimer holds**: nothing in the brief reads as personal investment advice or a buy/sell recommendation specific to any individual.

## What's safe to publish (positive list)

- Index levels (SPY, QQQ, IWM) and their RSI/MA position
- VIX, 10Y, DXY, Oil, Gold prices
- Sector ETF rotation scores
- Public economic calendar / earnings calendar items
- Watchlist tickers + trigger zones (these are general market levels, not entries tied to your account)
- Unusual options activity (ticker + V/OI; no contract sizing)
- WSB / public sentiment scores
- Insider cluster *counts* (no recipient names)
- General market commentary, plain-English regime calls

## When in doubt

Cut the section. A blank tab is better than a leak.
