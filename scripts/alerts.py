#!/usr/bin/env python3
"""Price/level alerts for the trader skill.

Watches a small list of conditions across tickers, evaluates them on a cron,
and broadcasts to the user (Me, portfolio_id=primary) when any condition trips.
yfinance is the data source; 15-min staleness is acceptable per spec.

Usage:
    alerts.py add --ticker AMZN --condition "price >= 278 AND rsi14 >= 70" --message "AMZN re-write zone"
    alerts.py list
    alerts.py remove <id>
    alerts.py reset <id>          # mark a fired alert armed again
    alerts.py check                # evaluate all armed alerts, broadcast fires
    alerts.py check --dry-run      # evaluate but do not broadcast

Storage: state/alerts.json (list of alert objects).
Broadcast: routed via broadcast.py --to <user-phone> so only the user sees alerts,
never the subscriber list.

Condition language (intentionally minimal):
    metric OP value [AND metric OP value]
    metric: price, rsi14, chg_pct, ma20, ma50, ma200, atr14, high, low, volume
    OP:     >=, <=, >, <, ==
    Special tickers: VIX (resolves to ^VIX), DXY (^DXY), TNX/10Y (^TNX)
    Compound: AND only. No OR, no parens. Keep it readable.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALERTS_PATH = ROOT / "state" / "alerts.json"
BROADCAST = ROOT / "scripts" / "broadcast.py"
PRICE = ROOT / "scripts" / "price.py"
PY = ROOT / "scripts" / ".venv" / "bin" / "python3"

sys.path.insert(0, str(ROOT / "scripts"))
import watchlist_store  # noqa: E402  (every alert ticker must be on the watchlist)

def _primary_phone() -> str:
    """Read the primary recipient's phone from broadcast_recipients.json."""
    rpath = ROOT / "broadcast_recipients.json"
    if rpath.exists():
        try:
            data = json.loads(rpath.read_text())
            for r in data.get("recipients", []):
                if r.get("portfolio_id") == "primary" and r.get("active", True):
                    return r["phone"]
        except Exception:
            pass
    return ""


USER_PHONE = _primary_phone()  # resolved from broadcast_recipients.json (portfolio_id=primary)

