#!/usr/bin/env python3
"""Hourly market-watch orchestrator.

User invokes this hourly via crontab during their active window (weekdays,
roughly 5 AM-4 PM PT covers pre-market + RTH + early after-hours). Each fire:

1. Skip if weekend or US-market-holiday (no broadcast, log only).
2. Run `brief.py hourly` as a subprocess; parse the JSON digest.
3. Load today's morning baseline from `state/cache/morning_digest_<date>.json`
   if present; if absent, run in degraded mode (fewer alerts).
4. For each tier-1 user position and watchlist item, run signal finders:
   position_alerts (STOP_NEAR/HIT, TARGET_NEAR/HIT, EXIT, TRIM),
   watchlist_transitions (NOT_TRIGGERED -> TRIGGERED), intraday_gaps
   (>= 1.5 ATR underlying move vs prior close), new_clusters (>= 2 sources,
   not in morning candidates, no earnings within 7d), earnings_imminent
   (today/tomorrow earner that's a user position), crypto_alerts (XRP and
   BTC moves vs prior close).
5. Dedup against `state/cache/hourly_alerts_sent_<date>.json` so the same
   alert doesn't fire every hour for the rest of the day.
6. Compose per-recipient messages. Three audience tiers, gated on the
   recipient's `portfolio_id` and `hourly_enabled` flags in
   `broadcast_recipients.json`. v1: only recipients with hourly_enabled=true
   receive the hourly broadcast.
7. Send via `broadcast.py --to <phone>` once per recipient. Save drafts to
   `state/broadcasts/`. Append research_log + daily-log entries.

Run modes:
  hourly_broadcast.py                 # live: run + broadcast
  hourly_broadcast.py --dry-run       # run + compose but don't send
  hourly_broadcast.py --save          # additionally save drafts to disk
  hourly_broadcast.py --force         # bypass weekend/holiday gate
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PY = SCRIPTS / ".venv" / "bin" / "python3"
STATE = ROOT / "state"
CACHE = STATE / "cache"
BROADCASTS_DIR = STATE / "broadcasts"
DAILY_LOG_DIR = STATE / "daily_log"
RECIPIENTS_PATH = ROOT / "broadcast_recipients.json"

PT = ZoneInfo("America/Los_Angeles")

# Thresholds (codify the user's stated maximize-profit calibrations)
STOP_NEAR_PCT = 0.01     # within 1% of stop = pre-stop heads-up
TARGET_NEAR_PCT = 0.01   # within 1% of target = pre-target heads-up
INTRADAY_ATR_THRESHOLD = 1.5  # >= 1.5 ATR move vs prior close = gap alert
CRYPTO_PCT_THRESHOLD = 5.0    # >= 5% intraday move on XRP/BTC = crypto alert


# -------------------- session classification --------------------

def now_pt() -> datetime:
    return datetime.now(tz=PT)


def classify_session(dt: datetime | None = None) -> str:
    """Classify the current PT time as PRE_MARKET / RTH / AFTER_HOURS / CLOSED.
    Boundaries are US equity market hours converted to PT.
    """
    dt = dt or now_pt()
    h, m = dt.hour, dt.minute
    minutes = h * 60 + m
    pre_start = 4 * 60        # 04:00 PT (07:00 ET = US ECN open)
    rth_start = 6 * 60 + 30   # 06:30 PT (09:30 ET)
    rth_end = 13 * 60         # 13:00 PT (16:00 ET)
    ah_end = 17 * 60          # 17:00 PT (20:00 ET)
    if minutes < pre_start:
        return "CLOSED"
    if minutes < rth_start:
        return "PRE_MARKET"
    if minutes < rth_end:
        return "RTH"
    if minutes < ah_end:
        return "AFTER_HOURS"
    return "CLOSED"


def is_market_day(dt: datetime | None = None) -> tuple[bool, str]:
    """Return (open, reason). Closed = weekend or US-market-holiday.
    Uses pandas_market_calendars (already a project dependency)."""
    dt = dt or now_pt()
    if dt.weekday() >= 5:
        return False, f"weekend ({dt.strftime('%A')})"
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(start_date=dt.date(), end_date=dt.date())
        if sched.empty:
            return False, "US market holiday"
    except Exception as e:
        # If pandas_market_calendars import fails, fall back to weekday-only.
        print(f"[hourly] WARN pandas_market_calendars unavailable: {e}",
              file=sys.stderr)
    return True, "open"


# -------------------- subprocess + I/O helpers --------------------

def run_brief_hourly(timeout: int = 180) -> dict | None:
    cmd = [str(PY), str(SCRIPTS / "brief.py"), "hourly"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[hourly] ERR brief.py hourly timed out after {timeout}s",
              file=sys.stderr)
        return None
    if r.returncode != 0:
        print(f"[hourly] ERR brief.py hourly exit={r.returncode}: {r.stderr[:300]}",
              file=sys.stderr)
        return None
    out = r.stdout.strip()
    last = next((ln for ln in reversed(out.splitlines()) if ln.startswith("{")), None)
    if not last:
        print("[hourly] ERR no JSON line in brief.py hourly output", file=sys.stderr)
        return None
    try:
        return json.loads(last)
    except json.JSONDecodeError as e:
        print(f"[hourly] ERR JSON parse: {e}", file=sys.stderr)
        return None


def load_morning_baseline(today: str) -> dict | None:
    p = CACHE / f"morning_digest_{today}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as e:
        print(f"[hourly] WARN morning_digest cache corrupt: {e}", file=sys.stderr)
        return None


def load_recipients() -> list[dict]:
    data = json.loads(RECIPIENTS_PATH.read_text())
    return [r for r in data["recipients"] if r.get("active", True)]


def load_portfolio_for(portfolio_id: str | None) -> dict | None:
    """Best-effort load. Returns None if id is missing or file absent."""
    if not portfolio_id:
        return None
    sys.path.insert(0, str(SCRIPTS))
    from _common import load_portfolio  # noqa
    try:
        return load_portfolio(portfolio_id)
    except FileNotFoundError as e:
        print(f"[hourly] WARN {e}", file=sys.stderr)
        return None


def load_dedup_ledger(today: str) -> dict:
    p = CACHE / f"hourly_alerts_sent_{today}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save_dedup_ledger(today: str, ledger: dict) -> None:
    p = CACHE / f"hourly_alerts_sent_{today}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ledger, indent=2))


def alert_key(audience: str, kind: str, ticker: str, detail: str = "") -> str:
    return f"{audience}::{kind}::{ticker}::{detail}"


# -------------------- signal finders --------------------

def find_position_alerts(hourly: dict, portfolio: dict | None) -> list[dict]:
    """Find tier-1 user positions whose primary_action is non-HOLD.

    Three categories surfaced:
      - STOP/TARGET/EXIT/TRIM (already triggered per position_review).
      - STOP_NEAR (within STOP_NEAR_PCT of the stop level).
      - TARGET_NEAR (within TARGET_NEAR_PCT of the target level).
    Tier-1 filter: position has tier1=True (or field absent => default True).
    """
    if portfolio is None:
        return []
    user_pos = portfolio.get("user_positions", [])
    by_key = {}  # (ticker, option_symbol or "") -> position dict
    for p in user_pos:
        by_key[(p["ticker"], p.get("option_symbol") or "")] = p

    alerts: list[dict] = []
    for rev in hourly.get("open_positions_review", []) or []:
        if rev.get("book") != "user":
            continue
        ticker = rev.get("ticker")
        action = rev.get("primary_action", "HOLD")
        # Tier-1 lookup. position_review reviews don't echo option_symbol
        # so this matches by ticker only; collisions on options are rare
        # within tier-1.
        pos = next((p for p in user_pos if p["ticker"] == ticker), None)
        if pos is None:
            continue
        if not pos.get("tier1", True):
            continue
        if action != "HOLD":
            alerts.append({
                "kind": "position_action",
                "ticker": ticker,
                "action": action,
                "reasons": rev.get("reasons", []),
                "stop": pos.get("stop"),
                "target": pos.get("target"),
                "qty": pos.get("qty"),
                "kind_inst": pos.get("kind"),
                "option_symbol": pos.get("option_symbol"),
                "last": rev.get("last"),
                "pnl_pct": rev.get("pnl_pct"),
            })
        # Pre-stop / pre-target heads-up
        last = rev.get("last")
        if last and pos.get("stop"):
            stop = float(pos["stop"])
            dist = (last - stop) / stop if pos.get("side") == "LONG" else (stop - last) / stop
            if 0 < dist <= STOP_NEAR_PCT:
                alerts.append({
                    "kind": "stop_near",
                    "ticker": ticker,
                    "stop": stop,
                    "last": last,
                    "dist_pct": dist * 100,
                    "qty": pos.get("qty"),
                })
        if last and pos.get("target"):
            target = float(pos["target"])
            dist = (target - last) / target if pos.get("side") == "LONG" else (last - target) / target
            if 0 < dist <= TARGET_NEAR_PCT:
                alerts.append({
                    "kind": "target_near",
                    "ticker": ticker,
                    "target": target,
                    "last": last,
                    "dist_pct": dist * 100,
                    "qty": pos.get("qty"),
                })
    return alerts


def find_watchlist_transitions(hourly: dict, morning: dict | None) -> list[dict]:
    """Items now TRIGGERED that were not TRIGGERED in the morning baseline."""
    morning_status = {}
    if morning:
        for it in morning.get("watchlist", []) or []:
            morning_status[it.get("ticker")] = it.get("status")
    out: list[dict] = []
    for it in hourly.get("watchlist", []) or []:
        ticker = it.get("ticker")
        status = it.get("status")
        if status != "TRIGGERED":
            continue
        prior = morning_status.get(ticker)
        if prior == "TRIGGERED":
            continue  # already triggered at open; not a new transition
        out.append({
            "kind": "watchlist_triggered",
            "ticker": ticker,
            "last": it.get("last"),
            "entry_trigger": it.get("entry_trigger"),
            "prior_status": prior,
        })
    return out


def find_new_clusters(hourly: dict, morning: dict | None) -> list[dict]:
    """Cross-scanner clusters present in hourly but absent from morning."""
    morning_set: set[str] = set()
    if morning:
        morning_set = {c.get("ticker") for c in morning.get("candidates", []) or []}
    earnings_in_7d = set((hourly.get("earnings_within_7d") or []) +
                         ((morning or {}).get("earnings_within_7d") or []))
    out: list[dict] = []
    for c in hourly.get("candidates", []) or []:
        tk = c.get("ticker")
        if not tk or tk in morning_set:
            continue
        if tk in earnings_in_7d:
            continue  # don't chase into earnings
        out.append({
            "kind": "new_cluster",
            "ticker": tk,
            "sources": c.get("sources", []),
            "details": c.get("details", []),
        })
    return out


def find_intraday_gaps(hourly: dict, portfolio: dict | None) -> list[dict]:
    """User positions whose underlying moved >= INTRADAY_ATR_THRESHOLD ATR vs
    prior close. Uses the regime/movers data from the hourly digest if
    available; falls back to a per-ticker price.py probe for tier-1 names
    not covered. Cheap because price.py uses a 5-min cache."""
    if portfolio is None:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    user_pos = [p for p in portfolio.get("user_positions", []) if p.get("tier1", True)]
    user_tickers = {p["ticker"] for p in user_pos
                    if p.get("kind") in ("stock", "etf")}  # skip options + crypto
    # Probe each unique underlying once via price.py
    for tk in sorted(user_tickers):
        if tk in seen:
            continue
        seen.add(tk)
        try:
            r = subprocess.run(
                [str(PY), str(SCRIPTS / "price.py"), tk, "--json"],
                capture_output=True, text=True, timeout=20,
            )
            line = next((ln for ln in reversed(r.stdout.splitlines())
                         if ln.startswith("{")), None)
            if not line:
                continue
            d = json.loads(line)
            data = d.get("data", {}) or {}
            close = data.get("close")
            chg = data.get("chg_pct")
            atr = data.get("atr14")
            if close is None or atr is None or chg is None or atr == 0:
                continue
            move_dollars = close * (chg / 100)
            atr_multiple = abs(move_dollars) / atr
            if atr_multiple >= INTRADAY_ATR_THRESHOLD:
                out.append({
                    "kind": "intraday_gap",
                    "ticker": tk,
                    "close": close,
                    "chg_pct": chg,
                    "atr14": atr,
                    "atr_multiple": round(atr_multiple, 2),
                    "direction": "up" if chg > 0 else "down",
                })
        except Exception:
            continue
    return out


def find_earnings_imminent(hourly: dict, portfolio: dict | None,
                           dedup: dict) -> list[dict]:
    """User-held tickers reporting earnings today or tomorrow, not yet
    alerted today (per dedup ledger)."""
    if portfolio is None:
        return []
    held = {p["ticker"] for p in portfolio.get("user_positions", [])}
    earnings_7d = hourly.get("earnings_within_7d") or []
    # Without per-ticker date detail we fall back to "is in 7d window".
    out: list[dict] = []
    for tk in earnings_7d:
        if tk not in held:
            continue
        out.append({"kind": "earnings_imminent", "ticker": tk})
    return out


def find_crypto_alerts(portfolio: dict | None) -> list[dict]:
    """Track XRP if user holds it; always include BTC for context. Alert if
    >= CRYPTO_PCT_THRESHOLD intraday."""
    held_crypto = set()
    if portfolio:
        for p in portfolio.get("user_positions", []):
            if p.get("kind") == "crypto":
                held_crypto.add(p["ticker"])
    # Always check BTC even if not held (regime context).
    universe = sorted(held_crypto | {"BTC"})
    out: list[dict] = []
    for tk in universe:
        symbol = f"{tk}-USD"
        try:
            r = subprocess.run(
                [str(PY), str(SCRIPTS / "price.py"), symbol, "--json"],
                capture_output=True, text=True, timeout=20,
            )
            line = next((ln for ln in reversed(r.stdout.splitlines())
                         if ln.startswith("{")), None)
            if not line:
                continue
            d = json.loads(line)
            data = d.get("data", {}) or {}
            close = data.get("close")
            chg = data.get("chg_pct")
            if close is None or chg is None:
                continue
            if abs(chg) < CRYPTO_PCT_THRESHOLD:
                continue
            out.append({
                "kind": "crypto_alert",
                "ticker": tk,
                "close": close,
                "chg_pct": chg,
                "held": tk in held_crypto,
            })
        except Exception:
            continue
    return out


# -------------------- per-recipient composer --------------------

def audience_tier(recipient: dict) -> str:
    pid = recipient.get("portfolio_id")
    if pid == "primary":
        return "self"
    if pid:
        return "subscriber_with_portfolio"
    return "subscriber_generic"


def compose_for_recipient(hourly: dict, alerts: dict[str, list], recipient: dict,
                          recipient_portfolio: dict | None,
                          session: str) -> str | None:
    """Build the message body for one recipient. Returns None if nothing
    material to say."""
    tier = audience_tier(recipient)
    held_tickers = set()
    if recipient_portfolio:
        held_tickers = {p["ticker"] for p in recipient_portfolio.get("user_positions", [])}

    lines: list[str] = []
    stamp = now_pt().strftime("%H:%M PT")
    header_label = "[HOURLY " + stamp + (f" {session}]" if session != "RTH" else "]")
    lines.append(f"{header_label} {hourly.get('headline','')}")
    lines.append("")

    # Position-specific alerts: only "self" tier sees these.
    if tier == "self":
        pos_alerts = alerts.get("position_alerts", [])
        if pos_alerts:
            lines.append("POSITION ACTIONS:")
            for a in pos_alerts:
                if a["kind"] == "position_action":
                    qty_str = f" qty={a['qty']}" if a.get("qty") else ""
                    sym_str = f" ({a['option_symbol']})" if a.get("option_symbol") else ""
                    last_str = f" last={a['last']}" if a.get("last") else ""
                    pnl_str = f" pnl={a['pnl_pct']:+.1f}%" if a.get("pnl_pct") is not None else ""
                    reasons = "; ".join(a.get("reasons") or [])[:200]
                    lines.append(f"- {a['ticker']}{sym_str} {a['action']}{qty_str}{last_str}{pnl_str}")
                    if reasons:
                        lines.append(f"  reason: {reasons}")
                elif a["kind"] == "stop_near":
                    lines.append(
                        f"- {a['ticker']} {a['dist_pct']:.1f}% from stop ${a['stop']} "
                        f"(last {a['last']}). Stop GTC will fire if breached."
                    )
                elif a["kind"] == "target_near":
                    lines.append(
                        f"- {a['ticker']} {a['dist_pct']:.1f}% from target ${a['target']} "
                        f"(last {a['last']}). Trim trailing-stop or take partial."
                    )
            lines.append("")

        gaps = alerts.get("intraday_gaps", [])
        if gaps:
            lines.append("INTRADAY GAPS (>= 1.5 ATR vs prior close):")
            for g in gaps:
                arrow = "up" if g["direction"] == "up" else "down"
                lines.append(
                    f"- {g['ticker']}: {g['chg_pct']:+.2f}% {arrow} "
                    f"({g['atr_multiple']}x ATR). Last {g['close']}."
                )
            lines.append("")

        earnings = alerts.get("earnings_imminent", [])
        if earnings:
            lines.append("EARNINGS HEADS-UP (you hold):")
            for e in earnings:
                lines.append(f"- {e['ticker']}: reports within 7d. Decide hedge or hold.")
            lines.append("")

    # Crypto alerts: self + subscriber tiers (BTC is generic context).
    crypto = alerts.get("crypto_alerts", [])
    if crypto:
        for c in crypto:
            label = "you hold" if c["held"] else "context"
            if tier != "self" and not c["held"]:
                lines.append(f"CRYPTO: {c['ticker']} {c['chg_pct']:+.2f}% (last {c['close']}).")
            elif tier == "self":
                lines.append(
                    f"CRYPTO ({label}): {c['ticker']} {c['chg_pct']:+.2f}% "
                    f"(last {c['close']})."
                )

    # Watchlist transitions: all tiers see them (generic, no portfolio info).
    wl = alerts.get("watchlist_transitions", [])
    if wl:
        lines.append("WATCHLIST TRIGGERED:")
        for w in wl:
            lines.append(
                f"- {w['ticker']} entered trigger zone (last {w['last']}, "
                f"trigger {w['entry_trigger']})."
            )
        lines.append("")

    # New BUY clusters: all tiers, but subscriber_with_portfolio filters
    # tickers the recipient already holds.
    clusters = alerts.get("new_clusters", [])
    if clusters:
        filtered = clusters
        if tier == "subscriber_with_portfolio":
            filtered = [c for c in clusters if c["ticker"] not in held_tickers]
        if filtered:
            lines.append("NEW BUY IDEAS (cluster of >= 2 signals):")
            for c in filtered:
                src = "+".join(c.get("sources", []))
                lines.append(f"- {c['ticker']}: {src}.")
            lines.append("")

    # If only the header remains, we have nothing to say.
    body = "\n".join(lines).strip()
    # Header always renders; check if anything beyond it is present.
    if body == header_label.strip() or len([ln for ln in lines if ln.strip()]) <= 1:
        return None
    return body


# -------------------- daily log + research log --------------------

def find_today_daily_log() -> Path:
    """Return path to today's daily log file. If multiple match the date
    prefix, pick the most recently modified. If none, create a fresh one."""
    today = date.today().isoformat()
    candidates = sorted(DAILY_LOG_DIR.glob(f"{today}_*.md"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if candidates:
        return candidates[0]
    DAILY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    p = DAILY_LOG_DIR / f"{today}_hourly.md"
    if not p.exists():
        p.write_text(f"# {today} hourly\n\n## Hourly updates\n\n")
    return p


def append_daily_log(line: str) -> None:
    p = find_today_daily_log()
    text = p.read_text()
    if "## Hourly updates" not in text:
        text = text.rstrip() + "\n\n## Hourly updates\n\n"
        p.write_text(text)
        text = p.read_text()
    # Append after the section header (find next blank line region).
    # Simple approach: append at end of file.
    with p.open("a") as f:
        f.write(line.rstrip() + "\n")


def log_research(started_iso: str, ended_iso: str, summary: str) -> None:
    subprocess.run(
        [str(PY), str(SCRIPTS / "research.py"), "log",
         "--start", started_iso, "--end", ended_iso, "--kind", "hourly",
         "--scripts", "brief.py,hourly_broadcast.py", "--summary", summary],
        capture_output=True,
    )


# -------------------- main --------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Don't actually broadcast.")
    ap.add_argument("--save", action="store_true", help="Save composed messages to disk.")
    ap.add_argument("--force", action="store_true",
                    help="Bypass weekend/holiday gate.")
    args = ap.parse_args()

    started_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pt = now_pt()
    today = pt.date().isoformat()
    session = classify_session(pt)
    open_today, why = is_market_day(pt)

    if not open_today and not args.force:
        msg = f"- {pt.strftime('%H:%M PT')} session={session} skipped ({why})"
        append_daily_log(msg)
        log_research(started_iso, started_iso,
                     f"hourly skipped: {why}; session={session}")
        print(f"[hourly] skipped: {why}", file=sys.stderr)
        return 0

    if session == "CLOSED" and not args.force:
        msg = f"- {pt.strftime('%H:%M PT')} session=CLOSED skipped (outside extended hours)"
        append_daily_log(msg)
        log_research(started_iso, started_iso, "hourly skipped: session=CLOSED")
        print("[hourly] skipped: session=CLOSED", file=sys.stderr)
        return 0

    # Run the hourly digest.
    hourly = run_brief_hourly()
    if hourly is None:
        msg = f"- {pt.strftime('%H:%M PT')} session={session} ERROR: brief.py hourly failed"
        append_daily_log(msg)
        return 2

    morning = load_morning_baseline(today)
    no_baseline = morning is None

    # Run signal finders against the user's own portfolio (primary).
    primary_portfolio = load_portfolio_for("primary")
    dedup = load_dedup_ledger(today)

    alerts = {
        "position_alerts": find_position_alerts(hourly, primary_portfolio),
        "watchlist_transitions": find_watchlist_transitions(hourly, morning),
        "new_clusters": find_new_clusters(hourly, morning),
        "intraday_gaps": find_intraday_gaps(hourly, primary_portfolio),
        "earnings_imminent": find_earnings_imminent(hourly, primary_portfolio, dedup),
        "crypto_alerts": find_crypto_alerts(primary_portfolio),
    }

    # In degraded mode (no morning baseline) suppress new_clusters since we
    # can't tell what's actually new vs already-known at open.
    if no_baseline:
        alerts["new_clusters"] = []

    # Dedup — same alert key in dedup ledger gets dropped.
    deduped: dict[str, list] = {}
    new_dedup_entries: list[str] = []
    for kind, lst in alerts.items():
        kept = []
        for a in lst:
            tk = a.get("ticker", "")
            detail = a.get("action", "") or a.get("kind", "")
            key = alert_key("primary", a.get("kind", kind), tk, detail)
            if key in dedup:
                continue
            kept.append(a)
            new_dedup_entries.append(key)
        deduped[kind] = kept
    alerts = deduped

    total = sum(len(v) for v in alerts.values())
    if total == 0:
        msg = (f"- {pt.strftime('%H:%M PT')} session={session} no signals "
               f"(baseline={'present' if not no_baseline else 'absent'})")
        append_daily_log(msg)
        log_research(started_iso, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     f"hourly no signals; session={session}")
        print(f"[hourly] no signals to broadcast", file=sys.stderr)
        return 0

    # Compose per-recipient.
    recipients = load_recipients()
    sent_summary: list[str] = []
    for recipient in recipients:
        if not recipient.get("hourly_enabled", False):
            continue
        rp = load_portfolio_for(recipient.get("portfolio_id"))
        body = compose_for_recipient(hourly, alerts, recipient, rp, session)
        if body is None:
            continue
        phone = recipient["phone"]
        name = recipient.get("name", "?")

        # Always persist composed message to disk for audit trail. The
        # --save flag is now redundant (kept for back-compat), and live cron
        # runs no longer lose what was actually broadcast.
        BROADCASTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = pt.strftime("%H%M")
        suffix = phone.replace("+", "p")
        draft = BROADCASTS_DIR / f"{today}_{stamp}_{suffix}.txt"
        draft.write_text(body + "\n")

        if args.dry_run:
            print(f"[DRY RUN] would send to {name} ({phone}), {len(body)} chars")
            print(body)
            print("---")
            sent_summary.append(f"{name}(dry)")
            continue

        # Live send
        r = subprocess.run(
            [str(PY), str(SCRIPTS / "broadcast.py"), "--to", phone, body],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            sent_summary.append(f"{name}({sum(len(v) for v in alerts.values())} sigs)")
        else:
            sent_summary.append(f"{name}(ERR rc={r.returncode})")
            print(f"[hourly] broadcast.py failed for {name}: {r.stderr[:200]}",
                  file=sys.stderr)

    # Persist dedup ledger
    if not args.dry_run:
        for k in new_dedup_entries:
            dedup[k] = pt.isoformat()
        save_dedup_ledger(today, dedup)

    # Daily log
    sigs_summary = ", ".join(f"{k}={len(v)}" for k, v in alerts.items() if v)
    line = (f"- {pt.strftime('%H:%M PT')} session={session} "
            f"signals: {sigs_summary} | sent: {', '.join(sent_summary) or 'none'}")
    append_daily_log(line)
    log_research(started_iso, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 f"hourly {session} {sigs_summary} sent={','.join(sent_summary)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
