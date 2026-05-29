#!/usr/bin/env python3
"""Cross-process rate limiter for a shared external API (public.com: 10 req/s).

The trader runs many scripts as separate subprocesses, but public.com's rate
limit is GLOBAL across the whole application, not per-process. A per-process
limiter would let several subprocesses collectively blow past the limit. This
gate uses a file lock plus a shared timestamp file so EVERY process and thread
paces against the same clock.

Usage:
    from _rate_limit import throttle
    throttle("publicdotcom", max_per_sec=10)   # blocks just long enough to comply
    ... then make the request ...

Mechanism: enforce a minimum spacing of 1/max_per_sec seconds between consecutive
request starts, serialized across processes via fcntl.flock on a per-key lock
file (which also stores the last-start timestamp). Conservative by design -- the
spacing guarantees the observed rate never exceeds max_per_sec, even under bursts.
The lock is held across the short sleep so concurrent callers queue in order.
"""
from __future__ import annotations

import fcntl
import time
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent.parent / "state"


def throttle(key: str, max_per_sec: float = 10.0) -> None:
    if max_per_sec <= 0:
        return
    interval = 1.0 / max_per_sec
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_DIR / f"ratelimit_{key}.lock"
    with open(lock_path, "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            lf.seek(0)
            raw = lf.read().strip()
            last_start = float(raw) if raw else 0.0
            now = time.time()
            wait = interval - (now - last_start)
            if wait > 0:
                time.sleep(wait)
                now = last_start + interval
            lf.seek(0)
            lf.truncate()
            lf.write(f"{now:.6f}")
            lf.flush()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
