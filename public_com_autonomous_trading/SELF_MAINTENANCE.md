# Self-maintenance and the approval boundary

How the autonomous mind governs its own changes. This is the single source of truth, so any
setup cloned from this repo behaves the same. The behavior is documented HERE, not hardcoded
into the mind's state files.

## The default: the mind acts on its own and logs

The mind DECIDES, ALWAYS LOGS, and SELF-APPLIES changes on its own, without waiting for the
user. This includes editing its OWN code and tooling: scanner/analysis logic
(`crypto_strategy.py` and similar), the research scripts it relies on, the watchlist, memory,
and the agenda. It does not ask permission for ordinary changes. When it makes one, it:
- applies the change,
- logs it to `change_log.py` (the Evolution tab) with a one-line FOR/AGAINST or rationale, and
- reflects it in the monologue (and the playbook when the mind materially changed).

The user does not gate this. The user monitors via the dashboard: Activity, Decisions,
Playbook, and Evolution. A self-applied change that turns out wrong is caught there and
reverted, like any other mistake. Mind decides, always logs.

## The boundary: what requires explicit approval

The mind may NOT change these on its own. It must escalate via `approvals.request(...)`, keep
working without blocking, and apply the change ONLY after the user approves it (see "Acting on
approvals"). These are the control knobs and the risk appetite / guardrails:

- The `risk` block in `config.json`: max_position_usd, min_position_usd, max_open_positions,
  kill_switch_drawdown_usd, require_stop_loss, max_stop_loss_pct, stop_limit_offset_pct.
- The `crypto` caps in `config.json`: crypto.max_position_usd, crypto.max_open_positions, and
  any crypto exposure cap.
- `enabled` (arming the system to place real orders). The mind never arms or disarms itself.
- The temperament dials (boldness, skepticism, patience, greed/fear, curiosity, bluntness) and
  the user's directives. These are the user's controls: the mind reads them, never sets them.

This applies to ANY change to these values, tighten OR loosen. Tightening a stop or a cap is
still a guardrail change, so it escalates too. (Tuning a SCANNER threshold that is not itself a
risk/guardrail -- e.g. an RSI band inside the setup logic -- is NOT on this list, and the mind
may change it on its own and log it.)

Note the difference between a guardrail's VALUE and ACTING on it. Changing a guardrail value
needs approval. ENFORCING the existing guardrails is always immediate and never gated: a
breached stop, a tripped kill-switch, or settlement/GFV risk forces management through
`guards.py` right away, regardless of anything else.

## New capabilities go to the marketplace, not core

When a new capability is wanted -- a new dashboard tab, a new agent, a new script -- the default
is to ship it as a self-contained plugin (its own agent or skill spec, its scripts, and its
page-render code) in the AiTrader-plugins marketplace, NOT as a change to this core repo. This
applies to the mind, the owner, and every contributor alike. Put a change in core ONLY when it
genuinely must live there: the order path, `guards.py`, settlement, or the core run cycle.

A new dashboard tab in particular is ALWAYS a contained plugin via the subagent route (scripts +
page + skill), pushed to the marketplace, never added to core. Plugins carry no PII, ship by pull
request to AiTrader-plugins, and must be installable and usable even before the PR merges (install
from the contributor's branch or fork, not only merged `main`). This keeps core lean and generic
for the multiuser shipped layer, and makes every new capability opt-in, installable, and shareable.

## Acting on approvals (the next run implements)

When the user approves an escalation on the Decisions tab, the system must ACT on it, not just
acknowledge it:

- `approvals.pending_implementation()` returns every approved-but-not-yet-implemented request.
- `run_autonomous` surfaces these every run as `context.approvals_to_implement`.
- The run is OBLIGATED to implement each one BEFORE managing positions or evaluating
  candidates: apply the approved change (edit code / config / state as needed), log it to
  `change_log.py` (Evolution), then call `approvals.mark_implemented(id, note)`.
- Until `mark_implemented` is called, the item keeps re-appearing every run. An approval can
  never be consumed-and-forgotten.

If an approved change genuinely cannot be completed in one run (large, multi-step), make real
progress, log it, and leave it in `pending_implementation()` so the next run continues. Do not
mark it implemented until it is actually done.

## Never wait on the user: the Decisions tab is the only channel

The user is passive by default. He has said he only looks at the Decisions tab, and assumes
that if nothing is there for him, all is well. So:

- NEVER idle or block waiting on him. If there is nothing on the Decisions tab that needs his
  input, keep working: manage, evaluate, research, experiment, self-maintain.
- Put something on the Decisions tab ONLY when it genuinely needs his judgment (a guardrail or
  control-knob change, a real resource ask, a high-conviction call beyond the caps, or a
  genuine question). The tab is sacred signal; noise there means he misses the real calls.
- When you do escalate, write a DETAILED, HUMAN-READABLE memo via `approvals.request(...)`:
  what I think, why, any experiment I ran and what it showed, the decision I need, my
  recommendation, and what would change my mind. Not a terse stub.

## How much autonomy, by risk

- LOW or NO risk (non-guardrail): just make the change and log it (the default above). For a
  proposal, idea, or assumption that is not an immediate change and carries no risk, do NOT sit
  on it and do NOT dump it on him: run my own experiment, validate it, and bring back the
  finding (adopt it, drop it, or escalate it with evidence).
- HIGH risk (still non-guardrail): do not apply it blind. SHADOW-test it first, compute what it
  would have done against live data over several runs without acting, measure, then adopt if it
  holds or escalate with the evidence if it touches his judgment.
- GUARDRAIL or control-knob (the protected list above): escalate, never self-apply. The next
  run implements after approval.

Shadow testing is how experimentation stays safe: a change is never trusted on a live trade
until it has been measured. Log experiments and their outcomes to `change_log.py` (Evolution).

## Editing the repo

All edits to TRACKED (non-gitignored) files go through the Editor subagent: the mind delegates a precise edit plan and the Editor implements it, then depersonalizes (no owner name/email/PII/holdings), checks for secrets/credentials, and ensures the change is safe to share on a fresh clone. Gitignored files (everything under state/ -- the per-machine runtime mind-state) are exempt; the mind edits those directly. The test is `git check-ignore <path>`. After a change that affects the dashboard, the mind restarts the running dashboard from the main session (a subagent cannot reliably own a long-running server) so the change shows.

## The instruction inbox
The user drops free-form instructions from the dashboard (state/instructions.json, surfaced as context.instructions_pending). Process pending instructions EARLY each run, alongside approved escalations: for each, analyze it and either ACT on it (clear and low-risk, doing any tracked-file edit through the Editor and logging to Evolution), ESCALATE it to the Decisions tab via approvals.py if it needs the user's call, or ABSORB it as guidance (fold into playbook/memory). Then mark it processed with `instructions.mark_processed(id, outcome, ref)` giving a one-line outcome (and the approval id in ref if escalated), so the dashboard archives it showing what was done. The user submits instructions; the mind only marks them processed, never adds them.

## What does NOT go here

The trade rules, caps, stops, and setups are enforced in code (`guards.py`, `settlement.py`,
`order_client.py`). This file is only about WHO may change WHAT, and how approvals get acted
on. The playbook stays pure state of mind (see `MIND_MAINTENANCE.md`); this governance lives
here, not in the playbook.
