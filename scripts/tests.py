#!/usr/bin/env python3
"""Smoke tests.

Walk through every script's --help and a sane default invocation, verify exit
codes and JSON shape. Catches regressions on setup.sh re-run or refactors.

Usage:
  tests.py            # run all tests, print summary
  tests.py --strict   # exit non-zero if ANY test fails
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PY = SCRIPTS / ".venv" / "bin" / "python3"


def _run(cmd: list[str], timeout: int = 60, cwd: str | None = None) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"TIMEOUT after {timeout}s"
    except Exception as e:
        return 1, "", str(e)


# Each test: (name, cmd_args, expectations)
# Expectations: "exit_0" or callable taking (rc, stdout, stderr) -> (ok, msg)

def expect_exit_0(rc: int, out: str, err: str) -> tuple[bool, str]:
    return (rc == 0, f"exit={rc}")


def expect_json(rc: int, out: str, err: str) -> tuple[bool, str]:
    if rc != 0:
        return (False, f"exit={rc}")
    last = next((l for l in reversed(out.splitlines()) if l.strip().startswith("{")), None)
    if not last:
        return (False, "no JSON output")
    try:
        d = json.loads(last)
        if not isinstance(d, dict) or "step" not in d:
            return (False, "JSON has no 'step' field")
        return (True, f"step={d.get('step')} ok={d.get('ok')}")
    except Exception as e:
        return (False, f"JSON parse: {e}")


TESTS = [
    # Help / sanity
    ("help_portfolio", ["portfolio.py", "show"], expect_exit_0),
    ("help_mtm", ["mtm.py"], expect_exit_0),
    ("help_research", ["research.py", "freshness"], None),  # rc 0 or 1 both OK
    ("help_universe", ["_universe.py"], expect_exit_0),
    ("help_apikeys", ["_apikeys.py"], expect_exit_0),
    ("help_providers_status", ["-m", "_providers", "--status"], expect_exit_0),

    # JSON outputs
    ("json_regime", ["regime.py", "--json"], expect_json),
    ("json_sectors", ["sector_scan.py", "--json"], expect_json),
    ("json_sentiment", ["sentiment.py", "--json"], expect_json),
    ("json_breadth", ["breadth.py", "--json"], expect_json),
    ("json_macro", ["macro.py", "--json"], expect_json),
    ("json_position_review", ["position_review.py", "--json"], expect_json),
    ("json_watchlist_check", ["watchlist_check.py", "--json"], expect_json),
    ("json_mtm", ["mtm.py", "show", "--json"], expect_json),
    ("json_shadow_pnl", ["shadow.py", "pnl", "--json"], expect_json),
    ("json_crypto", ["crypto.py", "--json"], expect_json),
    ("json_movers_gainers", ["movers.py", "--gainers", "--json"], expect_json),
    ("json_news", ["news.py", "NVDA", "--hours", "48", "--json"], expect_json),

    # Risk + preflight (don't expect APPROVED — just exit-code-sane)
    ("risk_basic_check", ["risk.py", "--ticker", "XLE", "--entry", "57",
                          "--stop", "55", "--target", "60", "--size", "4",
                          "--kind", "etf", "--vehicle", "etf",
                          "--correlation", "energy_long",
                          "--horizon-days", "5"], None),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    results: list[tuple[str, bool, str]] = []
    for name, cmd_args, expectation in TESTS:
        # Build command
        cwd = None
        if cmd_args[0] == "-m":
            full = [str(PY)] + cmd_args
            cwd = str(SCRIPTS)  # so `-m _providers` finds the package
        else:
            full = [str(PY), str(SCRIPTS / cmd_args[0])] + list(cmd_args[1:])
        rc, out, err = _run(full, timeout=60, cwd=cwd)
        if expectation is None:
            ok = rc in (0, 1, 2)  # any well-defined rc passes "doesn't crash"
            msg = f"exit={rc}"
        else:
            ok, msg = expectation(rc, out, err)
        results.append((name, ok, msg))

    pad = max(len(n) for n, _, _ in results)
    print("=" * 60)
    for name, ok, msg in results:
        flag = "PASS" if ok else "FAIL"
        print(f"  [{flag}] {name:<{pad}}  {msg}")
    print("=" * 60)
    n_pass = sum(1 for _, ok, _ in results if ok)
    n_total = len(results)
    print(f"  {n_pass}/{n_total} passed")

    if args.strict and n_pass < n_total:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
