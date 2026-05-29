#!/usr/bin/env python3
"""Daily-log monthly archive.

At first invocation each month, move prior-month daily logs to
state/daily_log/archive/YYYY-MM/. Keeps the active dir flat and quick to grep.

Idempotent: safe to call every brief run.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "state" / "daily_log"
ARCHIVE_BASE = LOG_DIR / "archive"


def archive_prior_months() -> int:
    """Move logs older than current month into archive/YYYY-MM/. Returns count moved."""
    if not LOG_DIR.exists():
        return 0
    today = date.today()
    cur_ym = today.strftime("%Y-%m")

    pattern = re.compile(r"(\d{4}-\d{2})-\d{2}_day\d+_.*\.md$")
    moved = 0
    for f in LOG_DIR.glob("*.md"):
        m = pattern.match(f.name)
        if not m:
            continue
        log_ym = m.group(1)
        if log_ym >= cur_ym:
            continue
        dest_dir = ARCHIVE_BASE / log_ym
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f.name
        if not dest.exists():
            shutil.move(str(f), str(dest))
            moved += 1
    return moved


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.dry_run:
        # Just count
        if not LOG_DIR.exists():
            print("No log dir.")
            return 0
        today = date.today()
        cur_ym = today.strftime("%Y-%m")
        pattern = re.compile(r"(\d{4}-\d{2})-\d{2}_day\d+_.*\.md$")
        candidates = [f for f in LOG_DIR.glob("*.md")
                      if pattern.match(f.name) and pattern.match(f.name).group(1) < cur_ym]
        print(f"Would archive {len(candidates)} log(s): {[f.name for f in candidates]}")
        return 0
    n = archive_prior_months()
    print(f"Archived {n} log(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
