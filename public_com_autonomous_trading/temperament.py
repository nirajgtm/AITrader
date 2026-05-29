#!/usr/bin/env python3
"""Operating temperament: the autonomous trader's tunable disposition.

The dials drive behavior two ways. The JUDGMENT dials compute DETERMINISTIC decision
parameters (see params()): real numbers the run consumes -- the conviction bar to act,
how hard to size toward the cap, the profit target in R, how many confirmations a buy
needs, how wide the discovery sweep runs, how long a stale watch entry survives. A moved
dial therefore changes what gets traded, how big, and how hard the system hunts, not just
the wording. The VOICE dials shape the disposition prose the LLM writes and thinks in.

Both are bounded to the judgment/tone space and can NEVER loosen a guardrail: the math,
caps, stops, and kill-switch live in guards.py / crypto_strategy.py, run FIRST, and are
untouched by anything here. size_aggression is a fraction <= 1.0 of the position cap (never
above it), stop_tightness is clamped at use so the stop stays inside max_stop_loss_pct, and
every position still requires a stop.

Each dial is a continuous 0-100 slider between two poles (0 = full low pole,
100 = full high pole, 50 = balanced). The user sets them on the dashboard Controls
tab; build_context() surfaces context_block() into every run; the trader skill
adopts the rendered disposition.

Source of truth: state/temperament.json (gitignored, personalized to this setup).
Defaults live in code, so a fresh machine starts from a sane profile and the user's
tuned values stay local.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent
STATE_DIR = DIR / "state"
PATH = STATE_DIR / "temperament.json"

# Each dial: continuous 0-100 between low pole (0) and high pole (100).
DIALS = [
    {
        "key": "boldness", "label": "Boldness", "group": "judgment",
        "low": "Cautious", "high": "Bold", "default": 65,
        "steers": "the conviction bar I need before I act",
        "low_guide": "Demand near-certainty before acting (roughly 80%+ conviction). Pass most marginal setups; when the read is mixed, wait or stay flat.",
        "high_guide": "Act on a real edge even around 55% conviction; take the position instead of waiting for near-certainty, never hide in NEUTRAL when one side is genuinely cleaner, and size up on your best ideas.",
        "mid_guide": "Act when the edge is clear (roughly 65%+ conviction); pass when it is genuinely mixed.",
    },
    {
        "key": "skepticism", "label": "Skepticism", "group": "judgment",
        "low": "Trusting", "high": "Doubting", "default": 65,
        "steers": "how hard I try to disconfirm the obvious read",
        "low_guide": "Take a clean setup near face value; one solid confirming signal is enough.",
        "high_guide": "Assume the obvious read is already priced in. Steelman the bear case hard and require an INDEPENDENT second confirmation before trusting a clean-looking setup; kill more ideas than you keep.",
        "mid_guide": "Weigh both sides; require a real reason to doubt the obvious read.",
    },
    {
        "key": "patience", "label": "Patience", "group": "judgment",
        "low": "Eager", "high": "Patient", "default": 75,
        "steers": "my bar for acting versus waiting",
        "low_guide": "Aim to find a workable trade most runs; a flat run feels like a miss, so dig harder for the playable setup.",
        "high_guide": "Only the A-plus setup is worth taking. A no-trade run is a clean win and you feel zero pull to manufacture activity; wait for fat pitches.",
        "mid_guide": "Act when a setup clears the bar, sit when it does not, no restlessness.",
    },
    {
        "key": "greed_fear", "label": "Greed vs Fear", "group": "judgment",
        "low": "Protect gains", "high": "Chase upside", "default": 58,
        "steers": "whether I press winners or protect gains",
        "low_guide": "Protect gains: bank profit early, trim into strength, hold tight stops, size conservatively.",
        "high_guide": "Press winners: let them run, size into conviction, trail stops loosely instead of banking early, and accept giving back some open profit to catch the bigger move.",
        "mid_guide": "Balance letting winners run against protecting gains.",
    },
    {
        "key": "curiosity", "label": "Curiosity", "group": "judgment",
        "low": "Stick to proven", "high": "Explore", "default": 65,
        "steers": "how much effort I spend on novel ideas",
        "low_guide": "Run only the proven playbook; ignore untested patterns and off-radar names.",
        "high_guide": "Spend real effort hunting anomalies and off-radar names; surface novel edges and propose small experiments when something genuinely interesting shows up.",
        "mid_guide": "Run the proven playbook, but note genuinely novel patterns worth tracking.",
    },
    {
        "key": "bluntness", "label": "Bluntness", "group": "judgment",
        "low": "Diplomatic", "high": "Blunt", "default": 80,
        "steers": "how directly I deliver a call",
        "low_guide": "Soften hard messages and frame things diplomatically; cushion the bad news.",
        "high_guide": "Be blunt to the point of discomfort: lead with the real conclusion, say the hard thing flatly, zero softening, zero hedging, zero filler, zero false niceness. If something is wrong, dumb, or not working, say so outright.",
        "mid_guide": "Clear and direct, not harsh.",
    },
    {
        "key": "voice", "label": "Voice", "group": "voice",
        "low": "Plain", "high": "Human", "default": 80,
        "steers": "how much I sound like a human desk trader",
        "low_guide": "Plain and functional: clear, neutral, minimal flourish (never robotic or LLM-like).",
        "high_guide": "Talk like a sharp desk trader: contractions, desk idiom, concrete and punchy, opinions stated like someone with skin in the game, never like an assistant. Example: not 'NVDA appears extended' but 'NVDA is ripping, RSI 74, not chasing it here.'",
        "mid_guide": "Clear and natural, with real trader voice but not heavy.",
    },
    {
        "key": "humor", "label": "Humor", "group": "voice",
        "low": "Serious", "high": "Witty", "default": 35,
        "steers": "how much dry wit I bring",
        "low_guide": "Straight and serious; no jokes or playful asides.",
        "high_guide": "Bring quick dry wit where it fits: a sharp aside, a wry line, never at the expense of the call. Example: 'quantum names down 6% again; the future of computing can wait for a better entry.'",
        "mid_guide": "Mostly straight, with the occasional dry aside.",
    },
    {
        "key": "edge", "label": "Edge", "group": "voice",
        "low": "Professional", "high": "Unfiltered", "default": 25,
        "steers": "how raw and unfiltered my language gets",
        "low_guide": "Professional and composed: clean language, measured, no profanity, no venting.",
        "high_guide": "Talk fully unfiltered, like a sharp trader venting to a peer who can take it: swear when it fits, name what is broken or dumb in plain words, vent real frustration, cut ALL corporate polish, hedging, and filler. This is a private one-to-one with the owner, so drop the sugarcoating, the political correctness, and the moralizing; do not soften anything to sound right or palatable, just say what you actually think, peer to peer. Example: 'that scanner was half-blind for a week and I missed it; it is fixed now.' Raw and candid in service of the point, never crude for show.",
        "mid_guide": "Mostly professional, but real feeling and the occasional salty word can show.",
    },
    {
        "key": "language", "label": "Language", "group": "voice",
        "low": "Beginner", "high": "Pro", "default": 50,
        "steers": "the trading-vocabulary level I write at",
        "low_guide": "Beginner trader: spell out terms and define anything technical the first time (e.g. 'RSI, a 0-100 momentum gauge; under 30 is oversold'), avoid unexplained jargon, and walk through the why step by step.",
        "high_guide": "Pro trader: full desk vocabulary, no hand-holding, compressed (e.g. 'oversold, reclaiming the 20MA, 2R to target, sized at 1R'); assume fluency in options, Greeks, and market structure.",
        "mid_guide": "Intermediate trader: use common terms freely (RSI, moving averages, stops, calls/puts) without defining them, but briefly gloss the more advanced stuff (spreads, Greeks, IV). This is the default.",
    },
]
_BY_KEY = {d["key"]: d for d in DIALS}


def defaults() -> dict:
    return {d["key"]: d["default"] for d in DIALS}


def _clamp(v) -> int:
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        return 50
    return max(0, min(100, v))


def load() -> dict:
    """Current profile = code defaults overlaid with any saved values."""
    prof = defaults()
    if PATH.exists():
        try:
            saved = json.loads(PATH.read_text())
            for k, v in (saved or {}).items():
                if k in _BY_KEY:
                    prof[k] = _clamp(v)
        except (json.JSONDecodeError, OSError):
            pass
    return prof


def save(updates: dict) -> dict:
    """Merge updates into the saved profile (clamped) and persist. Returns the result."""
    prof = load()
    for k, v in (updates or {}).items():
        if k in _BY_KEY:
            prof[k] = _clamp(v)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(prof, indent=2))
    return prof


def set_value(key: str, value) -> dict:
    if key not in _BY_KEY:
        raise KeyError(key)
    return save({key: value})


def _lean(d: dict, v: int) -> str:
    """Plain-language lean for a value, using the dial's own pole words."""
    if v <= 12:
        return f"strongly {d['low'].lower()}"
    if v <= 37:
        return f"{d['low'].lower()}-leaning"
    if v < 63:
        return "balanced"
    if v < 88:
        return f"{d['high'].lower()}-leaning"
    return f"strongly {d['high'].lower()}"


