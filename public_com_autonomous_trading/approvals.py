#!/usr/bin/env python3
"""Async approval queue (state/approvals.json) -- the system NEVER waits.

When something needs the user's sign-off (a risk-loosening into new territory, a
bigger position size, a change to a control knob or guardrail), the system appends a
PENDING request here and continues. The user acts whenever via the dashboard
(approve/reject + reasoning), which mutates this file.

On a later run the system consumes decided requests: rejected -> log to the rejected
registry; approved/instructed -> surface the user's reasoning once. Approved items
ALSO enter an implementation lifecycle: they stay in pending_implementation() every
run until the mind actually applies the change and calls mark_implemented(), so an
approval can never be consumed-and-forgotten. Nothing blocks waiting for a human.

Request: {id, ts, category, summary, detail, proposal,
          status(pending|approved|rejected|instructed), decided_ts, reasoning,
          consumed, implemented, implemented_ts}
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

DIR = Path(__file__).resolve().parent
PATH = DIR / "state" / "approvals.json"


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


def request(category: str, summary: str, detail: str = "", proposal: dict | None = None) -> str:
    """Queue a pending approval and return its id. Does NOT block."""
    items = _load()
    # de-dupe: if an identical still-pending request exists, reuse it
    for it in items:
        if it["status"] == "pending" and it["category"] == category and it["summary"] == summary:
            return it["id"]
    rid = uuid.uuid4().hex[:8]
    items.append({"id": rid, "ts": _now(), "category": category, "summary": summary,
                  "detail": detail, "proposal": proposal or {}, "status": "pending",
                  "decided_ts": None, "reasoning": "", "consumed": False,
                  "implemented": False, "implemented_ts": None})
    _save(items)
    return rid


def pending() -> list:
    return [it for it in _load() if it["status"] == "pending"]


def all_items() -> list:
    return _load()


def decide(rid: str, decision: str, reasoning: str = "") -> bool:
    """Resolve a request (called by the dashboard). decision in
    {approved, rejected, instruct}. 'instruct' means the user is NOT giving a
    binary yes/no but free-text guidance to act on; `reasoning` carries the
    instruction and the run follows it next cycle (status -> 'instructed')."""
    if decision not in ("approved", "rejected", "instruct"):
        return False
    items = _load()
    for it in items:
        if it["id"] == rid and it["status"] == "pending":
            it["status"] = "instructed" if decision == "instruct" else decision
            it["decided_ts"] = _now()
            it["reasoning"] = reasoning
            _save(items)
            return True
    return False


def consume_decided() -> list:
    """Return decided-but-unconsumed requests and mark them consumed. The caller
    surfaces these so the run reads the user's reasoning once (approved -> begin
    implementing; rejected -> record). Idempotent across runs.

    NOTE: marking an approved item consumed only means the run has SEEN the decision,
    NOT that the change was applied. Implementation is tracked separately via
    pending_implementation() / mark_implemented(), so an approved item keeps
    surfacing until it is actually done."""
    items = _load()
    out = []
    changed = False
    for it in items:
        if it["status"] in ("approved", "rejected", "instructed") and not it.get("consumed"):
            out.append(dict(it))
            it["consumed"] = True
            changed = True
    if changed:
        _save(items)
    return out


def pending_implementation() -> list:
    """Approved requests that have NOT yet been implemented. These re-surface every
    run until the mind actually applies the change and calls mark_implemented().
    This is what stops an approved item from being consumed-and-forgotten -- the run
    is obligated to act on these. Items approved before this lifecycle existed (no
    'implemented' key) are treated as not implemented, so they correctly resurface."""
    return [it for it in _load()
            if it["status"] == "approved" and not it.get("implemented")]


def mark_implemented(rid: str, note: str = "") -> bool:
    """Mark an approved request as actually implemented: the change was applied this
    run and logged (e.g. to change_log / Evolution). Stops it re-surfacing in
    pending_implementation(). Only valid for an approved request."""
    items = _load()
    for it in items:
        if it["id"] == rid and it["status"] == "approved":
            it["implemented"] = True
            it["implemented_ts"] = _now()
            if note:
                it["implementation_note"] = note
            _save(items)
            return True
    return False
