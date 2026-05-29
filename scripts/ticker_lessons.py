#!/usr/bin/env python3
"""Read the last N reflection blocks for a ticker from state/ticker_history/.

Each ticker_history/<TKR>.md is append-only. Newest entries at top.
Each entry starts with `## YYYY-MM-DD` and continues until the next `## ` or EOF.

Used by position_review.py and watchlist_check.py to surface prior lessons
into the morning brief, so a name we've traded before carries forward what
we learned the last few times we touched it.

Usage:
    from ticker_lessons import load_lessons
    lessons = load_lessons("MU", n=3)   # list of {date, header, body}

CLI:
    ticker_lessons.py MU --n 3 [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = ROOT / "state" / "ticker_history"


def load_lessons(ticker: str, n: int = 3) -> list[dict]:
    path = HISTORY_DIR / f"{ticker.upper()}.md"
    if not path.exists():
        return []
    text = path.read_text()
    blocks: list[dict] = []
    current_header: str | None = None
    current_body: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current_header is not None:
                blocks.append(_finalize(current_header, current_body))
            current_header = line[3:].strip()
            current_body = []
        elif current_header is not None:
            current_body.append(line)
    if current_header is not None:
        blocks.append(_finalize(current_header, current_body))
    return blocks[:n]


def _finalize(header: str, body_lines: list[str]) -> dict:
    date = header.split(" ")[0] if header else ""
    body = "\n".join(body_lines).strip()
    return {"date": date, "header": header, "body": body}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    lessons = load_lessons(args.ticker, n=args.n)
    if args.json:
        print(json.dumps({"ticker": args.ticker.upper(), "lessons": lessons}, indent=2))
    else:
        if not lessons:
            print(f"No prior reflections for {args.ticker.upper()}.")
            return 0
        for l in lessons:
            print(f"## {l['header']}")
            print(l["body"])
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
