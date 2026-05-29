#!/usr/bin/env python3
"""watchlist_hygiene.py — assess each active watchlist entry's health.

Reads the active watchlist entries from ``state/watchlist.json`` (ticker / stop /
target / entry-trigger / added-date), then fetches current price via ``price.py``
and classifies each entry as:

  IN_ZONE       — entry zone present and last is inside it
  INVALIDATED   — stop breached (long: last < stop; short: last > stop)
  STALE         — age >= 14d AND last is >5% away from nearest trigger
  WAITING       — none of the above (still actionable, not yet triggered)
  INCOMPLETE    — stop or target missing (e.g. "tbd" entries)
  NO_DATA       — price fetch failed

Never auto-edits the watchlist. Emits ``propose_archive`` actions for INVALIDATED
and STALE entries; archival is human-approved and runs through a separate tool.

Usage:
  watchlist_hygiene.py [--json] [--watchlist PATH]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from _terse import emit, step_result

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PY = SCRIPTS / ".venv" / "bin" / "python3"
WATCHLIST_JSON = ROOT / "state" / "watchlist.json"


def _load_entries() -> list[dict]:
    if not WATCHLIST_JSON.exists():
        return []
    try:
        wl = json.loads(WATCHLIST_JSON.read_text())
        return wl.get("entries") or []
    except Exception:
        return []


NUM_RE = re.compile(r"\$?(\d+(?:\.\d+)?)")


def _first_number(text: str) -> float | None:
    if not text:
        return None
    m = NUM_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _all_numbers(text: str) -> list[float]:
    if not text:
        return []
    out: list[float] = []
    for m in NUM_RE.finditer(text):
        try:
            out.append(float(m.group(1)))
        except ValueError:
            continue
    return out


def _parse_entries(md_text: str) -> list[dict]:
    """Return one dict per active ticker. Skips status snapshot table at top."""
    lines = md_text.splitlines()
    in_active = False
    entries: list[dict] = []
    cur: dict | None = None

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            heading = stripped[3:].strip().lower()
            if heading.startswith("active"):
                in_active = True
                if cur is not None:
                    entries.append(cur)
                    cur = None
                continue
            else:
                if cur is not None:
                    entries.append(cur)
                    cur = None
                in_active = False
                continue

        if not in_active:
            continue

        m = HEADER_RE.match(line)
        if m:
            if cur is not None:
                entries.append(cur)
            cur = {
                "ticker": m.group(1),
                "added": None,
                "stop": None,
                "target": None,
                "trigger_text": None,
                "stop_text": None,
                "target_text": None,
            }
            continue

        if cur is None:
            continue

        am = ADDED_RE.match(line)
        if am:
            cur["added"] = am.group(1)
            continue
        sm = STOP_RE.match(line)
        if sm:
            cur["stop_text"] = sm.group(1).strip()
            cur["stop"] = _first_number(sm.group(1))
            continue
        tm = TARGET_RE.match(line)
        if tm:
            cur["target_text"] = tm.group(1).strip()
            target_nums = _all_numbers(tm.group(1))
            if target_nums:
                # For a "Target: $267-$270" range, take the lower bound (conservative R:R)
                cur["target"] = min(target_nums[:2]) if len(target_nums) >= 2 else target_nums[0]
            continue
        trm = TRIGGER_RE.match(line)
        if trm:
            cur["trigger_text"] = trm.group(1).strip()
            continue

    if cur is not None:
        entries.append(cur)
    return entries


def _trigger_zone(trigger_text: str | None) -> tuple[float, float] | None:
    """Heuristic: extract the entry zone from the Entry trigger: prose."""
    if not trigger_text:
        return None
    nums = _all_numbers(trigger_text)
    if not nums:
        return None
    if len(nums) >= 2:
        return (min(nums[0], nums[1]), max(nums[0], nums[1]))
    n = nums[0]
    return (n * 0.99, n * 1.01)


def _fetch_price(ticker: str) -> dict | None:
    cmd = [str(PY), str(SCRIPTS / "price.py"), ticker, "--json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    out = (r.stdout or "").strip()
    last_line = next((ln for ln in reversed(out.splitlines()) if ln.startswith("{")), None)
    if not last_line:
        return None
    try:
        d = json.loads(last_line)
    except Exception:
        return None
    if not d.get("ok"):
        return None
    return d.get("data") or {}


def _classify(entry: dict, last: float | None, today: date) -> tuple[str, str, str]:
    """Return (status, action, reason).

    action ∈ {"keep", "propose_archive"}.
    """
    stop = entry.get("stop")
    target = entry.get("target")

    if stop is None or target is None:
        return ("INCOMPLETE", "keep", "stop or target not yet set (likely 'tbd' fields)")

    direction = "LONG" if target > stop else "SHORT"

    if last is None:
        return ("NO_DATA", "keep", "price fetch failed")

    # Invalidation
    if direction == "LONG" and last < stop:
        return ("INVALIDATED", "propose_archive",
                f"LONG thesis: last {last:.2f} < stop {stop:.2f}")
    if direction == "SHORT" and last > stop:
        return ("INVALIDATED", "propose_archive",
                f"SHORT thesis: last {last:.2f} > stop {stop:.2f}")

    # In-zone
    zone = _trigger_zone(entry.get("trigger_text"))
    if zone and zone[0] <= last <= zone[1]:
        return ("IN_ZONE", "keep", f"price {last:.2f} inside trigger zone {zone[0]:.2f}-{zone[1]:.2f}")

    # Stale
    age_days = None
    if entry.get("added"):
        try:
            added = date.fromisoformat(entry["added"])
            age_days = (today - added).days
        except Exception:
            pass

    if age_days is not None and age_days >= 14:
        # Distance from trigger zone (or trigger midpoint if no zone)
        if zone:
            mid = (zone[0] + zone[1]) / 2
        else:
            mid = (stop + target) / 2  # fallback
        if mid > 0:
            dist_pct = abs(last - mid) / mid * 100
            if dist_pct > 5:
                return ("STALE", "propose_archive",
                        f"age {age_days}d, last {last:.2f} is {dist_pct:.1f}% from trigger {mid:.2f}")

    return ("WAITING", "keep", "still actionable, not yet triggered")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not WATCHLIST_JSON.exists():
        msg = f"state/watchlist.json not found at {WATCHLIST_JSON}"
        if args.json:
            emit(step_result("watchlist_hygiene", ok=False, headline=msg, errors=[msg]))
        else:
            print(msg, file=sys.stderr)
        return 1

    # Convert JSON entries to the shape _classify() expects
    raw_entries = _load_entries()
    entries = []
    for e in raw_entries:
        levels = e.get("levels") or {}
        trigger_text = None
        elo = levels.get("entry_zone_lo")
        ehi = levels.get("entry_zone_hi")
        etrig = levels.get("entry_trigger")
        if elo and ehi:
            trigger_text = f"${elo}-${ehi}"
        elif etrig:
            trigger_text = f"${etrig}"
        entries.append({
            "ticker": e.get("ticker", ""),
            "added": e.get("added"),
            "stop": levels.get("stop"),
            "target": levels.get("target"),
            "trigger_text": trigger_text,
            "stop_text": str(levels.get("stop", "")),
            "target_text": str(levels.get("target", "")),
        })

    today = date.today()

    items: list[dict] = []
    actions: list[dict] = []
    counts: dict[str, int] = {}

    for e in entries:
        last_d = _fetch_price(e["ticker"])
        last = last_d.get("close") if last_d else None
        status, action, reason = _classify(e, last, today)
        counts[status] = counts.get(status, 0) + 1
        age_days = None
        if e.get("added"):
            try:
                age_days = (today - date.fromisoformat(e["added"])).days
            except Exception:
                pass
        items.append({
            "ticker": e["ticker"],
            "status": status,
            "age_days": age_days,
            "last": last,
            "stop": e.get("stop"),
            "target": e.get("target"),
            "action": action,
            "reason": reason,
        })
        if action == "propose_archive":
            actions.append({
                "kind": "watchlist_action",
                "ticker": e["ticker"],
                "action": "propose_archive",
                "reason": reason,
                "status": status,
            })

    flags: list[str] = []
    if counts.get("INVALIDATED", 0):
        flags.append(f"watchlist_invalidated_{counts['INVALIDATED']}")
    if counts.get("STALE", 0):
        flags.append(f"watchlist_stale_{counts['STALE']}")
    if counts.get("IN_ZONE", 0):
        flags.append(f"watchlist_in_zone_{counts['IN_ZONE']}")

    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    headline = f"{len(items)} active; " + " ".join(parts) if items else "no active entries"

    if args.json:
        emit(step_result("watchlist_hygiene", ok=True, headline=headline,
                         data={"items": items, "counts": counts},
                         flags=flags, actions=actions))
        return 0

    print(f"=== Watchlist hygiene ({today.isoformat()}) ===")
    print(headline)
    for it in items:
        last = f"{it['last']:.2f}" if it['last'] is not None else "n/a"
        age = f"{it['age_days']}d" if it['age_days'] is not None else "?"
        print(f"  {it['ticker']:<6} {it['status']:<12} age={age:<5} last={last:<8}  "
              f"action={it['action']}  ({it['reason']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
