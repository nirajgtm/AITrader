#!/usr/bin/env python3
"""historical_changes + rejected-proposals -- JSON source, rendered to tagged MD.

`changes`  = the timeline of every applied amendment / undo / sunset-revert.
`rejected` = amendment theses that were analyzed and rejected, with WHY -- so the
             LLM greps this before re-proposing a similar idea (and must address
             why it failed last time, or not re-propose).

Both are tagged + indexed so the .md is greppable (e.g. `grep '#risk'`,
`grep '#stop'`, `grep 2026-05`). JSON in state/ (gitignored, persists, greppable).

CLI:
  change_log.py change "area" "what changed" --tags t1,t2 [--rationale ...] [--for ...] [--against ...]
  change_log.py reject "thesis" --tags t1,t2 --reason "why"
  change_log.py find-rejected t1,t2        # similar past rejections (grep helper)
  change_log.py render
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

DIR = Path(__file__).resolve().parent
STATE_DIR = DIR / "state"
JSON_PATH = STATE_DIR / "change_log.json"
CHANGES_MD = STATE_DIR / "historical_changes.md"
REJECTED_MD = STATE_DIR / "rejected_proposals.md"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load() -> dict:
    if JSON_PATH.exists():
        try:
            return json.loads(JSON_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"changes": [], "rejected": []}


def _save(d: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(d, indent=2))


def record_change(area: str, what: str, *, tags=None, rationale: str = "",
                  for_summary: str = "", against_summary: str = "",
                  change: dict | None = None, sunset: dict | None = None,
                  kind: str = "amendment") -> dict:
    d = _load()
    e = {"id": uuid.uuid4().hex[:8], "ts": _now(), "kind": kind, "area": area,
         "what": what, "tags": sorted(set(tags or [])), "rationale": rationale,
         "for": for_summary, "against": against_summary, "change": change or {},
         "sunset": sunset or {}, "status": "active"}
    d["changes"].append(e)
    _save(d)
    return e


def record_rejection(thesis: str, *, tags=None, reason: str = "",
                     for_summary: str = "", against_summary: str = "") -> dict:
    d = _load()
    e = {"id": uuid.uuid4().hex[:8], "ts": _now(), "thesis": thesis,
         "tags": sorted(set(tags or [])), "reason": reason,
         "for": for_summary, "against": against_summary}
    d["rejected"].append(e)
    _save(d)
    return e


def changes() -> list:
    return _load()["changes"]


def rejected() -> list:
    return _load()["rejected"]


def find_rejected(tags: list[str]) -> list:
    """Past rejections sharing any tag -- check before re-proposing."""
    want = set(t.lower() for t in tags)
    return [r for r in rejected() if want & set(t.lower() for t in r["tags"])]


def render() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    d = _load()
    ch = sorted(d["changes"], key=lambda e: e["ts"], reverse=True)
    lines = ["# Historical Changes (timeline)", "",
             "_Rendered from state/change_log.json. Grep by #tag or date (YYYY-MM)._",
             f"_Updated {_now()}_", ""]
    for e in ch:
        tags = " ".join(f"#{t}" for t in e["tags"])
        lines.append(f"## {e['ts'][:10]} -- {e['area']}: {e['what']}  [#{e['kind']}] {tags}")
        if e.get("change"):
            lines.append(f"- change: {json.dumps(e['change'])}")
        if e.get("rationale"):
            lines.append(f"- rationale: {e['rationale']}")
        if e.get("for"):
            lines.append(f"- FOR: {e['for']}")
        if e.get("against"):
            lines.append(f"- AGAINST: {e['against']}")
        if e.get("sunset"):
            lines.append(f"- sunset: {json.dumps(e['sunset'])}")
        lines.append("")
    CHANGES_MD.write_text("\n".join(lines) + "\n")

    rj = sorted(d["rejected"], key=lambda e: e["ts"], reverse=True)
    rl = ["# Rejected Proposals", "",
          "_Theses analyzed and rejected. Grep before re-proposing -- if a similar",
          "idea appears here, address why it failed last time or do not re-propose._",
          f"_Updated {_now()}_", ""]
    for e in rj:
        tags = " ".join(f"#{t}" for t in e["tags"])
        rl.append(f"## {e['ts'][:10]} -- REJECTED: {e['thesis']}  {tags}")
        rl.append(f"- why: {e['reason']}")
        if e.get("against"):
            rl.append(f"- AGAINST: {e['against']}")
        rl.append("")
    REJECTED_MD.write_text("\n".join(rl) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Historical changes + rejected proposals.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("change")
    pc.add_argument("area"); pc.add_argument("what")
    pc.add_argument("--tags", default=""); pc.add_argument("--rationale", default="")
    pc.add_argument("--for", dest="for_s", default=""); pc.add_argument("--against", default="")
    pr = sub.add_parser("reject")
    pr.add_argument("thesis"); pr.add_argument("--tags", default=""); pr.add_argument("--reason", default="")
    pf = sub.add_parser("find-rejected"); pf.add_argument("tags")
    sub.add_parser("render")
    args = ap.parse_args()
    if args.cmd == "change":
        record_change(args.area, args.what, tags=[t for t in args.tags.split(",") if t],
                      rationale=args.rationale, for_summary=args.for_s, against_summary=args.against)
        render(); print("recorded")
    elif args.cmd == "reject":
        record_rejection(args.thesis, tags=[t for t in args.tags.split(",") if t], reason=args.reason)
        render(); print("recorded")
    elif args.cmd == "find-rejected":
        print(json.dumps(find_rejected([t for t in args.tags.split(",") if t]), indent=2))
    elif args.cmd == "render":
        render(); print("rendered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
