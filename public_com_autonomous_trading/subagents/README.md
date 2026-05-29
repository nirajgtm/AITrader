# Subagents

The mind's roster of specialist subagents. The mind spins these up with the Agent tool when
it judges one would help, briefs them per their spec, and acts on what they return. This
directory is organized so the roster can grow and shrink over time without clutter.

## Two tiers

- CORE (every run, defined in the `trader-autonomous` skill, kept as-is): the debate voices
  (Strategist, Historian, Risk Officer, Skeptic, Opportunist, Behaviorist), convened only
  when a call is genuinely contested, and the Janitor at end of run. Their invocation lives
  in the skill; they are listed in `registry.json` for a full picture.
- ON-DEMAND (this directory): specialists the mind invokes only when needed. Each has a
  `<name>.md` spec. The mind picks from the registry, briefs per the spec, invokes, and
  records use.

## How the mind uses an on-demand agent

1. Notice a need (a question it cannot answer well alone, research to run, a case to argue).
2. Check `registry.json` for a fitting role. Read its spec.
3. Invoke via the Agent tool with the role's brief: the question, the relevant facts, and the
   role's mandate. Use the model the spec names.
4. Treat the output as ADVICE. The mind synthesizes and decides; a subagent never decides.
5. Update `last_used` in the registry.

## Lifecycle (the mind owns this)

- CREATE: when the mind repeatedly needs a capability it does not have, write a new
  `<name>.md` spec and add it to `registry.json` (status active). Log it to Evolution.
- USE: invoke as needed; keep `last_used` current.
- RETIRE: if a role goes long unused or proves useless, mark it `retired` in the registry
  and delete its spec to reduce clutter. The Janitor flags idle ones; the mind prunes.

## Briefing discipline

- Debate voices and the Case Builder are briefed BLIND: the question, the facts, and their
  own mandate only. Never my lean, never the other voices' takes, so the disagreement is real.
- Research, news, analyst, ideas, and expert agents get the question and the scope, plus any
  facts they need. They may use the read-only research scripts and the web.
- Model: reasoning voices use the default model; mechanical work (the Janitor) uses Haiku.

## Source of truth

`registry.json` is authoritative for what exists and its status. Keep it honest.
