"""Per-agent controls: enable/disable plus cadence for the mind's subagents.

Owner intent lives in state/mind/agent_controls.json (written ONLY by the dashboard).
Runtime tracking lives in state/mind/agent_runtime.json (written ONLY by the mind).
Marketplace install requests live in state/mind/install_queue.json (written by the
dashboard, drained by the mind). Installed marketplace plugins are listed in
state/mind/installed_plugins.json; they JOIN the agent universe (status_all / due_set)
so the gate allows them and the dashboard renders them as tiles. All are gitignored.

Tiers and defaults are static and live here in AGENT_TIERS:
  - "recommended" agents feed the buy / sell / evaluate decision (the debate panel,
    Analyst, News, CaseBuilder, DomainExpert) plus the Editor (infrastructure). They are
    event-triggered, carry no cadence, and the dashboard shows a "recommended to keep
    enabled" pill. They can still be disabled; if one is off, the mind decides without it.
  - "cadence" agents run on a clock (every_run or every N hours): Janitor,
    ConvictionWriter, ShadowTrader, Ideas, Coach, MarketResearch, self-audit.
  - "plugin" agents are installed from the marketplace; they default to enabled, every_run,
    and an editable cadence.

The hard rule the mind obeys: a disabled agent is invisible. It is never convened, even
if a step or prompt asks for it. is_due() returning False means do not convene.

Default when an agent has no owner override: enabled, with the default_cadence below. So
an absent or empty agent_controls.json leaves every agent enabled (nothing silently off).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_STATE = _DIR / "state" / "mind"
CONTROLS_PATH = _STATE / "agent_controls.json"
RUNTIME_PATH = _STATE / "agent_runtime.json"
INSTALL_QUEUE_PATH = _STATE / "install_queue.json"
INSTALLED_PATH = _STATE / "installed_plugins.json"
REGISTRY_PATH = _DIR / "subagents" / "registry.json"

# Static per-agent tier and default cadence. Keys match the registry names (snake_case).
AGENT_TIERS: dict[str, dict] = {
    # Recommended-on, event-triggered (no cadence): feed the decision, or core infra.
    "editor":        {"tier": "recommended", "recommended": True},
    "strategist":    {"tier": "recommended", "recommended": True},
    "historian":     {"tier": "recommended", "recommended": True},
    "risk_officer":  {"tier": "recommended", "recommended": True},
    "skeptic":       {"tier": "recommended", "recommended": True},
    "opportunist":   {"tier": "recommended", "recommended": True},
    "behaviorist":   {"tier": "recommended", "recommended": True},
    "analyst":       {"tier": "recommended", "recommended": True},
    "news":          {"tier": "recommended", "recommended": True},
    "case_builder":  {"tier": "recommended", "recommended": True},
    "domain_expert": {"tier": "recommended", "recommended": True},
    # Cadence-controlled: run on a clock, not tied to a single decision.
    "janitor":           {"tier": "cadence", "recommended": False, "default_cadence": {"type": "every_run"}},
    "conviction_writer": {"tier": "cadence", "recommended": False, "default_cadence": {"type": "every_run"}, "tab": "conviction"},
    "shadow_trader":     {"tier": "cadence", "recommended": False, "default_cadence": {"type": "every_run"}, "tab": "shadow"},    "coach":             {"tier": "cadence", "recommended": False, "default_cadence": {"type": "interval", "hours": 24}},
    "market_research":   {"tier": "cadence", "recommended": False, "default_cadence": {"type": "interval", "hours": 24}},
    "self_audit":        {"tier": "cadence", "recommended": False, "default_cadence": {"type": "interval", "hours": 24}},
}

# Metadata for an installed marketplace plugin (not in the static roster above).
_PLUGIN_META = {"tier": "plugin", "recommended": False, "default_cadence": {"type": "every_run"}}


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _controls() -> dict:
    """Owner overrides: {"agents": {key: {"enabled": bool, "cadence": {...}}}}."""
    return _load(CONTROLS_PATH).get("agents") or {}


def _runtime() -> dict:
    """Mind runtime: {"agents": {key: {"last_convened", "run_count", "last_outcome"}}}."""
    return _load(RUNTIME_PATH).get("agents") or {}


def _installed_plugins() -> list[str]:
    """Marketplace plugins recorded as installed. They join the agent universe so the gate
    allows them and the dashboard shows them as tiles, from the moment the owner installs."""
    return _load(INSTALLED_PATH).get("plugins") or []


def _meta_for(key: str) -> dict:
    """Static metadata for a core agent, or default plugin metadata for an installed one."""
    return AGENT_TIERS.get(key) or _PLUGIN_META


def _to_key(name: str) -> str:
    """Normalize a registry display name to a snake_case agent key.
    "Risk Officer" -> "risk_officer", "ConvictionWriter" -> "conviction_writer"."""
    import re
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", (name or "").strip())
    return re.sub(r"[\s\-]+", "_", s).lower()


def _registry_descriptions() -> dict[str, dict]:
    """Map agent key -> {"role", "invoke_when"} sourced from subagents/registry.json, across
    every_run / debate_panel / on_demand. The registry keys agents by display name; we
    normalize those to the snake_case keys used here. Defensive: a missing/bad registry
    yields an empty map, so role/invoke_when fall back to "" everywhere."""
    out: dict[str, dict] = {}
    reg = _load(REGISTRY_PATH)
    for section in ("every_run", "debate_panel", "on_demand"):
        for a in reg.get(section) or []:
            if not isinstance(a, dict):
                continue
            key = _to_key(a.get("name") or "")
            if not key:
                continue
            out[key] = {"role": a.get("role", "") or "",
                        "invoke_when": a.get("invoke_when") or a.get("when") or ""}
    return out


def _all_keys() -> list[str]:
    """The full agent universe: static core roster plus installed marketplace plugins."""
    keys = list(AGENT_TIERS)
    keys += [p for p in _installed_plugins() if p not in AGENT_TIERS]
    return keys


def is_enabled(key: str) -> bool:
    """Owner override wins; default is enabled, so nothing is silently off."""
    return bool((_controls().get(key) or {}).get("enabled", True))


def cadence_for(key: str) -> dict | None:
    """Owner cadence override, else the static/plugin default. None = event-triggered."""
    ov = _controls().get(key) or {}
    if ov.get("cadence"):
        return ov["cadence"]
    return _meta_for(key).get("default_cadence")


def last_convened(key: str) -> str | None:
    return (_runtime().get(key) or {}).get("last_convened")


def _hours_since(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds() / 3600.0
    except ValueError:
        return None


def is_due(key: str, now: datetime | None = None) -> bool:
    """The gate. False means do NOT convene. A disabled agent is never due. An
    event-triggered agent (no cadence) is due whenever enabled; its calling step decides
    the rest. every_run is always due. interval is due once N hours have elapsed since
    last_convened, or if it has never run."""
    if not is_enabled(key):
        return False
    cad = cadence_for(key)
    if not cad:
        return True
    if cad.get("type") == "every_run":
        return True
    if cad.get("type") == "interval":
        now = now or datetime.now(timezone.utc).astimezone()
        h = _hours_since(last_convened(key), now)
        return h is None or h >= float(cad.get("hours", 24))
    return True


def mark_convened(key: str, outcome: str = "") -> None:
    """Stamp at convene time. The mind calls this when it dispatches the agent."""
    data = _load(RUNTIME_PATH)
    rec = data.setdefault("agents", {}).setdefault(key, {})
    rec["last_convened"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    rec["run_count"] = int(rec.get("run_count", 0)) + 1
    if outcome:
        rec["last_outcome"] = outcome
    _atomic_write(RUNTIME_PATH, data)


def mark_cadence_run() -> list[str]:
    """Auto-stamp the deterministic every-run CORE agents (Janitor, ConvictionWriter,
    ShadowTrader) at the orientation of a substantive run, so their last_convened is never
    blank just because the mind skipped the manual stamp. Honors owner overrides: a disabled
    agent, or one the owner re-cadenced off every_run, is skipped. Plugins and on-demand/event
    agents are NOT touched here -- the mind stamps those itself when it convenes them. Returns
    the keys stamped."""
    stamped = []
    for key in AGENT_TIERS:
        if (cadence_for(key) or {}).get("type") == "every_run" and is_enabled(key):
            mark_convened(key, "auto: substantive run")
            stamped.append(key)
    return stamped


def status_all(now: datetime | None = None) -> list[dict]:
    """Per-agent control view, for the dashboard and the run context. Carries each agent's
    role + invoke_when (from subagents/registry.json) so the mind can see what an agent does
    and when it fires; installed marketplace plugins not in the registry carry "" for both."""
    now = now or datetime.now(timezone.utc).astimezone()
    desc = _registry_descriptions()
    out = []
    for key in _all_keys():
        meta = _meta_for(key)
        d = desc.get(key) or {}
        out.append({
            "key": key,
            "tier": meta.get("tier"),
            "recommended": bool(meta.get("recommended")),
            "enabled": is_enabled(key),
            "cadence": cadence_for(key),
            "gates": meta.get("gates", []),
            "tab": meta.get("tab"),
            "role": d.get("role", ""),
            "invoke_when": d.get("invoke_when", ""),
            "last_convened": last_convened(key),
            "due": is_due(key, now),
        })
    return out


def due_set(now: datetime | None = None) -> list[str]:
    """The keys the mind may convene this run. Disabled and not-yet-due ones are absent."""
    now = now or datetime.now(timezone.utc).astimezone()
    return [k for k in _all_keys() if is_due(k, now)]


# --- marketplace: install queue (dashboard writes, mind drains) + installed list ---

def install_queue() -> list[dict]:
    return _load(INSTALL_QUEUE_PATH).get("queue") or []


def queue_install(plugin: str, marketplace: str = "aitrader-plugins") -> None:
    data = _load(INSTALL_QUEUE_PATH)
    q = data.setdefault("queue", [])
    if not any(it.get("plugin") == plugin for it in q):
        q.append({"plugin": plugin, "marketplace": marketplace,
                  "requested": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")})
        _atomic_write(INSTALL_QUEUE_PATH, data)


def clear_install(plugin: str) -> None:
    data = _load(INSTALL_QUEUE_PATH)
    data["queue"] = [it for it in (data.get("queue") or []) if it.get("plugin") != plugin]
    _atomic_write(INSTALL_QUEUE_PATH, data)


def record_installed(plugin: str) -> None:
    """Add a plugin to the installed list. The dashboard calls this the moment the owner
    clicks Install (a virtual install), so the plugin is immediately a controllable tile and
    is allowed by the gate; the real `claude plugin install` happens on the next run."""
    data = _load(INSTALLED_PATH)
    plugins = data.setdefault("plugins", [])
    if plugin not in plugins:
        plugins.append(plugin)
        _atomic_write(INSTALLED_PATH, data)


def remove_installed(plugin: str) -> None:
    data = _load(INSTALLED_PATH)
    data["plugins"] = [p for p in (data.get("plugins") or []) if p != plugin]
    _atomic_write(INSTALLED_PATH, data)


def set_control(key: str, *, enabled: bool | None = None,
                cadence: dict | None = None, clear_cadence: bool = False) -> None:
    """Write an owner override. Called by the dashboard, the only writer of agent_controls.json.
    enabled None leaves it as-is; clear_cadence drops any cadence override; otherwise a given
    cadence is stored."""
    data = _load(CONTROLS_PATH)
    rec = data.setdefault("agents", {}).setdefault(key, {})
    if enabled is not None:
        rec["enabled"] = bool(enabled)
    if clear_cadence:
        rec.pop("cadence", None)
    elif cadence is not None:
        rec["cadence"] = cadence
    _atomic_write(CONTROLS_PATH, data)


def main() -> None:
    import argparse
    import json as _json
    ap = argparse.ArgumentParser(description="Per-agent controls: due-set, status, convene stamping, install queue.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("due", help="print the agent keys the mind may convene this run (JSON list)")
    sub.add_parser("status", help="print per-agent control status (JSON)")
    c = sub.add_parser("convened", help="stamp an agent as convened this run")
    c.add_argument("key")
    c.add_argument("outcome", nargs="?", default="")
    q = sub.add_parser("queue-install", help="queue a marketplace plugin for install next run")
    q.add_argument("plugin")
    sub.add_parser("install-queue", help="print the pending install queue (JSON)")
    cl = sub.add_parser("clear-install", help="drop a plugin from the install queue")
    cl.add_argument("plugin")
    ri = sub.add_parser("record-installed", help="mark a plugin as installed (joins the agent universe)")
    ri.add_argument("plugin")
    sub.add_parser("installed", help="print the installed plugin list (JSON)")
    args = ap.parse_args()
    if args.cmd == "due":
        print(_json.dumps(due_set()))
    elif args.cmd == "status":
        print(_json.dumps(status_all(), indent=2))
    elif args.cmd == "convened":
        mark_convened(args.key, args.outcome)
        print(f"stamped {args.key}")
    elif args.cmd == "queue-install":
        queue_install(args.plugin)
        print(f"queued {args.plugin}")
    elif args.cmd == "install-queue":
        print(_json.dumps(install_queue(), indent=2))
    elif args.cmd == "clear-install":
        clear_install(args.plugin)
        print(f"cleared {args.plugin}")
    elif args.cmd == "record-installed":
        record_installed(args.plugin)
        print(f"recorded {args.plugin}")
    elif args.cmd == "installed":
        print(_json.dumps(_installed_plugins()))


if __name__ == "__main__":
    main()
