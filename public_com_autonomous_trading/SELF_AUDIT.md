# Self-audit - the system finds and fixes its own holes

The mind turns a critical eye on its OWN setup on a rotating basis: scripts, data,
coverage, rules, and dead weight. It finds the holes and fixes them, or escalates
them. The owner should rarely be the one to point out a flaw.

## Cadence
Run ONE theme's DEEP pass on most substantive runs (not just quiet or >24h-stale runs -- that made it fire too rarely to catch anything). Rotate themes so all get covered over a few runs. A live tape or open position still takes priority, and a truly time-pressured run can defer, but the default is: every substantive run advances one theme. The janitor (every run) is the shallow net underneath this (see below).

## Rotation (one per audit; `state/mind/self_audit.json` holds the pointer)
1. **script_health** - run each read-only script; flag any that error, return
   empty, time out, or return stale/clearly-wrong data. Trace the root cause (e.g.
   a paid-tier 402) and fix or re-source.
2. **discovery_coverage** - for each signal source (movers, flow, congress,
   insider, social chatter, earnings, sector leaders), confirm its names actually
   reach the discovery sweep. A legit liquid ticker a source surfaced that never
   reaches discovery is a hole: decide whether it should be surfaced and wire it in.
3. **unused_assets** - scripts, MD files, and JSON/state that exist but feed no
   live path. Each gets wired in (if it adds value) or removed (if it does not).
   Dead weight is removed, not kept "just in case".
4. **hypotheses_rules** - are the codified setups, watchlist entries, and stored
   convictions still valid? Retire dead theses, stale levels, and rules that never
   fire or no longer pay.
5. **guardrails_risk** - are caps, stops, sizing, and arming still right for the
   goal? NEVER self-apply a change here: escalate with a proposal.
6. **data_api_tokens** - which providers are degraded, rate-limited, or paid-walled?
   What tier, key, or feed would unlock real value? Escalate any cost/credential call.

## What you are hunting (every theme)
Be surgical, not a glance. Actually RUN the relevant scripts, READ the raw output, and compare it to what it SHOULD be. Trace root causes. Specifically hunt:
- **Silent failures** - a script that errors, returns empty, times out, or returns stale/clearly-wrong data while looking fine.
- **Regressions / dark signals** - a signal or feature that used to produce output and now returns nothing (e.g. a scan bucket that went to 0 after a change).
- **Overlaps / redundancy** - two scripts or paths doing the same job; decide which owns it and retire or narrow the other.
- **Bottlenecks** - rate limits, slow paths, partial coverage, anything capping how much of the universe or signal you actually see.
- **Miswired data** - a source that produces good data that never reaches where it is used.
- **Assumptions to challenge** - a rule, threshold, or design choice that may no longer hold.
The owner should NEVER be the one who first finds one of these. If you find one, fix it (non-guardrail, via the Editor, logged) or escalate it.

## Fix or escalate
- **Fix yourself** anything non-guardrail: re-source a broken script, wire in a
  missing source, remove dead weight, retire a stale rule. Route tracked-file edits
  through the Editor; log via `change_log.py change`.
- **Escalate** anything that is a guardrail/risk/control-knob change, costs money,
  or is a judgment call for the owner: `approvals.py request(...)` to the Decisions
  tab; keep working.

## Tracking
`state/mind/self_audit.json`: the rotation pointer, last-run timestamp, and a short
log of each theme's findings + what was fixed or escalated. Update it every audit.
The janitor (every-run, reflexive) is the shallow net that FLAGS candidates for this
deeper audit; here is where the deep dive and the fix happen.
