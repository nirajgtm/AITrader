#!/usr/bin/env python3
"""Deterministic client for the public.com brokerage API (DATA only).

This is the trader system's primary source of truth for quotes and market data,
because public.com serves REAL-TIME exchange quotes (vs yfinance's ~15-min delay).
It is intentionally limited to READ-ONLY data here -- no order placement lives in
this module (trading is handled separately and deliberately).

Auth: PUBLIC_BROKER_API in .env is a long-lived personal SECRET, not a token. We
exchange it for a short-lived Bearer JWT and cache that (with its expiry) in
state/public_token.json, re-minting before it expires. Nearly every path is
scoped by the brokerage account id (PUBLIC_COM_ACCOUNT_ID in .env).

Design for determinism:
  - Explicit timeouts on every request.
  - Bounded retry with backoff on 429 / 5xx; everything else fails fast.
  - Clear PublicAPIError on failure so callers can fall back (e.g. to yfinance).
  - No hidden global state beyond the on-disk token cache.

Endpoints (from the official Postman collection + docs):
  POST {AUTH}/personal/access-tokens                      -> mint JWT
  POST {GW}/marketdata/{accountId}/quotes                 -> real-time quotes (batch)
  GET  {GW}/historicdata/{type}/{symbol}/{period}/{aggregation}  -> OHLCV bars

CLI (for testing):
  publicdotcom_api.py quote AAPL [MSFT ...]
  publicdotcom_api.py history AAPL --period YEAR --aggregation ONE_DAY
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
TOKEN_CACHE = ROOT / "state" / "public_token.json"

AUTH_BASE = "https://api.public.com/userapiauthservice"
GATEWAY_BASE = "https://api.public.com/userapigateway"

TOKEN_VALIDITY_MIN = 120          # mint 2h tokens (re-mint before expiry)
TOKEN_RENEW_SLACK_SEC = 120       # re-mint when <2 min of life remains
HTTP_TIMEOUT = 15                 # seconds per request
MAX_RETRIES = 3                   # retries on 429/5xx
BACKOFF_BASE = 1.5                # seconds; exponential
RATE_LIMIT_PER_SEC = 10           # public.com global cap (shared across all processes)

# Cross-process rate limiter. public.com's 10 req/s ceiling is global across the
# whole app, but the trader runs many scripts as separate subprocesses, so we
# pace against a shared file-locked clock. If the limiter can't be imported we
# fall back to a no-op (warned) rather than crashing.
try:
    from _rate_limit import throttle as _throttle
except Exception:  # pragma: no cover
    def _throttle(key: str, max_per_sec: float = 10.0) -> None:
        pass


class PublicAPIError(RuntimeError):
    """Raised when the public.com API cannot satisfy a request. Callers that
    need resilience should catch this and fall back to another data source."""


# --------------------------------------------------------------------------
# Credentials (read straight from .env; never logged)
# --------------------------------------------------------------------------

def _env_value(key: str) -> str | None:
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip() or None
    return None


def _secret() -> str:
    s = _env_value("PUBLIC_BROKER_API")
    if not s:
        raise PublicAPIError("PUBLIC_BROKER_API not set in .env")
    return s


def _account_id() -> str:
    a = _env_value("PUBLIC_COM_ACCOUNT_ID")
    if not a:
        raise PublicAPIError("PUBLIC_COM_ACCOUNT_ID not set in .env")
    return a


# --------------------------------------------------------------------------
# Low-level HTTP with timeout + bounded backoff
# --------------------------------------------------------------------------

def _http(method: str, url: str, *, headers: dict, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        _throttle("publicdotcom", RATE_LIMIT_PER_SEC)  # global <=10 req/s across all processes
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            code = e.code
            if code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE * (2 ** attempt))
                last_err = e
                continue
            detail = ""
            try:
                detail = e.read().decode()[:300]
            except Exception:
                pass
            raise PublicAPIError(f"{method} {url} -> HTTP {code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE * (2 ** attempt))
                last_err = e
                continue
            raise PublicAPIError(f"{method} {url} -> network error: {e}") from e
    raise PublicAPIError(f"{method} {url} -> exhausted retries: {last_err}")


# --------------------------------------------------------------------------
# Token management (mint + cache JWT)
# --------------------------------------------------------------------------

def _load_cached_token() -> str | None:
    if not TOKEN_CACHE.exists():
        return None
    try:
        d = json.loads(TOKEN_CACHE.read_text())
        if d.get("accessToken") and d.get("expires_at", 0) - time.time() > TOKEN_RENEW_SLACK_SEC:
            return d["accessToken"]
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _mint_token() -> str:
    url = f"{AUTH_BASE}/personal/access-tokens"
    resp = _http("POST", url,
                 headers={"Content-Type": "application/json"},
                 body={"validityInMinutes": TOKEN_VALIDITY_MIN, "secret": _secret()})
    token = resp.get("accessToken")
    if not token:
        raise PublicAPIError(f"token mint returned no accessToken: keys={list(resp.keys())}")
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(json.dumps({
        "accessToken": token,
        "expires_at": time.time() + TOKEN_VALIDITY_MIN * 60,
        "minted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }))
    return token


def _token() -> str:
    return _load_cached_token() or _mint_token()


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


# --------------------------------------------------------------------------
# Public data methods
# --------------------------------------------------------------------------

def get_quotes(symbols: list[str], instrument_type: str = "EQUITY") -> dict[str, dict]:
    """Real-time quotes for a batch of symbols. Returns {SYMBOL: quote_dict}.
    quote_dict carries last, bid, ask, volume, previousClose, oneDayChange, etc."""
    syms = [s.upper() for s in symbols if s]
    if not syms:
        return {}
    url = f"{GATEWAY_BASE}/marketdata/{_account_id()}/quotes"
    body = {"instruments": [{"symbol": s, "type": instrument_type} for s in syms]}
    resp = _http("POST", url, headers=_auth_headers(), body=body)
    # Response shape: a list of quote objects (key name confirmed empirically by
    # the CLI). Normalize to {symbol: quote}.
    quotes = resp.get("quotes") if isinstance(resp, dict) else None
    if quotes is None and isinstance(resp, list):
        quotes = resp
    out: dict[str, dict] = {}
    for q in quotes or []:
        sym = (q.get("symbol") or (q.get("instrument") or {}).get("symbol") or "").upper()
        if sym:
            out[sym] = q
    return out


def get_quote(symbol: str, instrument_type: str = "EQUITY") -> dict | None:
    return get_quotes([symbol], instrument_type).get(symbol.upper())


def get_accounts() -> dict:
    """List the trading accounts on the profile (read-only)."""
    return _http("GET", f"{GATEWAY_BASE}/trading/account", headers=_auth_headers())


def get_portfolio() -> dict:
    """Account balances, buying power, and current positions (read-only)."""
    url = f"{GATEWAY_BASE}/trading/{_account_id()}/portfolio/v2"
    return _http("GET", url, headers=_auth_headers())


def get_history(symbol: str, period: str = "YEAR", aggregation: str = "ONE_DAY",
                instrument_type: str = "EQUITY") -> list[dict]:
    """OHLCV bars. period in {DAY,WEEK,MONTH,QUARTER,HALF_YEAR,YEAR,FIVE_YEARS,YTD},
    aggregation in {ONE_MINUTE,...,ONE_DAY,ONE_WEEK,ONE_MONTH,...}. Returns a list
    of bar dicts. The endpoint splits bars by session; callers pick what they need."""
    url = f"{GATEWAY_BASE}/historicdata/{instrument_type}/{symbol.upper()}/{period}/{aggregation}"
    resp = _http("GET", url, headers=_auth_headers())
    if isinstance(resp, list):
        return resp
    # public.com shape: {regularMarket: {bars: [...]}, preMarket: {...}, afterMarket: {...}}.
    # We want the regular-session daily bars for technicals.
    if isinstance(resp, dict):
        rm = resp.get("regularMarket")
        if isinstance(rm, dict) and isinstance(rm.get("bars"), list):
            return rm["bars"]
        for key in ("bars", "candles", "data", "history"):
            if isinstance(resp.get(key), list):
                return resp[key]
    return []


def get_daily_ohlcv(symbol: str, period: str = "YEAR",
                    instrument_type: str = "EQUITY") -> list[dict]:
    """Regular-session daily bars as clean floats: [{date, open, high, low, close, volume}].
    Sorted oldest->newest. Ready to feed moving-average / RSI / ATR computation."""
    out = []
    for b in get_history(symbol, period, "ONE_DAY", instrument_type):
        try:
            out.append({
                "date": (b.get("timestamp") or "")[:10],
                "open": float(b["open"]),
                "high": float(b["high"]),
                "low": float(b["low"]),
                "close": float(b["close"]),
                "volume": int(b.get("volume") or 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------
# Option market data (read-only)
#
# public.com serves REAL-TIME option quotes plus broker-grade greeks/IV, which
# is far more reliable than yfinance's delayed and frequently-empty option
# chains. These methods are DATA ONLY -- no order placement lives here.
#
# Response shapes verified empirically against the live API (2026-05-21):
#   option-expirations -> {"baseSymbol": SYM, "expirations": ["YYYY-MM-DD", ...]}
#   option-chain       -> {"baseSymbol": SYM, "calls": [contract...], "puts": [...]}
#       contract = {instrument:{symbol(OSI), type:"OPTION"}, outcome,
#                   last, bid, bidSize, ask, askSize, volume, openInterest,
#                   previousClose, oneDayChange,
#                   optionDetails:{greeks:{delta,gamma,theta,vega,rho,
#                                          impliedVolatility}, strikePrice,
#                                  midPrice}}
#   /quotes (type OPTION) -> {"quotes": [contract...]} but optionDetails is
#       null on this path (no greeks); use get_option_chain or
#       get_option_greeks when you need greeks/IV.
#   option-details/.../greeks -> {delta,gamma,theta,vega,rho,impliedVolatility}
#
# GOTCHA: every price/greek field comes back as a STRING ("31.50", "0.9134").
# Callers should float() them. A failed lookup may surface either as an
# outcome other than "SUCCESS" with null fields (no exception), or as a
# PublicAPIError -- so option consumers can catch PublicAPIError and fall back
# to yfinance, and should also treat empty/null results as "no data".
# --------------------------------------------------------------------------

OPTION_GREEK_KEYS = ("delta", "gamma", "theta", "vega", "rho", "impliedVolatility")


def get_option_expirations(symbol: str) -> list[str]:
    """Listed expiration dates (ISO 'YYYY-MM-DD', soonest first) for an equity's
    options. Returns [] when the symbol has no listed options."""
    url = f"{GATEWAY_BASE}/marketdata/{_account_id()}/option-expirations"
    body = {"instrument": {"symbol": symbol.upper(), "type": "EQUITY"}}
    resp = _http("POST", url, headers=_auth_headers(), body=body)
    exps = resp.get("expirations") if isinstance(resp, dict) else None
    return [e for e in (exps or []) if isinstance(e, str)]


def get_option_chain(symbol: str, expiration_date: str) -> dict:
    """Full real-time option chain for one expiration. expiration_date is ISO
    'YYYY-MM-DD' (use a value from get_option_expirations).

    Returns {"baseSymbol": SYM, "calls": [contract...], "puts": [contract...]}.
    Each contract is the raw public.com dict (prices/greeks are STRINGS); use
    the parse_option_contract() helper to get clean floats. Raises
    PublicAPIError if the expiration is not listed or the API rejects it."""
    url = f"{GATEWAY_BASE}/marketdata/{_account_id()}/option-chain"
    body = {"instrument": {"symbol": symbol.upper(), "type": "EQUITY"},
            "expirationDate": expiration_date}
    resp = _http("POST", url, headers=_auth_headers(), body=body)
    if not isinstance(resp, dict):
        return {"baseSymbol": symbol.upper(), "calls": [], "puts": []}
    resp.setdefault("calls", [])
    resp.setdefault("puts", [])
    return resp


def get_option_quote(osi_symbol: str) -> dict | None:
    """Real-time quote for a single OSI/OCC option symbol (e.g.
    'AAPL260618C00275000'). Returns the raw contract dict (last/bid/ask/volume/
    openInterest as STRINGS or ints), or None when the symbol is unknown.

    Note: this endpoint does NOT populate greeks (optionDetails is null here).
    Use get_option_greeks() or get_option_chain() for greeks/IV."""
    url = f"{GATEWAY_BASE}/marketdata/{_account_id()}/quotes"
    body = {"instruments": [{"symbol": osi_symbol.upper(), "type": "OPTION"}]}
    resp = _http("POST", url, headers=_auth_headers(), body=body)
    quotes = resp.get("quotes") if isinstance(resp, dict) else None
    if quotes is None and isinstance(resp, list):
        quotes = resp
    for q in quotes or []:
        if (q.get("outcome") or "").upper() == "SUCCESS":
            return q
    # Return the (null-filled) row if present so callers can inspect outcome;
    # but only when it actually matches the requested symbol.
    for q in quotes or []:
        sym = ((q.get("instrument") or {}).get("symbol") or "").upper()
        if sym == osi_symbol.upper():
            return q
    return None


def get_option_greeks(osi_symbol: str) -> dict | None:
    """Broker-grade greeks + IV for one OSI/OCC option symbol. Returns clean
    floats {delta, gamma, theta, vega, rho, implied_volatility} (IV is a
    fraction, e.g. 0.2954 = 29.54%), or None on failure. Never raises -- a
    PublicAPIError from the API is caught and turned into None so option
    consumers can fall back to yfinance."""
    try:
        url = f"{GATEWAY_BASE}/option-details/{_account_id()}/{osi_symbol.upper()}/greeks"
        resp = _http("GET", url, headers=_auth_headers())
    except PublicAPIError:
        return None
    return _parse_greeks(resp)


def _parse_greeks(raw: dict | None) -> dict | None:
    """Turn a raw greeks dict (string fields) into clean floats. Accepts either
    the top-level greeks payload or a contract's optionDetails.greeks block."""
    if not isinstance(raw, dict):
        return None
    g = raw.get("greeks") if isinstance(raw.get("greeks"), dict) else raw
    out: dict[str, float] = {}
    for k in OPTION_GREEK_KEYS:
        v = g.get(k)
        if v is None:
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    if not out:
        return None
    # Beginner-clear key for IV (broker returns it as a fraction).
    if "impliedVolatility" in out:
        out["implied_volatility"] = out["impliedVolatility"]
    return out


