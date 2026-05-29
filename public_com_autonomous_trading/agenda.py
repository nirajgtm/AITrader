#!/usr/bin/env python3
"""Agenda -- the mind's open thought queue (state/mind/agenda.json).

This is what the inner monologue keeps thinking about ACROSS runs: unresolved
thesis questions, held tensions (a belief now under doubt), experiments it is
tracking, ideas it wants to chew on. The agenda is SELF-AUTHORED -- only the
monologue writes to it; the user observes it through the Playbook, never edits it.

Every open item carries a disposition the synthesis assigns each run:
  decide    act on it this run
  park      keep chewing, no action yet  (the default -- most thoughts just sit)
  escalate  send it to the Decisions tab (approvals.py) for the user

Items close exactly three ways:
  resolved   the monologue answered it (and wrote the conclusion to memory)
  expired    no new signal for a while -- it ages out, with a note, in case it returns
  escalated  it became a real decision; the Decisions queue now tracks the outcome

TENSIONS are a special item type. When a new observation contradicts an existing
belief, the monologue opens a tension that REFERENCES the memory card under doubt
and carries a for/against counter. Each run adds weight (`tick`). When the weight
crosses the bar -- a bar that TEMPERAMENT moves: high skepticism lowers it, high
patience raises it -- the monologue flips the belief (memory.update) and resolves
the tension; if the belief is re-confirmed instead, the tension resolves and the
belief comes out stronger. The tool only tracks the counter; the MIND judges the bar.

Item: {id, ts, type(thought|tension|experiment), title, detail, ref(memory id),
       status(open|resolved|expired|escalated), disposition(decide|park|escalate),
       counter{for,against} (tensions), idle_runs, created, updated,
       resolution{outcome,note,ref}}

CLI:
  agenda.py list [--all] [--json]
  agenda.py add --type T --title "..." [--detail ...] [--ref memId] [--disposition park]
  agenda.py touch <id>                       # seen this run; resets idle counter
  agenda.py tick <id> --for | --against [--note ...]   # advance a tension counter
  agenda.py disposition <id> decide|park|escalate
  agenda.py resolve <id> [--note ...] [--ref ...]
  agenda.py escalate <id> [--ref approvalId] [--note ...]
  agenda.py age [--expire-after 12]          # bump idle on untouched-this-run; expire stale
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

DIR = Path(__file__).resolve().parent
PATH = DIR / "state" / "mind" / "agenda.json"

TYPES = ("thought", "tension", "experiment")
DISPOSITIONS = ("decide", "park", "escalate")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load() -> list:
    if PATH.exists():
        try:
            return json.loads(PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save(items: list) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(items, indent=2))


def _find(items: list, iid: str) -> dict | None:
    for it in items:
        if it["id"] == iid:
            return it
    return None


def add(item_type: str, title: str, detail: str = "", ref: str = "",
        disposition: str = "park") -> dict:
    if item_type not in TYPES:
        raise ValueError(f"type must be one of {TYPES}")
    if disposition not in DISPOSITIONS:
        disposition = "park"
    items = _load()
    it = {
        "id": uuid.uuid4().hex[:8], "ts": _now(), "type": item_type,
        "title": title.strip(), "detail": detail, "ref": ref,
        "status": "open", "disposition": disposition,
        "counter": {"for": 0, "against": 0} if item_type == "tension" else None,
        "idle_runs": 0, "created": _now(), "updated": _now(),
        "resolution": None,
    }
    items.append(it)
    _save(items)
    return it


def touch(iid: str) -> dict | None:
    items = _load()
    it = _find(items, iid)
    if not it:
        return None
    it["idle_runs"] = 0
    it["updated"] = _now()
    _save(items)
    return it


def tick(iid: str, side: str, note: str = "") -> dict | None:
    items = _load()
    it = _find(items, iid)
    if not it or it["type"] != "tension":
        return None
    it.setdefault("counter", {"for": 0, "against": 0})
    if side in ("for", "against"):
        it["counter"][side] += 1
    it["idle_runs"] = 0
    it["updated"] = _now()
    if note:
        it.setdefault("notes", []).append({"ts": _now(), "note": note})
    _save(items)
    return it


def set_disposition(iid: str, disposition: str) -> dict | None:
    if disposition not in DISPOSITIONS:
        return None
    items = _load()
    it = _find(items, iid)
    if not it:
        return None
    it["disposition"] = disposition
    it["updated"] = _now()
    _save(items)
    return it


def _close(iid: str, status: str, outcome: str, note: str, ref: str) -> dict | None:
    items = _load()
    it = _find(items, iid)
    if not it:
        return None
    it["status"] = status
    it["updated"] = _now()
    it["resolution"] = {"outcome": outcome, "note": note, "ref": ref}
    _save(items)
    return it


def resolve(iid: str, note: str = "", ref: str = "") -> dict | None:
    return _close(iid, "resolved", "resolved", note, ref)


def escalate(iid: str, ref: str = "", note: str = "") -> dict | None:
    it = _close(iid, "escalated", "escalated", note, ref)
    if it:
        it["disposition"] = "escalate"
        items = _load()
        _find(items, iid).update(it)
        _save(items)
    return it


def age(expire_after: int = 12) -> list:
    """Bump idle_runs on every open item, expire those untouched too long.
    Call ONCE per run AFTER the monologue has touched what it engaged with."""
    items = _load()
    expired = []
    for it in items:
        if it["status"] != "open":
            continue
        it["idle_runs"] = it.get("idle_runs", 0) + 1
        if it["idle_runs"] >= expire_after:
            it["status"] = "expired"
            it["updated"] = _now()
            it["resolution"] = {"outcome": "expired",
                                "note": f"no new signal for {it['idle_runs']} runs",
                                "ref": ""}
            expired.append(it)
    _save(items)
    return expired


def open_items() -> list:
    return [it for it in _load() if it["status"] == "open"]


# ----- rendering -----------------------------------------------------------

def _fmt(it: dict) -> str:
    head = f"{it['id']} [{it['type']}/{it['disposition']}] {it['title']}"
    if it["type"] == "tension" and it.get("counter"):
        c = it["counter"]
        head += f"  (for {c['for']} / against {c['against']})"
    if it.get("ref"):
        head += f"  ->{it['ref']}"
    if it["status"] == "open" and it.get("idle_runs"):
        head += f"  idle:{it['idle_runs']}"
    if it["status"] != "open":
        head += f"  <{it['status']}>"
    return head


def main() -> int:
    ap = argparse.ArgumentParser(description="Mind agenda (open thoughts + tensions).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list"); pl.add_argument("--all", action="store_true"); pl.add_argument("--json", action="store_true")
    pa = sub.add_parser("add")
    pa.add_argument("--type", required=True); pa.add_argument("--title", required=True)
    pa.add_argument("--detail", default=""); pa.add_argument("--ref", default="")
    pa.add_argument("--disposition", default="park")
    pt = sub.add_parser("touch"); pt.add_argument("id")
    pk = sub.add_parser("tick"); pk.add_argument("id")
    g = pk.add_mutually_exclusive_group(required=True)
    g.add_argument("--for", dest="for_side", action="store_true")
    g.add_argument("--against", dest="against_side", action="store_true")
    pk.add_argument("--note", default="")
    pdp = sub.add_parser("disposition"); pdp.add_argument("id"); pdp.add_argument("value", choices=DISPOSITIONS)
    prs = sub.add_parser("resolve"); prs.add_argument("id"); prs.add_argument("--note", default=""); prs.add_argument("--ref", default="")
    pes = sub.add_parser("escalate"); pes.add_argument("id"); pes.add_argument("--ref", default=""); pes.add_argument("--note", default="")
    pag = sub.add_parser("age"); pag.add_argument("--expire-after", type=int, default=12)
    args = ap.parse_args()

    if args.cmd == "list":
        items = _load() if args.all else open_items()
        if args.json:
            print(json.dumps(items, indent=2))
        elif not items:
            print("(agenda empty)")
        else:
            for it in items:
                print(_fmt(it))
    elif args.cmd == "add":
        it = add(args.type, args.title, args.detail, args.ref, args.disposition)
        print(f"added {it['id']} ({it['type']})")
    elif args.cmd == "touch":
        print("touched" if touch(args.id) else f"(no item {args.id})")
    elif args.cmd == "tick":
        side = "for" if args.for_side else "against"
        it = tick(args.id, side, args.note)
        print(_fmt(it) if it else f"(no tension {args.id})")
        return 0 if it else 1
    elif args.cmd == "disposition":
        print("set" if set_disposition(args.id, args.value) else f"(no item {args.id})")
    elif args.cmd == "resolve":
        print("resolved" if resolve(args.id, args.note, args.ref) else f"(no item {args.id})")
    elif args.cmd == "escalate":
        print("escalated" if escalate(args.id, args.ref, args.note) else f"(no item {args.id})")
    elif args.cmd == "age":
        exp = age(args.expire_after)
        print(f"aged; {len(exp)} expired" + (": " + ", ".join(e["id"] for e in exp) if exp else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
