"""Shared helpers for the trader arsenal."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
PORTFOLIO_PATH = STATE_DIR / "portfolio.json"
PORTFOLIOS_DIR = STATE_DIR / "portfolios"
LEDGER_PATH = STATE_DIR / "ledger.jsonl"
SHADOW_LEDGER_PATH = STATE_DIR / "shadow_ledger.jsonl"
SHADOW_POSITIONS_PATH = STATE_DIR / "shadow_positions.json"
RESEARCH_LOG_PATH = STATE_DIR / "research_log.jsonl"


def portfolio_path(portfolio_id: str = "primary") -> Path:
    """Resolve a portfolio_id to its file path.

    "primary" maps to state/portfolio.json (the user's main book; many
    callers read it directly). Any other id maps to
    state/portfolios/<id>.json. This separation lets recipients of the
    hourly broadcast each have their own portfolio file used for filtering
    BUY ideas (skip names already held), without colliding with the primary
    book.
    """
    if portfolio_id == "primary":
        return PORTFOLIO_PATH
    return PORTFOLIOS_DIR / f"{portfolio_id}.json"


def ledger_path(book: str = "real") -> Path:
    return SHADOW_LEDGER_PATH if book == "shadow" else LEDGER_PATH


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def load_portfolio(portfolio_id: str = "primary") -> dict:
    """Load a portfolio by id. Default "primary" reads state/portfolio.json
    (existing behavior). Other ids read state/portfolios/<id>.json. Raises
    FileNotFoundError with a clear message when the file is missing."""
    path = portfolio_path(portfolio_id)
    if not path.exists():
        raise FileNotFoundError(
            f"Portfolio '{portfolio_id}' not found at {path}. "
            f"Add the file (v3 schema) before referencing this id."
        )
    with path.open() as f:
        return json.load(f)


def save_portfolio(p: dict, portfolio_id: str = "primary") -> None:
    path = portfolio_path(portfolio_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(p, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def append_ledger(entry: dict, book: str = "real") -> None:
    entry.setdefault("ts", now_iso())
    entry.setdefault("book", book)
    path = ledger_path(book)
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def read_ledger(book: str = "real", last_n: int | None = None) -> list[dict]:
    """Read the JSONL ledger. With last_n set, return only the final N entries
    via a tail-style seek-from-end (no full-file scan). Caller order is
    preserved (oldest of the tail first, newest last)."""
    path = ledger_path(book)
    if not path.exists():
        return []
    if last_n is None:
        out = []
        with path.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out
    return _tail_jsonl(path, last_n)


def _tail_jsonl(path, last_n: int) -> list[dict]:
    """Return the last `last_n` JSON records from a JSONL file by seeking from
    end. Reads in 8KB chunks until N+1 newlines are accumulated (the +1 covers
    the partial leading line that the chunk boundary may have split)."""
    if last_n <= 0:
        return []
    chunk_size = 8192
    data = b""
    newlines = 0
    with path.open("rb") as f:
        f.seek(0, 2)  # end
        pos = f.tell()
        while pos > 0 and newlines <= last_n:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            data = f.read(read_size) + data
            newlines = data.count(b"\n")
    text = data.decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    tail = lines[-last_n:]
    return [json.loads(ln) for ln in tail]


def load_shadow_positions() -> dict:
    with SHADOW_POSITIONS_PATH.open() as f:
        return json.load(f)


def save_shadow_positions(p: dict) -> None:
    tmp = SHADOW_POSITIONS_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(p, f, indent=2)
        f.write("\n")
    os.replace(tmp, SHADOW_POSITIONS_PATH)


def fmt_usd(x: float) -> str:
    return f"${x:,.2f}"
