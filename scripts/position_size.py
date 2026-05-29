#!/usr/bin/env python3
"""Compute position size from risk budget.

shares = floor( account * risk_pct / (entry - stop) )

Usage:
  position_size.py --account 1000 --risk-pct 2 --entry 50 --stop 47
"""
from __future__ import annotations

import argparse
import math
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=float, required=True)
    ap.add_argument("--risk-pct", type=float, required=True, help="e.g. 2 for 2%")
    ap.add_argument("--entry", type=float, required=True)
    ap.add_argument("--stop", type=float, required=True)
    ap.add_argument("--target", type=float, default=None, help="optional, for R:R")
    args = ap.parse_args()

    if args.stop == args.entry:
        print("stop == entry; invalid", file=sys.stderr)
        return 1

    per_share_risk = abs(args.entry - args.stop)
    side = "LONG" if args.entry > args.stop else "SHORT"
    risk_budget = args.account * args.risk_pct / 100
    shares = math.floor(risk_budget / per_share_risk)
    dollar_at_risk = shares * per_share_risk
    capital_used = shares * args.entry
    concentration = capital_used / args.account * 100

    print(f"=== Position sizing ({side}) ===")
    print(f"  Account:        ${args.account:,.2f}")
    print(f"  Risk budget:    ${risk_budget:,.2f}  ({args.risk_pct}%)")
    print(f"  Entry:          ${args.entry:.2f}")
    print(f"  Stop:           ${args.stop:.2f}")
    print(f"  Per-share risk: ${per_share_risk:.2f}")
    print(f"  -> Shares:      {shares}")
    print(f"  -> $ at risk:   ${dollar_at_risk:,.2f}")
    print(f"  -> Capital:     ${capital_used:,.2f}  ({concentration:.1f}% of account)")

    if args.target is not None:
        per_share_reward = abs(args.target - args.entry)
        rr = per_share_reward / per_share_risk
        dollar_reward = shares * per_share_reward
        print(f"  Target:         ${args.target:.2f}")
        print(f"  R:R:            {rr:.2f} : 1")
        print(f"  $ reward:       ${dollar_reward:,.2f}")
        if rr < 2:
            print("  [WARN] R:R < 2. CONSTITUTION requires >= 2:1.")

    if concentration > 25:
        print("  [WARN] Concentration > 25%. CONSTITUTION violation.")
    if shares == 0:
        print("  [WARN] Risk budget too tight for this stop distance. Widen account or stop.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