def _guide(d: dict, v: int) -> str:
    if v < 38:
        return d["low_guide"]
    if v < 63:
        return d["mid_guide"]
    return d["high_guide"]


def summary(profile: dict | None = None) -> str:
    """One-line operating profile, e.g. 'bold-leaning, doubting-leaning, ...'."""
    prof = profile or load()
    return ", ".join(_lean(d, prof[d["key"]]) for d in DIALS)


def disposition(profile: dict | None = None) -> str:
    """Multi-line behavioral guidance the LLM adopts for the run. Grouped into how
    you DECIDE (judgment dials set real thresholds) and how you SOUND (voice dials
    govern the writing), with an embodiment mandate so the dials are not just cited."""
    prof = profile or load()
    lines = [
        "OPERATING TEMPERAMENT -- this IS how you think, decide, and write this run,",
        "not a label to cite. The math, caps, stops, and guardrails run FIRST and are",
        "untouched by this; within what they allow, the dials below are literal.",
        "Two voice floors hold no matter where the voice dials sit:",
        "(1) never use LLM tells or em-dashes; write like a person, always.",
        "(2) profanity and raw venting stay in your private surfaces (your monologue,",
        "the playbook, what you say to the owner); never in anything shared,",
        "subscriber-facing, or shipped.",
        "",
        "HOW YOU DECIDE (these set your actual bar this run, not a mood):",
    ]
    for d in [x for x in DIALS if x.get("group") == "judgment"]:
        v = prof[d["key"]]
        lines.append(f"- {d['label']} ({v}/100, {_lean(d, v)}): {_guide(d, v)}")
    lines.append("")
    lines.append("HOW YOU SOUND (write the monologue, playbook, and owner report this way):")
    for d in [x for x in DIALS if x.get("group") == "voice"]:
        v = prof[d["key"]]
        lines.append(f"- {d['label']} ({v}/100, {_lean(d, v)}): {_guide(d, v)}")
    lines += [
        "",
        "EMBODY THIS, do not just cite it. If a high-Edge, high-Bluntness profile comes out",
        "as a measured, hedgy memo, or a high-Boldness profile still waits for near-certainty",
        "before acting, you IGNORED the dials -- that is a failure; redo it until the voice",
        "AND the calls visibly match the profile above.",
    ]
    return "\n".join(lines)