def _to_float(value) -> float | None:
    """public.com returns prices as strings; float() them safely."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_option_contract(contract: dict) -> dict | None:
    """Normalize one raw option contract (from get_option_chain calls/puts, or
    get_option_quote) into clean, beginner-clear floats. Returns None if the
    contract did not resolve (outcome other than SUCCESS, or no symbol).

    Output keys:
      symbol, strike, bid, ask, last, mid, volume, open_interest, previous_close,
      delta, gamma, theta, vega, rho, implied_volatility (fraction)
    Greeks/IV are present only when optionDetails carried them (the chain does;
    the bare /quotes path does not)."""
    if not isinstance(contract, dict):
        return None
    if (contract.get("outcome") or "SUCCESS").upper() != "SUCCESS":
        return None
    inst = contract.get("instrument") or {}
    sym = inst.get("symbol")
    if not sym:
        return None
    details = contract.get("optionDetails") or {}
    out: dict = {
        "symbol": sym,
        "bid": _to_float(contract.get("bid")),
        "ask": _to_float(contract.get("ask")),
        "last": _to_float(contract.get("last")),
        "mid": _to_float(details.get("midPrice")),
        "strike": _to_float(details.get("strikePrice")),
        "volume": int(contract["volume"]) if contract.get("volume") is not None else None,
        "open_interest": int(contract["openInterest"]) if contract.get("openInterest") is not None else None,
        "previous_close": _to_float(contract.get("previousClose")),
    }
    greeks = _parse_greeks(details.get("greeks"))
    if greeks:
        out["delta"] = greeks.get("delta")
        out["gamma"] = greeks.get("gamma")
        out["theta"] = greeks.get("theta")
        out["vega"] = greeks.get("vega")
        out["rho"] = greeks.get("rho")
        out["implied_volatility"] = greeks.get("implied_volatility")
    # If mid wasn't supplied, derive it from bid/ask when both are present.
    if out["mid"] is None and out["bid"] and out["ask"] and out["ask"] >= out["bid"]:
        out["mid"] = (out["bid"] + out["ask"]) / 2
    return out


# --------------------------------------------------------------------------
# CLI for testing
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="public.com data client (read-only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pq = sub.add_parser("quote", help="Real-time quote(s).")
    pq.add_argument("symbols", nargs="+")
    pq.add_argument("--type", default="EQUITY")
    ph = sub.add_parser("history", help="Historical bars.")
    ph.add_argument("symbol")
    ph.add_argument("--period", default="YEAR")
    ph.add_argument("--aggregation", default="ONE_DAY")
    ph.add_argument("--type", default="EQUITY")
    pt = sub.add_parser("token", help="Mint/show a token (debug; prints masked).")
    pe = sub.add_parser("expirations", help="List option expirations for a symbol.")
    pe.add_argument("symbol")
    pc = sub.add_parser("chain", help="Option chain for one expiration (parsed).")
    pc.add_argument("symbol")
    pc.add_argument("expiration", help="ISO date YYYY-MM-DD")
    pc.add_argument("--near", type=int, default=6, help="Strikes +/- N around ATM")
    po = sub.add_parser("optquote", help="Quote for one OSI option symbol.")
    po.add_argument("osi_symbol")
    pg = sub.add_parser("greeks", help="Greeks/IV for one OSI option symbol.")
    pg.add_argument("osi_symbol")
    args = ap.parse_args()

    try:
        if args.cmd == "quote":
            print(json.dumps(get_quotes(args.symbols, args.type), indent=2))
        elif args.cmd == "history":
            bars = get_history(args.symbol, args.period, args.aggregation, args.type)
            print(f"{len(bars)} bars; first={bars[0] if bars else None}")
            print(f"last={bars[-1] if bars else None}")
        elif args.cmd == "token":
            t = _token()
            print(f"token ok: {t[:6]}...{t[-4:]} (len {len(t)})")
        elif args.cmd == "expirations":
            exps = get_option_expirations(args.symbol)
            print(f"{args.symbol.upper()}: {len(exps)} expirations")
            for e in exps:
                print(f"  {e}")
        elif args.cmd == "chain":
            chain = get_option_chain(args.symbol, args.expiration)
            calls = [parse_option_contract(c) for c in chain.get("calls", [])]
            puts = [parse_option_contract(c) for c in chain.get("puts", [])]
            calls = [c for c in calls if c]
            puts = [p for p in puts if p]
            print(f"{args.symbol.upper()} {args.expiration}: {len(calls)} calls, {len(puts)} puts")
            mid = calls[len(calls) // 2] if calls else None
            print("sample near-ATM call:")
            print(json.dumps(mid, indent=2))
        elif args.cmd == "optquote":
            print(json.dumps(get_option_quote(args.osi_symbol), indent=2))
        elif args.cmd == "greeks":
            print(json.dumps(get_option_greeks(args.osi_symbol), indent=2))
        return 0
    except PublicAPIError as e:
        print(f"PublicAPIError: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
