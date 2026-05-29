"""Tiny TTL-based JSON file cache.

Used by every script that hits the network. Saves tokens (and rate-limit budget)
by avoiding redundant fetches across morning-brief steps.

Usage:
    from _cache import cache_get, cache_put, cached

    # Manual:
    data = cache_get("congress_recent", ttl_seconds=3600)
    if data is None:
        data = fetch_from_api()
        cache_put("congress_recent", data)

    # Decorator:
    @cached("regime_snapshot", ttl_seconds=900)
    def fetch_regime():
        ...

Cache layout:
    state/cache/<key>.json  → {"ts": ISO, "data": <jsonable>}

Keys are sanitized to filesystem-safe form. TTL is checked on read.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "state" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_TTL = 900  # 15 minutes


def _safe_key(key: str) -> str:
    """Sanitize a cache key to a filesystem-safe filename."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    if len(safe) > 100:
        h = hashlib.sha1(key.encode()).hexdigest()[:8]
        safe = safe[:80] + "_" + h
    return safe


def _path(key: str) -> Path:
    return CACHE_DIR / (_safe_key(key) + ".json")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> datetime:
    s = s.replace("Z", "+00:00") if s.endswith("Z") else s
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def cache_get(key: str, ttl_seconds: int = DEFAULT_TTL) -> Optional[Any]:
    """Return cached data if present and within TTL, else None."""
    p = _path(key)
    if not p.exists():
        return None
    try:
        with p.open() as f:
            payload = json.load(f)
        ts = _parse_iso(payload["ts"])
        age = (_now() - ts).total_seconds()
        if age > ttl_seconds:
            return None
        return payload["data"]
    except Exception:
        return None


def cache_put(key: str, data: Any) -> None:
    """Write data to cache with a current timestamp."""
    p = _path(key)
    payload = {"ts": _now().isoformat(timespec="seconds"), "data": data}
    tmp = p.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(payload, f, indent=2, default=str)
    os.replace(tmp, p)


def cache_age(key: str) -> Optional[float]:
    """Return age in seconds, or None if missing."""
    p = _path(key)
    if not p.exists():
        return None
    try:
        with p.open() as f:
            payload = json.load(f)
        return (_now() - _parse_iso(payload["ts"])).total_seconds()
    except Exception:
        return None


def cache_clear(key: Optional[str] = None) -> int:
    """Clear a single key or the whole cache. Returns number of files removed."""
    if key:
        p = _path(key)
        if p.exists():
            p.unlink()
            return 1
        return 0
    n = 0
    for f in CACHE_DIR.glob("*.json"):
        f.unlink()
        n += 1
    return n


def prune_older_than(max_age_days: float = 7) -> int:
    """Delete cache entries older than max_age_days. Returns number removed."""
    import time
    cutoff = time.time() - (max_age_days * 86400)
    n = 0
    for f in CACHE_DIR.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except Exception:
            continue
    return n


def cached(key: str, ttl_seconds: int = DEFAULT_TTL):
    """Decorator: cache function result by `key` for `ttl_seconds`.

    The key is static — if the function takes args that should partition the
    cache, build the key in the caller (e.g. f"news_{ticker}").
    """
    def deco(fn: Callable):
        @functools.wraps(fn)
        def wrap(*args, **kwargs):
            hit = cache_get(key, ttl_seconds=ttl_seconds)
            if hit is not None:
                return hit
            result = fn(*args, **kwargs)
            if result is not None:
                cache_put(key, result)
            return result
        return wrap
    return deco


# CLI: list/inspect/clear
def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Cache inspector")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    cl = sub.add_parser("clear")
    cl.add_argument("--key", default=None)
    pr = sub.add_parser("prune")
    pr.add_argument("--days", type=float, default=7)
    g = sub.add_parser("get")
    g.add_argument("--key", required=True)

    args = ap.parse_args()

    if args.cmd == "list":
        files = sorted(CACHE_DIR.glob("*.json"))
        if not files:
            print("(cache empty)")
            return 0
        print(f"{'KEY':<40}{'AGE':>10}{'SIZE':>10}")
        for f in files:
            try:
                payload = json.loads(f.read_text())
                ts = _parse_iso(payload["ts"])
                age = (_now() - ts).total_seconds()
                if age < 60:
                    age_s = f"{int(age)}s"
                elif age < 3600:
                    age_s = f"{int(age/60)}m"
                elif age < 86400:
                    age_s = f"{age/3600:.1f}h"
                else:
                    age_s = f"{age/86400:.1f}d"
            except Exception:
                age_s = "?"
            size = f.stat().st_size
            print(f"{f.stem:<40}{age_s:>10}{size:>10}")
        return 0

    if args.cmd == "clear":
        n = cache_clear(args.key)
        print(f"Cleared {n} entry/entries.")
        return 0

    if args.cmd == "prune":
        n = prune_older_than(args.days)
        print(f"Pruned {n} entry/entries older than {args.days}d.")
        return 0

    if args.cmd == "get":
        data = cache_get(args.key, ttl_seconds=10**9)
        age = cache_age(args.key)
        print(json.dumps({"age_seconds": age, "data": data}, indent=2, default=str))
        return 0

    return 1


if __name__ == "__main__":
    import sys
    sys.exit(_main())
