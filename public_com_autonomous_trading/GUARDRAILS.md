# Day-Trading & Settlement Guardrails (public.com)

Safety rules every autonomous order must pass BEFORE placement. Sourced from
public.com help articles (cited). These are checks the execution layer enforces;
none of this places orders by itself.

## Account type: CASH (confirmed by user 2026-05-21)

Account `&lt;YOUR_ACCOUNT_ID&gt;` is a **cash account**. Therefore:
- **PDT does NOT apply** — no 4-day-trades-in-5 limit, no $25k equity floor, no
  DTBP / Day-Trade-Call / margin-call machinery. The margin section below is kept
  for reference only and is **N/A** for this account.
- The **binding rules are Good-Faith-Violations, T+1 settlement, and free-riding.**
  These are the guardrails the execution layer MUST enforce.

## Cash-account rules (BINDING)

- **Good Faith Violation (GFV):** buying with unsettled funds, then selling that
  security before the funding settled. Escalation over rolling 12 months:
  4th GFV -> settled-funds-only for 12 months; 5th -> "Sell Only" 90 days
  (clearing-house, cannot be removed).
- **Free-riding:** funding a buy by selling that same security before settlement.
  One in 12 months -> restriction; profits become non-withdrawable.
- **Settlement:** T+1 for equities/ETFs. Options T+1 is **NOT confirmed** in the
  docs — verify before relying on it.

## Margin-account rules (N/A — reference only; &lt;YOUR_ACCOUNT_ID&gt; is cash)

- **PDT:** 4+ day trades in 5 consecutive business days -> flagged; needs $25k
  (cash+securities, excludes crypto/alts/treasuries/HYC/bonds) at prior close,
  else "Sell Only" 90 days.
- **DTBP** = 4x prior-close maintenance-margin excess; DT Call (T+3 to cure, else
  90-day closing-only) and Money-Due Call (T+3) are hard stops.

## What counts as a day trade
Open + close the same security (stock / ETF / option) on the same business day.
**Exempt:** crypto, alternatives, treasuries. Multi-leg options legged in/out as
separate orders can each count as a day trade — prefer single multi-leg orders.

## Codeable pre-trade checks (the execution layer must run these)

1. **Settled-cash ledger** (cash acct): track settled vs unsettled per lot; never
   sell a position bought with unsettled funds before its funding settles (T+1).
2. **Free-riding guard:** every cash-account buy must be coverable by settled cash.
3. **No buying on unsettled proceeds** unless the new position can be held until the
   funding sale settles.
4. **Day-trade counter** — N/A (margin/PDT only; this is a cash account).
5. **$25k equity check** — N/A (margin/PDT only).
6. **T+1 settlement** respected for equities/ETFs; options settlement to be verified.
7. **Crypto/alts/treasuries** exempt (relevant only to day-trade counting; N/A here).
8. **DTBP / call modeling** — N/A (margin only).

## Layered with the other constraints (from the directory README)
Preflight before placement; UUID idempotency; kill-switch default-off; 10 req/s
limit; L2 options only (no spreads unconfirmed); no options on operator-restricted
tickers (employer/insider; per owner config); no shorting (cash);
sizing/concentration caps.

## Open items to verify before execution code
- [x] Account type of &lt;YOUR_ACCOUNT_ID&gt; -> CASH (confirmed by user 2026-05-21). PDT N/A.
- [ ] Options settlement timing (assume T+1, confirm).
- [ ] Whether multi-leg/spread orders are enabled (L2 suggests not).

## Sources
help.public.com articles: 1730948 (Day Trading), 2426107 (Cash Account
Violations), 2426109 (Unsettled Funds), 10029147 (DTBP), 11750505 (DT Call vs
PDT), 9252110 (Deposit/Settlement), 9773751 (Margin Calls).
