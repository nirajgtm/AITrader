#!/usr/bin/env python3
"""Leadership tier — names where active edge lives.

Why this exists: the constitution's index-FOMO rule (rule 5) is a quality
filter on average across the broad universe, but the names that drive index
leadership runs are exactly the ones where the gate keeps blocking entries.
Backtest evidence (state/backtest/*.json):
  - Mega-cap PEAD: index-FOMO costs ~28pp over 12mo
  - 10-mega-cap breakouts: index-FOMO flips +30% to -20% (sample is small;
    broader S&P 500 breakout shows the gate is fine on average)

Trade we're making: keep the default rule for the long tail of the universe,
but for tickers in the leadership tier, allow strategies to ignore index-FOMO
because that's where index alpha actually comes from. If you're going to
"own SPY plus alpha," the alpha lives here.

Tier definition is intentionally simple and human-editable. Refresh as
leadership rotates. Track in a future enhancement: top-N by 60d RS rank,
sector ETF top-3 in 5d rotation. For now, hardcoded set + manual review.

Usage:
  leadership.py check NVDA       # exit 0 if leadership, 1 otherwise
  leadership.py list             # print current set
  leadership.py why NVDA         # print which criteria match
"""
from __future__ import annotations

import argparse
import sys

# Manual leadership tier as of 2026-05.
# Review on the first /trader invocation of each calendar month per the
# universe-review cadence (state/last_universe_review.txt).
#
# Inclusion criteria (any of):
#  - Top 7 mega-caps by mkt cap (the "magnificent 7" of the moment)
#  - Top 5 NDX names by 60d total return where market cap >= $200B
#  - Sector ETF currently ranked top 3 in 5d rotation (added manually)
#
# Last reviewed: 2026-05-02 by user.
LEADERSHIP_TIER: set[str] = {
    # Mag-7 mega-caps
    "NVDA", "MSFT", "AAPL", "AMZN", "GOOG", "GOOGL", "META", "TSLA",
    # Extended large-cap leaders (semis + AI infrastructure)
    "AMD", "AVGO", "MU", "MRVL", "ORCL",
    # Software / consumer leaders
    "NFLX", "CRM", "PLTR",
    # Sector ETFs in current top-3 5d rotation (per 2026-05-01 brief: XLE, XLV, XLF)
    "XLE", "XLV", "XLF",
}


def is_leadership_tier(ticker: str) -> bool:
    return (ticker or "").upper() in LEADERSHIP_TIER


def reasons(ticker: str) -> list[str]:
    """Return human-readable reasons why a ticker is or isn't in the tier."""
    t = (ticker or "").upper()
    if t in LEADERSHIP_TIER:
        if t in {"NVDA", "MSFT", "AAPL", "AMZN", "GOOG", "GOOGL", "META", "TSLA"}:
            return [f"{t}: mag-7 mega-cap"]
        if t in {"AMD", "AVGO", "MU", "MRVL", "ORCL"}:
            return [f"{t}: extended large-cap leader (semis / AI infra)"]
        if t in {"NFLX", "CRM", "PLTR"}:
            return [f"{t}: software / consumer leader"]
        if t in {"XLE", "XLV", "XLF"}:
            return [f"{t}: sector ETF in current top-3 5d rotation"]
        return [f"{t}: in LEADERSHIP_TIER set (uncategorized)"]
    return [f"{t}: not in LEADERSHIP_TIER. "
            "Add manually if 60d RS rank > top 5 NDX or sector top-3."]


def cmd_check(args: argparse.Namespace) -> int:
    return 0 if is_leadership_tier(args.ticker) else 1


def cmd_list(_args: argparse.Namespace) -> int:
    for t in sorted(LEADERSHIP_TIER):
        print(t)
    return 0


def cmd_why(args: argparse.Namespace) -> int:
    for line in reasons(args.ticker):
        print(line)
    return 0 if is_leadership_tier(args.ticker) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="exit 0 if ticker is leadership tier")
    c.add_argument("ticker")
    sub.add_parser("list", help="print all leadership-tier tickers")
    w = sub.add_parser("why", help="explain inclusion / exclusion")
    w.add_argument("ticker")
    args = ap.parse_args()
    return {"check": cmd_check, "list": cmd_list, "why": cmd_why}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
