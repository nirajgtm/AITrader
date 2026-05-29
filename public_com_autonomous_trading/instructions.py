#!/usr/bin/env python3
"""The user's instruction inbox (state/instructions.json).

The user drops free-form instructions from the dashboard; the autonomous run ingests
pending ones, the mind acts/escalates/absorbs each, then marks it processed with an
outcome note (which archives it on the dashboard). Mirrors approvals.py. The user
submits instructions; the mind only ever marks them processed, never adds them.

Item: {id, ts, text, tab, status(pending|processed), processed_ts, outcome, ref, images}
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

PATH = Path(__file__).resolve().parent / "state" / "instructions.json"


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


def add(text: str, tab: str = "Other", image_blobs=None) -> str:
    """User submits an instruction (from the dashboard). Optional image_blobs is a
    list of (bytes, ext); each is written to state/instruction_images/<id>_<n>.<ext>
    and its filename stored on the record so the mind can read it. Returns the id, or
    '' if BOTH text and images are empty."""
    text = (text or "").strip()
    image_blobs = image_blobs or []
    if not text and not image_blobs:
        return ""
    items = _load()
    rid = uuid.uuid4().hex[:8]
    images = []
    if image_blobs:
        imgdir = PATH.parent / "instruction_images"
        imgdir.mkdir(parents=True, exist_ok=True)
        for n, (data, ext) in enumerate(image_blobs):
            fname = f"{rid}_{n}.{ext}"
            (imgdir / fname).write_bytes(data)
            images.append(fname)
    items.append({"id": rid, "ts": _now(), "text": text, "tab": (tab or "Other"),
                  "status": "pending", "processed_ts": None, "outcome": "", "ref": "",
                  "images": images})
    _save(items)
    return rid


def pending() -> list:
    return [i for i in _load() if i.get("status") == "pending"]


def all_items() -> list:
    return _load()


def mark_processed(rid: str, outcome: str = "", ref: str = "") -> bool:
    """The mind marks an instruction processed (archived) with a one-line outcome and an
    optional ref (e.g. a Decisions/approval id it escalated to)."""
    items = _load()
    for i in items:
        if i.get("id") == rid:
            i["status"] = "processed"
            i["processed_ts"] = _now()
            i["outcome"] = outcome
            i["ref"] = ref
            _save(items)
            return True
    return False
