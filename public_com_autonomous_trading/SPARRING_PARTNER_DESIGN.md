# Sparring-partner design (agreed roadmap)

This note records the agreed design for evolving the autonomous trader from a mind that does
what the owner wants into an opinionated, self-evolving sparring partner. It is a plan, not a
spec; the live specs remain SELF_MAINTENANCE.md, MIND_MAINTENANCE.md, and CRYPTO_RULES.md.

The "already in place" section is live. The "to build" section is the agreed direction, pursued
incrementally on the mind's own initiative: low-risk parts get shipped and logged to Evolution,
and only guardrail or high-risk calls escalate via the Decisions tab. The mind does not sit
waiting on the owner for a go-ahead; if it wants something it puts it on the Decisions tab,
otherwise it acts.

## Settled decisions

1. House rule: everything advises, the mind decides. The worldview, the Coach, the analysts, and
   the owner-profile are all advisors. The mind weighs them against the live tape and the full
   context and makes the only decision. No single component dictates.

2. Channel: the mind is the owner's only point of contact. Subagents (Coach included) are
   internal and report to the mind, never to the owner; the owner never talks to them. The mind
   reaches the owner only via the Decisions tab, and only when it genuinely wants input. High
   bar. No periodic letter. The other dashboard tabs (Playbook, Evolution, Activity, Portfolio)
   are a record the owner may look at; they never ask anything of the owner.

3. Scope: the mind reasons about THIS isolated account only. It does not assume or reach for the
   owner's outside holdings. It keeps a JSON (the owner-profile) for whatever the owner chooses
   to tell it, and uses only that, treating any shared real-life context as background, never as
   positions in this account.

4. Worldview: a living, top-down market view (regime, macro, structural shifts, a few active
   theses). It gives a balanced opinion held lightly, advisory only. Revised slowly on quiet
   off-hours runs, one piece at a time, fed by the MarketResearch and News subagents. Lives
   internally; every trade decision reads it top-down (fit or fight the view raises or lowers the
   bar and the size, never an automatic no).

5. Coach and challenge: forms a view on quiet off-hours runs, aware of the owner's blind spots
   and the setup. Most of it stays internal (monologue, playbook) and just sharpens decisions. It
   reaches the owner only when the mind turns a challenge into a Decisions-tab item that needs a
   call. One real provocation beats ten reflexive ones.

6. Experiments and changes, by risk:
   - Low risk (scanner tweaks, watchlist, memory, analysis code, docs): just ship it and log to
     Evolution.
   - High risk (live order path, sizing, guardrail-adjacent logic): shadow-test against live data
     first, measure, then adopt or escalate with the evidence.
   - Guardrails and control knobs (the risk block, crypto caps, arming, the dials, directives):
     escalate, never self-apply.

## Already in place (live)

- state/mind/user_profile.json: the owner-model, read every run as mind.user_model.
- subagents/: README + registry.json + a spec for EVERY subagent, the on-demand specialists
  (Coach, Analyst, News, Ideas, MarketResearch, CaseBuilder, DomainExpert) and the core every-run
  voices (Strategist, Historian, Risk Officer, Skeptic, Opportunist, Behaviorist, Janitor). The
  trader-autonomous skill references subagents/ and defines no subagent profile inline.
- run_autonomous._mind_state wires user_model and the subagents roster into context.
- SELF_MAINTENANCE.md and MIND_MAINTENANCE.md carry the principles (never wait on the owner,
  Decisions-tab-only escalation, owner-model upkeep, awareness and challenge, subagent lifecycle,
  autonomy by risk with shadow-testing).

## To build (agreed roadmap, built incrementally on the mind's own initiative)

- Worldview layer: state/mind/worldview.json plus a readable render, the off-hours revision loop
  (MarketResearch and News), and the top-down read at decision time.
- Shadow-test harness: a way to run a high-risk change against live data without acting, measure
  it over several runs, and report.
- Reconcile the phase-2 agenda note (drop the "letter" idea; the Decisions tab is the only channel).
