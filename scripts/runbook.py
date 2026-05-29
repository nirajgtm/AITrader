#!/usr/bin/env python3
"""Runbook — legacy orchestrator + preflight gate.

NOTE: As of Phase 1.6, `brief.py` is the canonical entry point for
morning/quick/status. It produces a compact JSON digest instead of dumping all
subscript stdout. Use `brief.py` from the SKILL.

This file is kept primarily for `runbook.py preflight` — the pre-trade
CONSTITUTION gate (cumulative cap, FOMO, earnings blackout, strategy-file
existence). morning/quick/status modes here are deprecated but kept as a
debug fallback if ever needed.

Modes:
  - `runbook.py preflight ...`  PRE-TRADE GATE (canonical, still used)
  - `runbook.py morning`        deprecated — use `brief.py morning`
  - `runbook.py quick`          deprecated — use `brief.py quick`
  - `runbook.py status`         deprecated — use `brief.py status`

Outputs:
  - Structured stdout (sectioned, scannable)
  - Auto-logs the research session to research_log.jsonl on completion
  - Writes per-step JSON snapshots to state/cache/runbook_<step>_<ts>.json
  - Returns non-zero exit code if any step had a hard failure
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PY = SCRIPTS / ".venv" / "bin" / "python3"
STATE = ROOT / "state"
KNOWLEDGE = ROOT / "knowledge"

sys.path.insert(0, str(SCRIPTS))
import watchlist_store  # noqa: E402  (watchlist is JSON, read via the shared store)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run(cmd: list[str], capture_stdout: bool = True, timeout: int = 90) -> tuple[int, str]:
    """Run a subprocess, return (exit_code, output)."""
    try:
        r = subprocess.run(cmd, capture_output=capture_stdout, text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode, out
    except subprocess.TimeoutExpired:
        return 124, f"[TIMEOUT after {timeout}s]"
    except Exception as e:
        return 1, f"[ERR] {e}"


def _section(title: str, body: str) -> None:
    bar = "=" * 78
    print(f"\n{bar}\n  {title}\n{bar}\n{body.rstrip()}")


# ---------- steps ----------

def step_state_load() -> dict:
    """Step 1: state load — portfolio, MTM, shadow."""
    out = {}
    rc1, p_show = _run([str(PY), str(SCRIPTS / "portfolio.py"), "show"])
    rc2, mtm = _run([str(PY), str(SCRIPTS / "mtm.py")])
    rc3, sh_list = _run([str(PY), str(SCRIPTS / "shadow.py"), "list", "--open-only"])
    rc4, sh_mtm = _run([str(PY), str(SCRIPTS / "shadow.py"), "mtm"])
    rc5, sh_pnl = _run([str(PY), str(SCRIPTS / "shadow.py"), "pnl"])
    body = "\n".join([p_show, "\n--- mtm ---\n", mtm, "\n--- shadow ---\n", sh_list, sh_mtm, sh_pnl])
    _section("1. STATE LOAD", body)
    out["ok"] = all(rc == 0 for rc in [rc1, rc2, rc3, rc4, rc5])
    return out


def step_regime() -> dict:
    rc, body = _run([str(PY), str(SCRIPTS / "regime.py")])
    _section("3. REGIME (top-down)", body)
    return {"ok": rc == 0}


def step_sentiment() -> dict:
    rc, body = _run([str(PY), str(SCRIPTS / "sentiment.py")])
    _section("3a. SENTIMENT / VOL STRUCTURE", body)
    return {"ok": rc == 0}


def step_breadth() -> dict:
    rc, body = _run([str(PY), str(SCRIPTS / "breadth.py")], timeout=240)
    _section("3b. BREADTH", body)
    return {"ok": rc == 0}


def step_sectors() -> dict:
    rc, body = _run([str(PY), str(SCRIPTS / "sector_scan.py")])
    _section("4. SECTOR ROTATION", body)
    return {"ok": rc == 0}


def step_earnings() -> dict:
    rc1, mega = _run([
        str(PY), str(SCRIPTS / "earnings.py"),
        "NVDA", "AAPL", "MSFT", "META", "AMZN", "GOOGL", "TSLA", "AMD", "AVGO", "NFLX",
    ])
    rc2, watch = _run([str(PY), str(SCRIPTS / "earnings.py"), "--from-watchlist"])
    body = mega + "\n\n--- watchlist earnings ---\n" + watch
    _section("5. EARNINGS CALENDAR (30d)", body)
    return {"ok": rc1 == 0}


def step_macro() -> dict:
    rc, body = _run([str(PY), str(SCRIPTS / "macro.py"), "--days", "14"])
    _section("9. MACRO / FED CALENDAR (14d)", body)
    return {"ok": rc == 0}


def step_flow() -> dict:
    rc1, majors = _run([str(PY), str(SCRIPTS / "flow_scan.py"), "--majors"])
    rc2, watch = _run([str(PY), str(SCRIPTS / "flow_scan.py"), "--from-watchlist"])
    body = majors + "\n\n--- watchlist flow ---\n" + watch
    _section("6. OPTIONS FLOW (unusual activity)", body)
    return {"ok": rc1 == 0}


def step_congress() -> dict:
    rc, body = _run([str(PY), str(SCRIPTS / "congress.py"), "--days", "7"])
    _section("7. CONGRESSIONAL TRADES (7d)", body)
    return {"ok": True}  # data gap is acceptable, not fatal


def step_insider() -> dict:
    rc, body = _run([str(PY), str(SCRIPTS / "insider.py"), "--days", "30"])
    _section("8. INSIDER FORM-4 CLUSTERS (30d)", body)
    return {"ok": True}


def step_movers() -> dict:
    rc, body = _run([str(PY), str(SCRIPTS / "movers.py")])
    _section("10. TOP MOVERS", body)
    return {"ok": rc == 0}


def step_news_watchlist() -> dict:
    rc, body = _run([str(PY), str(SCRIPTS / "news.py"), "--from-watchlist", "--hours", "48"])
    _section("3c. WATCHLIST NEWS (48h)", body)
    return {"ok": rc == 0}


def step_crypto() -> dict:
    rc, body = _run([str(PY), str(SCRIPTS / "crypto.py")])
    _section("11. CRYPTO", body)
    return {"ok": rc == 0}


def step_watchlist() -> dict:
    """Step 12: read the watchlist (state/watchlist.json) and price-scan each ticker."""
    tickers = watchlist_store.active_tickers()
    if not tickers:
        _section("12. WATCHLIST", "(no watchlist entries)")
        return {"ok": False}
    body_parts = []
    for t in tickers:
        rc, body = _run([str(PY), str(SCRIPTS / "price.py"), t, "--days", "5"])
        body_parts.append(body)
    body = "\n".join(body_parts) if body_parts else "(watchlist empty)"
    _section(f"12. WATCHLIST PRICE SCAN  ({len(tickers)} tickers)", body)
    return {"ok": True, "tickers": tickers}


def step_open_position_review() -> dict:
    """Open positions: run earnings + horizon expiry checks for each."""
    p = json.loads((STATE / "portfolio.json").read_text())
    body_parts = []
    today = datetime.now().date().isoformat()
    for pos in p.get("positions", []):
        t = pos["ticker"]
        body_parts.append(f"--- {t} ---")
        body_parts.append(f"opened={pos.get('opened_at')}  expires={pos.get('horizon_expires_at')}  "
                          f"strategy={pos.get('strategy')}")
        body_parts.append(f"invalidation: {pos.get('invalidation', '(none)')}")
        if pos.get("horizon_expires_at") and pos["horizon_expires_at"] <= today:
            body_parts.append(f"[!!] HORIZON EXPIRED {pos['horizon_expires_at']} <= today {today}. "
                              f"Mandatory review.")
        rc, px = _run([str(PY), str(SCRIPTS / "price.py"), t, "--days", "5"])
        body_parts.append(px)
    body = "\n".join(body_parts) if body_parts else "(no open positions)"
    _section("OPEN-POSITION REVIEW (price + horizon + invalidation)", body)
    return {"ok": True}


# ---------- runbook orchestrator ----------

def run_morning() -> int:
    """Full walk."""
    started = _now()
    results = {}

    # Step 0/1
    results["state"] = step_state_load()

    # Step 3+
    results["regime"] = step_regime()
    results["sentiment"] = step_sentiment()
    results["breadth"] = step_breadth()
    results["sectors"] = step_sectors()
    results["earnings"] = step_earnings()
    results["macro"] = step_macro()
    results["flow"] = step_flow()
    results["congress"] = step_congress()
    results["insider"] = step_insider()
    results["movers"] = step_movers()
    results["crypto"] = step_crypto()
    results["news_watchlist"] = step_news_watchlist()
    results["watchlist"] = step_watchlist()
    results["open_review"] = step_open_position_review()

    ended = _now()

    # Summary
    _section("RUNBOOK SUMMARY",
             "\n".join(f"  {k:<20} {'OK' if v.get('ok') else 'GAP'}" for k, v in results.items()))

    gaps = [k for k, v in results.items() if not v.get("ok")]
    print(f"\nStarted: {started}  Ended: {ended}  Gaps: {gaps or 'none'}")

    # Auto-log session (avoids the "forgot to log" failure mode)
    summary_one = f"runbook morning walked {len(results)} steps; gaps={','.join(gaps) if gaps else 'none'}"
    rc, _ = _run([
        str(PY), str(SCRIPTS / "research.py"), "log",
        "--start", started, "--end", ended, "--kind", "full",
        "--scripts", ",".join(results.keys()),
        "--summary", summary_one,
        "--gaps", ",".join(gaps),
    ])

    return 0 if not gaps else 0  # gaps don't auto-fail; surfaced in summary


def run_quick() -> int:
    """FRESH-QUICK path: regime, sector, flow, sentiment refresh."""
    started = _now()
    results = {
        "state": step_state_load(),
        "regime": step_regime(),
        "sentiment": step_sentiment(),
        "sectors": step_sectors(),
        "flow": step_flow(),
        "open_review": step_open_position_review(),
    }
    ended = _now()
    _section("QUICK SUMMARY",
             "\n".join(f"  {k:<20} {'OK' if v.get('ok') else 'GAP'}" for k, v in results.items()))
    rc, _ = _run([
        str(PY), str(SCRIPTS / "research.py"), "log",
        "--start", started, "--end", ended, "--kind", "quick",
        "--scripts", ",".join(results.keys()),
        "--summary", "runbook quick refresh (regime/sectors/flow/sentiment)",
    ])
    return 0


def run_status() -> int:
    """FRESH-FULL path: state-only review, surface cached research."""
    step_state_load()
    rc, freshness = _run([str(PY), str(SCRIPTS / "research.py"), "freshness"])
    rc, last = _run([str(PY), str(SCRIPTS / "research.py"), "last"])
    _section("RESEARCH FRESHNESS", freshness + "\n" + last)
    step_open_position_review()
    return 0


def _read_fomo_treatment(strategy_name: str, ticker: str | None = None) -> tuple[str | None, str]:
    """Read `## FOMO treatment` from a strategy file, with leadership-tier
    carve-out (v2.3, 2026-05-02).

    Returns (treatment, source_note). If the strategy file has a
    `## FOMO treatment when leadership` section AND the ticker is in
    `leadership.LEADERSHIP_TIER`, the leadership treatment overrides the default.

    Treatment values: hard_block | size_demote | allow. Returns (None, "")
    if no field is found.
    """
    if not strategy_name:
        return None, ""
    sf = KNOWLEDGE / "strategies" / f"{strategy_name}.md"
    if not sf.exists():
        return None, ""
    import re
    text = sf.read_text(errors="ignore")
    valid = {"hard_block", "size_demote", "allow"}

    leadership_treatment = None
    m_lead = re.search(r"^##\s*FOMO\s+treatment\s+when\s+leadership\s*\n([a-z_]+)",
                       text, re.M | re.I)
    if m_lead:
        cand = m_lead.group(1).strip().lower()
        if cand in valid:
            leadership_treatment = cand

    default_treatment = None
    m_def = re.search(r"^##\s*FOMO\s+treatment\s*\n([a-z_]+)", text, re.M | re.I)
    if m_def:
        cand = m_def.group(1).strip().lower()
        if cand in valid:
            default_treatment = cand

    if leadership_treatment and ticker:
        try:
            sys.path.insert(0, str(SCRIPTS))
            from leadership import is_leadership_tier
            if is_leadership_tier(ticker):
                return leadership_treatment, f"leadership carve-out ({ticker})"
        except Exception:
            pass

    return default_treatment, "default"


def run_preflight(args) -> int:
    """Run risk.py + strategy-existence + cumulative + earnings checks for a candidate."""
    started = _now()

    # Strategy file existence
    strategy_ok = True
    strategy_msg = ""
    fomo_treatment = None
    if args.strategy:
        sf = KNOWLEDGE / "strategies" / f"{args.strategy}.md"
        if not sf.exists() or sf.read_text(errors="ignore").strip().startswith("# Strategy: <NAME>"):
            strategy_ok = False
            strategy_msg = (f"Strategy file `knowledge/strategies/{args.strategy}.md` "
                            f"missing or unfilled. Write the playbook before trading this strategy.")
        else:
            strategy_msg = f"Strategy `{args.strategy}` file exists and has content."
            fomo_treatment, fomo_source = _read_fomo_treatment(args.strategy, args.ticker)
            if fomo_treatment:
                strategy_msg += f" FOMO treatment: {fomo_treatment} ({fomo_source})."

    # risk.py
    risk_cmd = [
        str(PY), str(SCRIPTS / "risk.py"),
        "--ticker", args.ticker,
        "--kind", args.kind,
        "--side", args.side,
        "--entry", str(args.entry),
        "--stop", str(args.stop),
        "--target", str(args.target),
        "--size", str(args.size),
    ]
    if args.vehicle:
        risk_cmd += ["--vehicle", args.vehicle]
    if args.premium is not None:
        risk_cmd += ["--premium", str(args.premium)]
    if args.ma20 is not None:
        risk_cmd += ["--ma20", str(args.ma20)]
    if args.atr is not None:
        risk_cmd += ["--atr", str(args.atr)]
    if args.underlying:
        risk_cmd += ["--underlying", args.underlying]
    if args.correlation:
        risk_cmd += ["--correlation", args.correlation]
    if args.horizon_days:
        risk_cmd += ["--horizon-days", str(args.horizon_days)]
    if args.skip_earnings_check:
        risk_cmd += ["--skip-earnings-check"]
    if fomo_treatment:
        risk_cmd += ["--fomo-treatment", fomo_treatment]
    if getattr(args, "mean_revert", False):
        risk_cmd += ["--mean-revert"]
    if getattr(args, "thesis", None):
        risk_cmd += ["--thesis", args.thesis]

    rc, body = _run(risk_cmd)
    _section("PRE-FLIGHT: RISK CHECK", body)
    _section("PRE-FLIGHT: STRATEGY", ("OK — " if strategy_ok else "FAIL — ") + strategy_msg)

    overall = (rc == 0) and strategy_ok
    print(f"\n=== PRE-FLIGHT VERDICT: {'APPROVED' if overall else 'REJECTED'} ===")
    return 0 if overall else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("morning")
    sub.add_parser("quick")
    sub.add_parser("status")

    pf = sub.add_parser("preflight")
    pf.add_argument("--ticker", required=True)
    pf.add_argument("--kind", default="stock", choices=["stock", "option", "crypto", "etf"])
    pf.add_argument("--side", default="LONG", choices=["LONG", "SHORT"])
    pf.add_argument("--vehicle", choices=["stock", "etf", "crypto", "long_call",
                                          "long_put", "debit_spread", "calendar",
                                          "covered_call", "csp"])
    pf.add_argument("--entry", type=float, required=True)
    pf.add_argument("--stop", type=float, required=True)
    pf.add_argument("--target", type=float, required=True)
    pf.add_argument("--size", type=float, required=True)
    pf.add_argument("--premium", type=float)
    pf.add_argument("--ma20", type=float)
    pf.add_argument("--atr", type=float)
    pf.add_argument("--underlying")
    pf.add_argument("--correlation", default="unknown")
    pf.add_argument("--horizon-days", type=int, default=10)
    pf.add_argument("--strategy")
    pf.add_argument("--skip-earnings-check", action="store_true")
    pf.add_argument("--mean-revert", action="store_true",
                    help="v2.2: skip FOMO if thesis cites RSI<30 or support bounce.")
    pf.add_argument("--thesis", default="",
                    help="Trade thesis text (used for --mean-revert keyword check).")

    args = ap.parse_args()
    if args.cmd == "morning":
        return run_morning()
    if args.cmd == "quick":
        return run_quick()
    if args.cmd == "status":
        return run_status()
    if args.cmd == "preflight":
        return run_preflight(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
