#!/usr/bin/env python3
"""whats_working_well / whats_not_working -- JSON source, rendered to MD.

The system logs a reflection ONLY when something is genuinely worth flagging (good
or bad) -- not every run. The JSON (state/reflections.json) is the deterministic
source: dedup by id, tagged, status-tracked. The .md files are rendered from it so
they're always clean, deduped, grouped, and obsolete entries are dropped. Files
live in state/ (gitignored, persist on disk, greppable).

Entry: {id, summary, detail, tags[], status(active|resolved|obsolete),
        created, updated, hits}

CLI:
  reflections.py flag working   <id> "summary" --tags t1,t2 [--detail ...]
  reflections.py flag not       <id> "summary" --tags t1,t2 [--detail ...]
  reflections.py resolve <kind> <id>     # mark resolved (fixed/played out)
  reflections.py retire  <kind> <id>     # mark obsolete (no longer relevant)
  reflections.py render                  # rewrite both .md from JSON
  reflections.py show <kind>             # print active entries (json)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DIR = Path(__file__).resolve().parent
STATE_DIR = DIR / "state"
JSON_PATH = STATE_DIR / "reflections.json"
MD = {"working": STATE_DIR / "whats_working_well.md",
      "not_working": STATE_DIR / "whats_not_working.md"}
_KIND = {"working": "working", "work": "working", "not": "not_working",
         "not_working": "not_working"}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load() -> dict:
    if JSON_PATH.exists():
        try:
            return json.loads(JSON_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"working": [], "not_working": []}


def _save(d: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(d, indent=2))


def flag(kind: str, entry_id: str, summary: str, tags=None, detail: str = "") -> dict:
    """Upsert a reflection (dedup by id). Re-flagging an existing id bumps `hits`
    and refreshes summary/detail/updated -- so repeated issues accrue weight."""
    kind = _KIND.get(kind, kind)
    d = _load()
    bucket = d.setdefault(kind, [])
    tags = tags or []
    for e in bucket:
        if e["id"] == entry_id:
            e.update(summary=summary, detail=detail or e.get("detail", ""),
                     tags=sorted(set(e.get("tags", []) + tags)), status="active",
                     updated=_now(), hits=e.get("hits", 1) + 1)
            _save(d)
            return e
    e = {"id": entry_id, "summary": summary, "detail": detail, "tags": sorted(set(tags)),
         "status": "active", "created": _now(), "updated": _now(), "hits": 1}
    bucket.append(e)
    _save(d)
    return e


def _set_status(kind: str, entry_id: str, status: str,
                outcome: str = "", note: str = "", ref: str = "") -> bool:
    """Set status and, when given, record WHAT closed it: resolution =
    {outcome, note, ref}. outcome is auto_resolved | escalated | no_action;
    ref links the change_log/approval id that carries the action."""
    kind = _KIND.get(kind, kind)
    d = _load()
    for e in d.get(kind, []):
        if e["id"] == entry_id:
            e["status"] = status
            e["updated"] = _now()
            if outcome or note or ref:
                e["resolution"] = {"outcome": outcome, "note": note, "ref": ref}
            _save(d)
            return True
    return False


def resolve(kind, entry_id, outcome="auto_resolved", note="", ref=""):
    """Close a reflection that has been ACTED on. outcome=auto_resolved (I fixed
    it) or escalated (raised to the user via approvals; ref=approval id)."""
    return _set_status(kind, entry_id, "resolved", outcome, note, ref)


def retire(kind, entry_id, note="", ref=""):
    """Close a reflection that needs no action (obsolete / no longer relevant)."""
    return _set_status(kind, entry_id, "obsolete", "no_action", note, ref)


def active(kind: str) -> list:
    kind = _KIND.get(kind, kind)
    return [e for e in _load().get(kind, []) if e["status"] == "active"]


def _render_one(kind: str, title: str) -> None:
    d = _load()
    entries = [e for e in d.get(kind, []) if e["status"] != "obsolete"]
    # group by first tag, active before resolved, most-hit first
    entries.sort(key=lambda e: (e["status"] != "active", (e["tags"] or ["~"])[0], -e.get("hits", 1)))
    lines = [f"# {title}", "",
             "_Rendered from state/reflections.json -- do not hand-edit; use reflections.py._",
             f"_Updated {_now()}_", ""]
    cur_tag = None
    for e in entries:
        tag0 = (e["tags"] or ["untagged"])[0]
        if tag0 != cur_tag:
            cur_tag = tag0
            lines.append(f"## {tag0}")
        flags = " ".join(f"#{t}" for t in e["tags"])
        stat = "" if e["status"] == "active" else f" _({e['status']})_"
        hits = f" x{e['hits']}" if e.get("hits", 1) > 1 else ""
        lines.append(f"- **{e['id']}**{stat}{hits}: {e['summary']}  {flags}")
        if e.get("detail"):
            lines.append(f"    - {e['detail']}")
        res = e.get("resolution") or {}
        if res.get("outcome") or res.get("note"):
            ref = f" [{res['ref']}]" if res.get("ref") else ""
            lines.append(f"    - _resolution: {res.get('outcome','')} {res.get('note','')}{ref}_".rstrip())
    MD[kind].parent.mkdir(parents=True, exist_ok=True)
    MD[kind].write_text("\n".join(lines) + "\n")


def render() -> None:
    _render_one("working", "What's Working Well")
    _render_one("not_working", "What's Not Working")


def main() -> int:
    ap = argparse.ArgumentParser(description="Reflection logs (working / not working).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("flag")
    pf.add_argument("kind"); pf.add_argument("id"); pf.add_argument("summary")
    pf.add_argument("--tags", default=""); pf.add_argument("--detail", default="")
    for name in ("resolve", "retire"):
        p = sub.add_parser(name); p.add_argument("kind"); p.add_argument("id")
    sub.add_parser("render")
    ps = sub.add_parser("show"); ps.add_argument("kind")
    args = ap.parse_args()
    if args.cmd == "flag":
        e = flag(args.kind, args.id, args.summary,
                 [t for t in args.tags.split(",") if t], args.detail)
        render(); print(json.dumps(e))
    elif args.cmd in ("resolve", "retire"):
        ok = (resolve if args.cmd == "resolve" else retire)(args.kind, args.id)
        render(); print("ok" if ok else "not found")
    elif args.cmd == "render":
        render(); print("rendered")
    elif args.cmd == "show":
        print(json.dumps(active(args.kind), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