VALID_METRICS = {"price", "close", "rsi14", "chg_pct", "ma20", "ma50", "ma200", "atr14", "high", "low", "volume"}
VALID_OPS = {">=", "<=", ">", "<", "=="}
TICKER_ALIAS = {"VIX": "^VIX", "DXY": "DX-Y.NYB", "TNX": "^TNX", "10Y": "^TNX"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_alerts() -> list[dict]:
    if not ALERTS_PATH.exists():
        return []
    return json.loads(ALERTS_PATH.read_text())


def save_alerts(alerts: list[dict]) -> None:
    ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALERTS_PATH.write_text(json.dumps(alerts, indent=2))


def parse_condition(cond: str) -> list[tuple[str, str, float]]:
    """Parse 'metric OP value [AND metric OP value]' into [(metric, op, value), ...]."""
    parts = [p.strip() for p in re.split(r"\bAND\b", cond, flags=re.IGNORECASE)]
    out = []
    for p in parts:
        m = re.match(r"^\s*(\w+)\s*(>=|<=|==|>|<)\s*(-?\d+(?:\.\d+)?)\s*$", p)
        if not m:
            raise ValueError(f"could not parse clause: {p!r}")
        metric, op, val = m.group(1).lower(), m.group(2), float(m.group(3))
        if metric == "price":
            metric = "close"
        if metric not in VALID_METRICS:
            raise ValueError(f"unknown metric {metric!r}; valid: {sorted(VALID_METRICS)}")
        if op not in VALID_OPS:
            raise ValueError(f"unknown op {op!r}")
        out.append((metric, op, val))
    if not out:
        raise ValueError(f"no clauses in condition: {cond!r}")
    return out


def evaluate_clause(actual: float, op: str, target: float) -> bool:
    return {">=": actual >= target, "<=": actual <= target, ">": actual > target,
            "<": actual < target, "==": actual == target}[op]


def fetch_metrics(ticker: str) -> dict | None:
    """Call price.py for the resolved ticker and return its data dict, or None."""
    resolved = TICKER_ALIAS.get(ticker.upper(), ticker.upper())
    if resolved.startswith("^") or resolved.endswith(".NYB"):
        # price.py uses tradable universe; fall back to direct yfinance for index proxies
        try:
            import yfinance as yf  # type: ignore
            h = yf.Ticker(resolved).history(period="60d")
            if len(h) < 2:
                return None
            close = float(h["Close"].iloc[-1])
            prev = float(h["Close"].iloc[-2])
            return {
                "close": close,
                "chg_pct": (close - prev) / prev * 100,
                "high": float(h["High"].iloc[-1]),
                "low": float(h["Low"].iloc[-1]),
                "volume": float(h["Volume"].iloc[-1]),
                # No MAs/RSI/ATR for index proxies here; supply NaN sentinels
                "rsi14": None, "ma20": None, "ma50": None, "ma200": None, "atr14": None,
            }
        except Exception as e:
            print(f"  fetch error {ticker} ({resolved}): {e}", file=sys.stderr)
            return None
    # Regular ticker via price.py for the full metric set
    try:
        out = subprocess.run([str(PY), str(PRICE), resolved, "--json"],
                             capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return None
        d = json.loads(out.stdout).get("data", {})
        return d or None
    except Exception as e:
        print(f"  fetch error {ticker}: {e}", file=sys.stderr)
        return None


def cmd_add(args: argparse.Namespace) -> int:
    clauses = parse_condition(args.condition)
    alerts = load_alerts()
    ticker = args.ticker.upper()
    aid = args.id or f"{ticker.lower()}-{uuid.uuid4().hex[:6]}"
    if any(a["id"] == aid for a in alerts):
        print(f"id {aid} already exists; use --id to set a different one", file=sys.stderr)
        return 1

    # Hypothesis = why this alert exists / what we expect if it fires. The brief
    # reads it to re-validate before acting, so a fired trigger is never a blind
    # buy signal. Falls back to the short message if not given.
    hypothesis = args.hypothesis or args.message
    macro = bool(args.macro) or watchlist_store.is_macro(ticker)

    # Invariant: every non-macro alert ticker must be on the watchlist. If it
    # isn't, auto-create a stub (carrying the hypothesis) so the two never drift.
    if not macro and not watchlist_store.has_ticker(ticker):
        watchlist_store.add_stub(ticker, hypothesis)
        print(f"note: {ticker} was not on the watchlist; added a stub entry "
              f"(needs a real thesis) so the alert stays in sync with the watchlist.")

    alert = {
        "id": aid,
        "ticker": ticker,
        "condition": args.condition,
        "clauses": [{"metric": m, "op": op, "value": v} for m, op, v in clauses],
        "message": args.message,
        "hypothesis": hypothesis,
        "macro": macro,
        "state": "armed",
        "fired_at": None,
        "acknowledged_at": None,
        "created": now_iso(),
        "expires": args.expires,
    }
    alerts.append(alert)
    save_alerts(alerts)
    print(f"added {aid}: {ticker} {args.condition}" + ("  [macro]" if macro else ""))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    alerts = load_alerts()
    if not alerts:
        print("(no alerts)")
        return 0
    armed = [a for a in alerts if a["state"] == "armed"]
    fired = [a for a in alerts if a["state"] == "fired"]
    print(f"ARMED ({len(armed)}):")
    for a in armed:
        print(f"  {a['id']:30} {a['ticker']:8} {a['condition']:50} -> {a['message']}")
    if fired:
        print(f"\nFIRED ({len(fired)}):")
        for a in fired:
            print(f"  {a['id']:30} {a['ticker']:8} fired_at={a['fired_at']}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    alerts = load_alerts()
    before = len(alerts)
    alerts = [a for a in alerts if a["id"] != args.id]
    if len(alerts) == before:
        print(f"no alert with id {args.id}", file=sys.stderr)
        return 1
    save_alerts(alerts)
    print(f"removed {args.id}")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    alerts = load_alerts()
    for a in alerts:
        if a["id"] == args.id:
            a["state"] = "armed"
            a["fired_at"] = None
            a["acknowledged_at"] = None
            save_alerts(alerts)
            print(f"reset {args.id}")
            return 0
    print(f"no alert with id {args.id}", file=sys.stderr)
    return 1


def cmd_acknowledge(args: argparse.Namespace) -> int:
    """Mark a fired alert as reviewed so it stops surfacing in the brief.

    The brief surfaces fired alerts for re-validation; once /trader has read the
    hypothesis, validated it, and suggested an action, it acknowledges the alert
    so the same fire isn't re-surfaced on every subsequent run.
    """
    alerts = load_alerts()
    for a in alerts:
        if a["id"] == args.id:
            if a.get("state") != "fired":
                print(f"{args.id} is '{a.get('state')}', not 'fired'; nothing to acknowledge",
                      file=sys.stderr)
                return 1
            a["state"] = "acknowledged"
            a["acknowledged_at"] = now_iso()
            save_alerts(alerts)
            print(f"acknowledged {args.id} (reviewed; will stop surfacing in the brief)")
            return 0
    print(f"no alert with id {args.id}", file=sys.stderr)
    return 1


def fired_unacknowledged() -> list[dict]:
    """Alerts that have fired but haven't been reviewed/acknowledged in a brief yet.
    Imported by brief.py to surface them for re-validation."""
    return [a for a in load_alerts() if a.get("state") == "fired"]


def evaluate_alert(alert: dict, metrics_cache: dict) -> tuple[bool, dict, str]:
    """Returns (fired, evidence, reason). Evidence is the actual values used."""
    tkr = alert["ticker"]
    if tkr not in metrics_cache:
        metrics_cache[tkr] = fetch_metrics(tkr)
    m = metrics_cache[tkr]
    if m is None:
        return False, {}, "data unavailable"
    evidence = {}
    for clause in alert["clauses"]:
        metric, op, val = clause["metric"], clause["op"], clause["value"]
        actual = m.get(metric)
        evidence[metric] = actual
        if actual is None:
            return False, evidence, f"{metric} unavailable"
        if not evaluate_clause(float(actual), op, val):
            return False, evidence, f"{metric}={actual:.2f} not {op} {val}"
    return True, evidence, "all clauses true"


def cmd_check(args: argparse.Namespace) -> int:
    alerts = load_alerts()
    armed = [a for a in alerts if a["state"] == "armed"]
    if not armed:
        if args.verbose:
            print("(no armed alerts)")
        return 0
    metrics_cache: dict = {}
    fired_list = []
    for a in armed:
        fired, evidence, reason = evaluate_alert(a, metrics_cache)
        if args.verbose:
            marker = "FIRE" if fired else "    "
            print(f"  {marker} {a['id']:30} {a['ticker']:8} {reason}")
        if not fired:
            continue
        # Compose broadcast text
        ev_parts = []
        for clause in a["clauses"]:
            actual = evidence.get(clause["metric"])
            if isinstance(actual, (int, float)):
                ev_parts.append(f"{clause['metric']}={actual:.2f}")
        ev_str = ", ".join(ev_parts)
        msg = f"ALERT [{a['ticker']}] {a['message']} | {ev_str}"
        fired_list.append((a, msg))
    if not fired_list:
        return 0
    # Broadcast each. Use --to to route only to user.
    for a, msg in fired_list:
        print(f"FIRING: {msg}")
        if args.dry_run:
            continue
        try:
            subprocess.run([str(PY), str(BROADCAST), "--to", USER_PHONE, msg],
                           check=False, timeout=30)
        except Exception as e:
            print(f"  broadcast error: {e}", file=sys.stderr)
        a["state"] = "fired"
        a["fired_at"] = now_iso()
    if not args.dry_run:
        save_alerts(alerts)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Trader skill price/level alerts.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("add", help="Add a new alert.")
    pa.add_argument("--ticker", required=True)
    pa.add_argument("--condition", required=True, help='e.g. "price >= 278 AND rsi14 >= 70"')
    pa.add_argument("--message", required=True, help="Short label shown when the alert fires.")
    pa.add_argument("--hypothesis", default=None,
                    help="Why this alert exists / what we expect if it fires. The brief reads "
                         "this to re-validate before acting (a fired trigger is not a buy signal). "
                         "Defaults to --message if omitted.")
    pa.add_argument("--macro", action="store_true",
                    help="Mark as a macro level (10Y/VIX/DXY). Macro alerts are exempt from the "
                         "rule that every alert ticker must be on the watchlist.")
    pa.add_argument("--id", default=None)
    pa.add_argument("--expires", default=None, help="ISO date when alert auto-archives (optional)")
    pa.set_defaults(func=cmd_add)

    pl = sub.add_parser("list", help="List alerts.")
    pl.set_defaults(func=cmd_list)

    pr = sub.add_parser("remove", help="Remove an alert by id.")
    pr.add_argument("id")
    pr.set_defaults(func=cmd_remove)

    prs = sub.add_parser("reset", help="Re-arm a previously fired alert.")
    prs.add_argument("id")
    prs.set_defaults(func=cmd_reset)

    pack = sub.add_parser("acknowledge",
                          help="Mark a fired alert as reviewed so it stops surfacing in the brief.")
    pack.add_argument("id")
    pack.set_defaults(func=cmd_acknowledge)

    pc = sub.add_parser("check", help="Evaluate all armed alerts; broadcast fires.")
    pc.add_argument("--dry-run", action="store_true")
    pc.add_argument("--verbose", "-v", action="store_true")
    pc.set_defaults(func=cmd_check)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
