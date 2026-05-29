#!/usr/bin/env python3
"""Mind memory -- the autonomous trader's indexed long-term memory.

Six buckets hold what the mind has learned. Each entry is a small CARD in an
always-loaded index, plus a BODY file that is pulled only on demand. This keeps
orientation cheap: the mind scans the headline cards, then loads only the handful
of bodies that matter to the run, instead of dragging everything into context.

Buckets:
  tickers        per-symbol convictions and how each name actually trades
  patterns       behavioral / market patterns that repeat (gap-and-fade, chop, ...)
  decisions      what I chose, why, and how it turned out (interpreted ledger)
  self           my blind spots, mistakes I repeat, things about my own process
  market_regime  the current backdrop and how it has shifted
  sources        which signals / scanners actually pay off vs which are noise

Layout (state/mind/memory/, gitignored):
  index.json            cards only: {id, bucket, headline, tags, strength, status,
                                     created, touched, history[]}
  <bucket>/<id>.md      the body text, read on demand

Lifecycle -- nothing hard-deletes. A stale card is DEMOTED (status -> cold): it
drops off the hot index so it stops costing context, but it stays retrievable and
comes back warm with its history if the subject resurfaces. A belief that turned
out wrong is demoted WITH its refutation attached, so resurfacing teaches the mind
instead of misleading it. Being wrong is one of the most useful things to remember.

Write resolution -- the MIND decides which of these to do; this tool only records:
  store         create a new card (run `recall` first; store warns on tag overlap)
  update        rewrite a card in place, optional "previously thought" history note
  merge         fold duplicate cards into one, demote the losers as merged
  demote / wake move a card off / back onto the hot index
  rollup        summarize many old cards into one (e.g. a quarter of decisions)

CLI:
  memory.py index   [--bucket B] [--json]
  memory.py recall  --tags t1,t2 [--bucket B] [--json]
  memory.py load    <id>
  memory.py recent  [--bucket B] [--n 10] [--json]
  memory.py store   --bucket B --headline H --tags t1,t2 [--strength 60] --body "..."
  memory.py update  <id> [--headline H] [--tags t1,t2] [--strength N] [--body "..."] [--note "..."]
  memory.py merge   <id1> <id2> ... --into <id> [--note "..."]
  memory.py demote  <id> [--note "..."]
  memory.py wake    <id>
  memory.py rollup  --bucket B --headline H --ids id1,id2 --body "..." [--tags t1,t2]
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

DIR = Path(__file__).resolve().parent
MEM_DIR = DIR / "state" / "mind" / "memory"
INDEX_PATH = MEM_DIR / "index.json"

BUCKETS = ["tickers", "patterns", "decisions", "self", "market_regime", "sources"]


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _load_index() -> dict:
    if INDEX_PATH.exists():
        try:
            return json.loads(INDEX_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"cards": []}


def _save_index(idx: dict) -> None:
    MEM_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(idx, indent=2))


def _body_path(bucket: str, cid: str) -> Path:
    return MEM_DIR / bucket / f"{cid}.md"


def _find(idx: dict, cid: str) -> dict | None:
    for c in idx["cards"]:
        if c["id"] == cid:
            return c
    return None


def _tags(raw) -> list[str]:
    if isinstance(raw, str):
        raw = raw.split(",")
    return sorted({t.strip().lower() for t in (raw or []) if t and t.strip()})


def _write_body(card: dict, body: str) -> None:
    p = _body_path(card["bucket"], card["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tagline = " ".join(f"#{t}" for t in card["tags"])
    head = [
        f"# {card['headline']}",
        f"_bucket: {card['bucket']} | strength: {card['strength']} | status: {card['status']}_",
        f"_id: {card['id']} | created: {card['created']} | touched: {card['touched']}_",
        f"_tags: {tagline}_",
        "",
        body.strip(),
        "",
    ]
    if card.get("history"):
        head.append("---")
        head.append("history:")
        for h in card["history"]:
            head.append(f"- {h['ts'][:19]}: {h['note']}")
        head.append("")
    p.write_text("\n".join(head))


def _read_body(card: dict) -> str:
    p = _body_path(card["bucket"], card["id"])
    if p.exists():
        return p.read_text()
    return f"(body missing for {card['id']})"


# ----- write operations ----------------------------------------------------

def store(bucket: str, headline: str, tags, body: str, strength: int = 60) -> dict:
    if bucket not in BUCKETS:
        raise ValueError(f"unknown bucket '{bucket}'; pick one of {BUCKETS}")
    idx = _load_index()
    card = {
        "id": _short_id(), "bucket": bucket, "headline": headline.strip(),
        "tags": _tags(tags), "strength": int(strength), "status": "hot",
        "created": _now(), "touched": _now(),
        "history": [{"ts": _now(), "note": "created"}],
    }
    idx["cards"].append(card)
    _save_index(idx)
    _write_body(card, body)
    return card


def update(cid: str, headline: str | None = None, tags=None,
           strength: int | None = None, body: str | None = None,
           note: str = "") -> dict | None:
    idx = _load_index()
    card = _find(idx, cid)
    if not card:
        return None
    if headline is not None:
        card["headline"] = headline.strip()
    if tags is not None:
        card["tags"] = _tags(tags)
    if strength is not None:
        card["strength"] = int(strength)
    card["touched"] = _now()
    card["status"] = "hot"  # touching a card re-warms it
    if note:
        card.setdefault("history", []).append({"ts": _now(), "note": note})
    _save_index(idx)
    if body is not None:
        _write_body(card, body)
    else:
        _write_body(card, _strip_body(_read_body(card)))
    return card


def _strip_body(rendered: str) -> str:
    """Recover just the body text from a rendered .md (drop header + history)."""
    lines = rendered.splitlines()
    out, started = [], False
    for ln in lines:
        if not started:
            # header runs until the first blank line after the _tags_ line
            if ln.startswith("_tags:"):
                started = True
            continue
        if ln.strip() == "---":
            break
        out.append(ln)
    return "\n".join(out).strip()


def merge(ids: list[str], into: str, note: str = "") -> dict | None:
    idx = _load_index()
    winner = _find(idx, into)
    if not winner:
        return None
    pieces = [_strip_body(_read_body(winner))]
    folded_tags = set(winner["tags"])
    for cid in ids:
        if cid == into:
            continue
        loser = _find(idx, cid)
        if not loser:
            continue
        pieces.append(f"\n(merged from {cid} -- {loser['headline']})\n"
                      + _strip_body(_read_body(loser)))
        folded_tags |= set(loser["tags"])
        loser["status"] = "merged"
        loser["touched"] = _now()
        loser.setdefault("history", []).append(
            {"ts": _now(), "note": f"folded into {into}"})
    winner["tags"] = sorted(folded_tags)
    winner["touched"] = _now()
    winner.setdefault("history", []).append(
        {"ts": _now(), "note": note or f"merged {', '.join(ids)}"})
    _save_index(idx)
    _write_body(winner, "\n".join(pieces).strip())
    return winner


def demote(cid: str, note: str = "") -> dict | None:
    idx = _load_index()
    card = _find(idx, cid)
    if not card:
        return None
    card["status"] = "cold"
    card["touched"] = _now()
    card.setdefault("history", []).append(
        {"ts": _now(), "note": f"demoted: {note}" if note else "demoted (stale)"})
    _save_index(idx)
    _write_body(card, _strip_body(_read_body(card)))
    return card


def wake(cid: str) -> dict | None:
    idx = _load_index()
    card = _find(idx, cid)
    if not card:
        return None
    card["status"] = "hot"
    card["touched"] = _now()
    card.setdefault("history", []).append({"ts": _now(), "note": "woken (resurfaced)"})
    _save_index(idx)
    _write_body(card, _strip_body(_read_body(card)))
    return card


def rollup(bucket: str, headline: str, ids: list[str], body: str, tags=None) -> dict:
    """Summarize several old cards into one new card; demote the originals."""
    summary_card = store(bucket, headline, tags or [], body, strength=50)
    for cid in ids:
        demote(cid, note=f"rolled up into {summary_card['id']}")
    return summary_card


# ----- read operations -----------------------------------------------------

def recall(tags, bucket: str | None = None) -> list[dict]:
    want = set(_tags(tags))
    idx = _load_index()
    out = []
    for c in idx["cards"]:
        if c["status"] == "merged":
            continue
        if bucket and c["bucket"] != bucket:
            continue
        if want & set(c["tags"]):
            out.append(c)
    out.sort(key=lambda c: (c["status"] != "hot", -c["strength"], c["touched"]),
             reverse=False)
    return out


def index(bucket: str | None = None) -> list[dict]:
    idx = _load_index()
    cards = [c for c in idx["cards"]
             if c["status"] == "hot" and (not bucket or c["bucket"] == bucket)]
    cards.sort(key=lambda c: (c["bucket"], -c["strength"]))
    return cards


def recent(bucket: str | None = None, n: int = 10) -> list[dict]:
    idx = _load_index()
    cards = [c for c in idx["cards"]
             if c["status"] != "merged" and (not bucket or c["bucket"] == bucket)]
    cards.sort(key=lambda c: c["touched"], reverse=True)
    return cards[:n]


def all_cards() -> list[dict]:
    """Every card including cold and merged -- for counts and self-audits."""
    return _load_index().get("cards", [])


def counts() -> dict:
    """Per-bucket tally of hot / cold / merged cards (for the self-audit read)."""
    out = {}
    for c in all_cards():
        b = out.setdefault(c["bucket"], {"hot": 0, "cold": 0, "merged": 0})
        b[c["status"]] = b.get(c["status"], 0) + 1
    return out


# ----- rendering -----------------------------------------------------------

def _fmt_card(c: dict) -> str:
    flags = " ".join(f"#{t}" for t in c["tags"])
    cold = "" if c["status"] == "hot" else f" ({c['status']})"
    return (f"{c['bucket']}/{c['id']} [{c['strength']}]{cold} {c['headline']}"
            f"  {flags}  (touched {c['touched'][:10]})")


def _print_cards(cards: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(cards, indent=2))
        return
    if not cards:
        print("(none)")
        return
    for c in cards:
        print(_fmt_card(c))


def main() -> int:
    ap = argparse.ArgumentParser(description="Mind memory (six indexed buckets).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index"); pi.add_argument("--bucket"); pi.add_argument("--json", action="store_true")
    pr = sub.add_parser("recall"); pr.add_argument("--tags", required=True); pr.add_argument("--bucket"); pr.add_argument("--json", action="store_true")
    pl = sub.add_parser("load"); pl.add_argument("id")
    pn = sub.add_parser("recent"); pn.add_argument("--bucket"); pn.add_argument("--n", type=int, default=10); pn.add_argument("--json", action="store_true")
    ps = sub.add_parser("store")
    ps.add_argument("--bucket", required=True); ps.add_argument("--headline", required=True)
    ps.add_argument("--tags", default=""); ps.add_argument("--strength", type=int, default=60)
    ps.add_argument("--body", required=True)
    pu = sub.add_parser("update"); pu.add_argument("id")
    pu.add_argument("--headline"); pu.add_argument("--tags"); pu.add_argument("--strength", type=int)
    pu.add_argument("--body"); pu.add_argument("--note", default="")
    pm = sub.add_parser("merge"); pm.add_argument("ids", nargs="+"); pm.add_argument("--into", required=True); pm.add_argument("--note", default="")
    pd = sub.add_parser("demote"); pd.add_argument("id"); pd.add_argument("--note", default="")
    pw = sub.add_parser("wake"); pw.add_argument("id")
    pp = sub.add_parser("rollup")
    pp.add_argument("--bucket", required=True); pp.add_argument("--headline", required=True)
    pp.add_argument("--ids", required=True); pp.add_argument("--body", required=True); pp.add_argument("--tags", default="")
    args = ap.parse_args()

    if args.cmd == "index":
        _print_cards(index(args.bucket), args.json)
    elif args.cmd == "recall":
        cards = recall(args.tags, args.bucket)
        if not args.json and cards:
            print(f"-- {len(cards)} match(es) for tags={args.tags} "
                  f"(consider update/merge before storing a duplicate) --")
        _print_cards(cards, args.json)
    elif args.cmd == "load":
        idx = _load_index(); card = _find(idx, args.id)
        if not card:
            print(f"(no card {args.id})"); return 1
        print(_read_body(card))
    elif args.cmd == "recent":
        _print_cards(recent(args.bucket, args.n), args.json)
    elif args.cmd == "store":
        # soft duplicate warning: surface tag-overlapping cards in the same bucket
        dupes = [c for c in recall(args.tags, args.bucket) if c["status"] == "hot"]
        if dupes:
            print(f"-- heads up: {len(dupes)} existing card(s) share these tags; "
                  f"update/merge may be better than a new card --")
            for c in dupes[:5]:
                print("  " + _fmt_card(c))
        c = store(args.bucket, args.headline, args.tags, args.body, args.strength)
        print(f"stored {c['bucket']}/{c['id']}")
    elif args.cmd == "update":
        c = update(args.id, args.headline, args.tags, args.strength, args.body, args.note)
        print(f"updated {c['bucket']}/{c['id']}" if c else f"(no card {args.id})")
        return 0 if c else 1
    elif args.cmd == "merge":
        c = merge(args.ids, args.into, args.note)
        print(f"merged into {c['bucket']}/{c['id']}" if c else f"(no card {args.into})")
        return 0 if c else 1
    elif args.cmd == "demote":
        c = demote(args.id, args.note)
        print(f"demoted {args.id}" if c else f"(no card {args.id})")
        return 0 if c else 1
    elif args.cmd == "wake":
        c = wake(args.id)
        print(f"woke {args.id}" if c else f"(no card {args.id})")
        return 0 if c else 1
    elif args.cmd == "rollup":
        c = rollup(args.bucket, args.headline,
                   [i for i in args.ids.split(",") if i], args.body, args.tags)
        print(f"rolled up into {c['bucket']}/{c['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
