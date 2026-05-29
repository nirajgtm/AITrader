#!/usr/bin/env python3
"""Risk-parameter state machine with an undo lineage (state/risk_state.json).

The EFFECTIVE risk parameters the system trades under live here, not in config.json
(config holds the seed defaults). Changes form an undo stack:
  - mutate  -> push a new state (snapshot of the risk params)
  - undo    -> pop to the immediate parent (cannot jump to an arbitrary state)

Policy (decided by the user): LOOSENING needs approval UNLESS it is in the undo
lineage. Concretely, a proposed change is AUTO-applied when it is a tightening
(every risk dim moves tighter-or-equal) OR it lands on a parameter set the system
has held before (an undo / re-visit). A change that loosens any dim into a
never-held value is held as pending_approval -- it does NOT take effect until the
user approves. Undo is always auto (it only returns to a value we already had).

Tighten direction (smaller = tighter) for every managed dim:
  max_position_usd, max_open_positions, max_stop_loss_pct, kill_switch_drawdown_usd.
require_stop_loss flipping to false counts as loosening.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DIR = Path(__file__).resolve().parent
STATE_PATH = DIR / "state" / "risk_state.json"
CONFIG_PATH = DIR / "config.json"

MANAGED = ["max_position_usd", "max_open_positions", "max_stop_loss_pct",
           "kill_switch_drawdown_usd"]


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sig(params: dict) -> str:
    """Canonical signature of the risk-relevant dims (for lineage membership)."""
    core = {k: params.get(k) for k in MANAGED}
    core["require_stop_loss"] = params.get("require_stop_loss", True)
    return json.dumps(core, sort_keys=True)


def _seed_defaults() -> dict:
    return json.loads(CONFIG_PATH.read_text())["risk"]


def _load() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    seed = _seed_defaults()
    st = {
        "stack": [{"params": seed, "reason": "seed from config.json", "type": "seed", "ts": _now()}],
        "seen": [_sig(seed)],
        "pending_approval": [],
    }
    _save(st)
    return st


def _save(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, indent=2))


def current_params() -> dict:
    """The risk params the system is trading under right now (top of the stack)."""
    return _load()["stack"][-1]["params"]


def classify(new: dict, current: dict | None = None, st: dict | None = None) -> str:
    """'tighten' | 'undo_lineage' | 'loosen_new'."""
    st = st or _load()
    current = current or st["stack"][-1]["params"]
    loosens = False
    for k in MANAGED:
        if float(new.get(k, current[k])) > float(current[k]):
            loosens = True
    if not new.get("require_stop_loss", True) and current.get("require_stop_loss", True):
        loosens = True
    if not loosens:
        return "tighten"
    return "undo_lineage" if _sig(new) in st["seen"] else "loosen_new"


def propose(new_params: dict, reason: str, analysis_ref: str = "") -> dict:
    """Apply if auto-allowed (tighten / undo-lineage); else stage for approval.
    Returns {applied, type, ...}. new_params is a full risk dict."""
    st = _load()
    kind = classify(new_params, st=st)
    if kind in ("tighten", "undo_lineage"):
        st["stack"].append({"params": new_params, "reason": reason, "type": kind, "ts": _now()})
        if _sig(new_params) not in st["seen"]:
            st["seen"].append(_sig(new_params))
        _save(st)
        return {"applied": True, "type": kind, "params": new_params}
    st["pending_approval"].append({"params": new_params, "reason": reason,
                                   "analysis_ref": analysis_ref, "ts": _now()})
    _save(st)
    return {"applied": False, "type": "loosen_new", "needs_approval": True,
            "params": new_params}


def undo(reason: str = "") -> dict:
    """Pop to the immediate parent. Always auto-allowed. No-op at the root."""
    st = _load()
    if len(st["stack"]) <= 1:
        return {"applied": False, "reason": "already at root (seed); nothing to undo"}
    popped = st["stack"].pop()
    _save(st)
    return {"applied": True, "undid": popped["params"], "now": st["stack"][-1]["params"],
            "reason": reason}


def apply_approved(new_params: dict, reason: str) -> dict:
    """Force-apply a human-APPROVED loosening (called after the user approves via
    the dashboard). Pushes the new state onto the stack and records it as held."""
    st = _load()
    st["stack"].append({"params": new_params, "reason": f"APPROVED: {reason}",
                        "type": "loosen_approved", "ts": _now()})
    if _sig(new_params) not in st["seen"]:
        st["seen"].append(_sig(new_params))
    _save(st)
    return {"applied": True, "type": "loosen_approved", "params": new_params}


def approve_pending(index: int = 0, reason: str = "user approved") -> dict:
    """Apply a staged loosening (only a human should call this path)."""
    st = _load()
    if index >= len(st["pending_approval"]):
        return {"applied": False, "reason": "no such pending proposal"}
    prop = st["pending_approval"].pop(index)
    new = prop["params"]
    st["stack"].append({"params": new, "reason": f"APPROVED: {prop['reason']}; {reason}",
                        "type": "loosen_approved", "ts": _now()})
    if _sig(new) not in st["seen"]:
        st["seen"].append(_sig(new))
    _save(st)
    return {"applied": True, "type": "loosen_approved", "params": new}
