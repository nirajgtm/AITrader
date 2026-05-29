"""API key loader.

Reads ~/claude-configs/trader/.env and exposes get_key(name) which returns the
value or None if absent. Every provider in scripts/_providers/ uses this.

Never echo a key back to stdout. Provider classes log "key=present/absent" only.

Usage:
    from _apikeys import get_key, require_key, has_key

    if has_key("FINNHUB_API_KEY"):
        ...

    key = require_key("FMP_API_KEY")  # raises if missing

    key = get_key("FRED_API_KEY", default=None)  # returns None if missing
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

_loaded = False


def _load() -> None:
    """Load .env once. Idempotent."""
    global _loaded
    if _loaded:
        return
    try:
        from dotenv import load_dotenv
        if ENV_PATH.exists():
            load_dotenv(ENV_PATH, override=False)
    except ImportError:
        # graceful fallback if python-dotenv not installed
        if ENV_PATH.exists():
            for line in ENV_PATH.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                os.environ.setdefault(k, v)
    _loaded = True


def get_key(name: str, default: Optional[str] = None) -> Optional[str]:
    _load()
    val = os.environ.get(name, default)
    if val == "":
        return default
    return val


def has_key(name: str) -> bool:
    return bool(get_key(name))


def require_key(name: str) -> str:
    val = get_key(name)
    if not val:
        raise RuntimeError(
            f"API key {name} required but not set. "
            f"Add it to {ENV_PATH} (see .env.example for the list)."
        )
    return val


def status() -> dict:
    """Return a {key_name: bool} map for diagnostic output (never the key itself)."""
    keys = [
        "ALPHAVANTAGE_API_KEY",
        "FINNHUB_API_KEY",
        "FMP_API_KEY",
        "MASSIVE_API_KEY",
        "FRED_API_KEY",
        "COINGECKO_DEMO_API_KEY",
        "QUIVER_API_KEY",
        "MARKETAUX_API_KEY",
        "TIINGO_API_KEY",
    ]
    return {k: has_key(k) for k in keys}


if __name__ == "__main__":
    print("API key status (presence only — never the value):")
    for k, present in status().items():
        mark = "✓" if present else "·"
        print(f"  {mark} {k}: {'set' if present else 'not set'}")