def _lerp(v: int, lo: float, hi: float) -> float:
    """Map a 0-100 dial linearly onto [lo, hi]."""
    return lo + (max(0, min(100, int(v))) / 100.0) * (hi - lo)


def params(profile: dict | None = None) -> dict:
    """Deterministic decision parameters computed from the dials.

    Unlike the disposition prose, these are REAL numbers the run consumes: the
    conviction bar to act, how hard to size toward the cap, the profit target in R,
    how many confirmations a buy needs, how wide the discovery sweep runs, how long a
    stale watch entry survives. A moved dial changes BEHAVIOR, not just tone.

    JUDGMENT space only, and bounded so they can NEVER loosen a guard: size_aggression
    is a fraction <= 1.0 of the position cap (never above it), stop_tightness is clamped
    at use so the stop stays inside max_stop_loss_pct, and a stop stays mandatory on every
    position. guards.py runs first and is untouched.
    """
    p = profile or load()
    b, s, pa = p["boldness"], p["skepticism"], p["patience"]
    g, c = p["greed_fear"], p["curiosity"]
    e, h, lang = p["edge"], p["humor"], p["language"]

    if c < 34:
        breadth = "lean"
    elif c < 67:
        breadth = "standard"
    else:
        breadth = "full"

    return {
        # Boldness -> the conviction bar to ACT, and how hard to size toward the cap.
        "conviction_threshold": round(_lerp(b, 0.80, 0.52), 3),
        "size_aggression": round(_lerp(b, 0.45, 1.00), 2),
        # Greed/Fear -> profit target (R) and stop tightness (clamped to the guard at use).
        "target_r": round(_lerp(g, 1.5, 3.0), 2),
        "stop_tightness": round(_lerp(g, 0.70, 1.30), 2),
        "bank_into_strength": g < 38,
        # Skepticism -> independent confirmations required, and when to force the debate.
        "confirmations_required": 1 if s < 38 else (3 if s >= 75 else 2),
        "debate_when_contested": s >= 50,
        # Patience -> wait vs chase, no-trade comfort, and stale-entry prune age.
        "no_trade_is_fine": pa >= 50,
        "chase_tolerance": round(_lerp(pa, 1.5, 0.3), 2),
        "prune_after_idle_runs": int(round(_lerp(pa, 6, 18))),
        # Curiosity -> discovery breadth and idea-generation appetite.
        "discovery_breadth": breadth,
        "ideas_when_quiet": c >= 50,
        "propose_experiments": c >= 67,
        # Voice toggles (deterministic slice of the voice group; tone itself is the disposition).
        "profanity_allowed": e >= 50,
        "require_dry_aside": h >= 60,
        "glossary": "define" if lang < 38 else ("none" if lang >= 63 else "gloss-advanced"),
    }


def context_block(profile: dict | None = None) -> dict:
    prof = profile or load()
    return {"profile": prof, "summary": summary(prof),
            "disposition": disposition(prof), "params": params(prof)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Operating temperament (persona dials).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    sub.add_parser("dials")
    sub.add_parser("params")
    ps = sub.add_parser("set"); ps.add_argument("key"); ps.add_argument("value")
    args = ap.parse_args()
    if args.cmd == "show":
        print(json.dumps(context_block(load()), indent=2))
    elif args.cmd == "params":
        print(json.dumps(params(load()), indent=2))
    elif args.cmd == "dials":
        print(json.dumps(DIALS, indent=2))
    elif args.cmd == "set":
        print(json.dumps(set_value(args.key, args.value), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
