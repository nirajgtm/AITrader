#!/usr/bin/env python3
"""Entry point for the autonomous trader (/trader public_api_autonomous).

This builds the deterministic DECISION CONTEXT each run; it does NOT decide buys
on its own. The flow:
  1. Gate    -- date/time + market open? armed? kill-switch?
  2. Account -- cash + live positions (read-only).
  3. Settled -- GFV-proof settled cash available to spend.
  4. Manage  -- each held position vs its stored hypothesis: stop/target/time
                checks -> a SUGGESTED action for the LLM to re-validate.
  5. Surface -- candidates from the latest brief digest (NOT re-run) + fresh
                quotes + hard guardrail blockers + a technical-led score.
  6. Emit    -- one JSON context the skill (LLM) reasons over, then executes the
                decided trades via order_client (preflight + place, armed-gated).

Read-only throughout (uses scripts/ read-only tools); order_client is the only
thing that can place. Disarmed by default -> the whole run is a dry-run.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

DIR = Path(__file__).resolve().parent
ROOT = DIR.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(DIR))

import random                    # noqa: E402

import publicdotcom_api as pub   # noqa: E402  read-only client
import guards                    # noqa: E402
import settlement                # noqa: E402
import history                   # noqa: E402
import positions                 # noqa: E402  (hypotheses store)
import approvals                 # noqa: E402  (async approval queue)
import change_log                # noqa: E402  (timeline + rejected registry)
import risk_state                # noqa: E402  (evolving risk params)
import reflections               # noqa: E402  (working / not-working logs)
import autonomous_watchlist      # noqa: E402  (the system's OWN anticipated-entry watchlist)
import crypto_strategy           # noqa: E402  (24/7 crypto rules + evaluator)
import temperament               # noqa: E402  (operating disposition / persona dials)
import watchlist_store           # noqa: E402  (/trader main watchlist — active entries fed into discovery
import memory                    # noqa: E402  (indexed long-term mind memory: 6 buckets + index)
import agenda                    # noqa: E402  (open thought queue + held tensions)
import instructions              # noqa: E402  (the user's instruction inbox)
import shadow_trades             # noqa: E402  (paper conviction book -- marked to market each run)
import agent_controls            # noqa: E402  (per-agent enable/disable + cadence gate)
import agent_signals             # noqa: E402  (advisory signal inbox -- names signal agents surface to the mind)

AMENDMENT_RANDOM_FLOOR = 1 / 50  # stochastic trigger when no urgent pattern

STATE_DIR = DIR / "state"
CACHE_DIR = ROOT / "state" / "cache"
MIND_DIR = STATE_DIR / "mind"
IDENTITY_PATH = MIND_DIR / "identity.md"
DIRECTIVE_PATH = MIND_DIR / "directive.json"
CONTROLS_SEEN = MIND_DIR / "controls_seen.json"

BANNED = {t.upper() for t in (guards.load_config().get("restricted_tickers") or [])}  # owner trading restriction, e.g. an employer's stock; set restricted_tickers in config.json (gitignored, per-machine)
TIME_STOP_DAYS = 10

# public.com resting stop orders are DAY orders (no GTC). At the session close they
# die and come back REJECTED/CANCELLED/EXPIRED, leaving a whole-share position
# unprotected until re-placed. Any status NOT in this set means the stop is not live.
STOP_WORKING_STATUSES = {"OPEN", "QUEUED", "PENDING", "NEW", "ACCEPTED", "WORKING",
                         "SUBMITTED", "PENDING_NEW", "RECEIVED", "PARTIALLY_FILLED"}

# Leveraged / inverse ETFs: excluded from auto-discovery. They decay over multi-day
# holds (daily-reset path dependence) so they're unsuitable for this swing book; a
# discovery scan surfaces them on big up days but they're noise here.
LEVERAGED_ETF = {"SOXL", "SOXS", "TQQQ", "SQQQ", "SPXL", "SPXU", "UPRO", "SPXS",
                 "TNA", "TZA", "URTY", "SRTY", "UDOW", "SDOW", "FAS", "FAZ",
                 "LABU", "LABD", "NUGT", "DUST", "JNUG", "JDST", "YINN", "YANG",
                 "TMF", "TMV", "UVXY", "SVXY", "VXX", "UVIX", "SVIX", "BOIL",
                 "KOLD", "ERX", "ERY", "GUSH", "DRIP", "FNGU", "FNGD", "BULZ"}

# Names that move with SPY (mega-cap tech / momentum / index proxies). When the
# market itself is FOMO-extended, longs in these get size-demoted to half (they
# carry the index's mean-reversion risk); uncorrelated/defensive names don't. This
# is a heuristic proxy for CONSTITUTION rule 5's correlation classes (refine later
# with a rolling 20d vs-SPY correlation if needed).
SPY_CORRELATED = {"NVDA", "AAPL", "MSFT", "META", "GOOGL", "GOOG", "AMZN", "TSLA",
                  "AMD", "AVGO", "NFLX", "MU", "SMCI", "QQQ", "SPY", "TQQQ", "SPXL",
                  "SMH", "SOXL", "XLK",
                  "VRT", "MOD", "TT"}  # AI data-center cooling/power, high-beta to the AI trade


def _to_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def latest_brief_digest() -> dict:
    if not CACHE_DIR.exists():
        return {}
    files = sorted(CACHE_DIR.glob("morning_digest_*.json"))
    if not files:
        return {}
    try:
        return json.loads(files[-1].read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _quote(symbol: str) -> dict:
    """Fresh real-time fields via the read-only client (last, and technicals via price.py)."""
    out = {"symbol": symbol}
    try:
        q = pub.get_quote(symbol) or {}
        out["last"] = _to_float(q.get("last"))
    except Exception:
        out["last"] = None
    return out


def _price_metrics(symbol: str) -> dict:
    """price.py --json (public.com-primary): close/rsi/ma/atr/fomo_ceiling."""
    import subprocess
    try:
        r = subprocess.run([str(ROOT / "scripts" / ".venv" / "bin" / "python3"),
                            str(ROOT / "scripts" / "price.py"), symbol, "--json"],
                           capture_output=True, text=True, timeout=30)
        line = next((l for l in reversed(r.stdout.splitlines()) if l.startswith("{")), None)
        if line:
            return json.loads(line).get("data") or {}
    except Exception:
        pass
    return {}


def discovery_tickers(limit: int = 12) -> list:
    """Broad-universe DISCOVERY so the system finds its own ideas instead of only
    re-reading the (often stale) brief digest. Shells deep_scan.py (Polygon
    grouped-daily, EOD-lagged) and returns the names that SET UP: volume-confirmed
    breakouts first, then top gainers. These are NAMES to evaluate, not signals to
    act on -- each is re-priced live and run through the same per-name blockers +
    score in surface_candidates, so extended/overbought ones get filtered there."""
    import subprocess
    try:
        r = subprocess.run([str(ROOT / "scripts" / ".venv" / "bin" / "python3"),
                            str(ROOT / "scripts" / "deep_scan.py"), "--json"],
                           capture_output=True, text=True, timeout=60)
        line = next((l for l in reversed(r.stdout.splitlines()) if l.startswith("{")), None)
        if not line:
            return []
        d = json.loads(line).get("data") or {}
        out = []
        for bucket in ("bo_vol_confirm", "gainers"):  # breakouts lead, then gainers
            for it in (d.get(bucket) or []):
                t = (it.get("tk") or "").upper()
                if t and t not in out and t not in LEVERAGED_ETF:
                    out.append(t)
        return out[:limit]
    except Exception:
        return []


def review_positions(account_positions: list, hyps: dict) -> list:
    """Each held position vs its hypothesis -> suggested action for LLM re-validation."""
    reviews = []
    for pos in account_positions:
        sym = (pos.get("symbol") or (pos.get("instrument") or {}).get("symbol") or "").upper()
        if not sym:
            continue
        h = hyps.get(sym, {})
        m = _price_metrics(sym)
        last = m.get("close")
        stop = _to_float(h.get("stop"))
        target = _to_float(h.get("target"))
        entry = _to_float(h.get("entry"))
        suggestion, reasons = "HOLD", []
        if last is not None and stop is not None and last <= stop:
            suggestion = "SELL"; reasons.append(f"at/below stop {stop} (last {last})")
        elif last is not None and target is not None and last >= target:
            suggestion = "SELL_REVIEW"; reasons.append(f"at/above target {target} (last {last}) -- re-validate thesis, may raise")
        days_held = None
        if h.get("entry_date"):
            try:
                days_held = (date.today() - date.fromisoformat(h["entry_date"])).days
            except Exception:
                pass
        if suggestion == "HOLD" and days_held and days_held >= TIME_STOP_DAYS and entry and last and last < entry * 1.02:
            suggestion = "TIME_STOP_REVIEW"; reasons.append(f"{days_held}d held, no progress -- consider freeing capital")
        reviews.append({
            "ticker": sym, "last": last, "entry": entry, "stop": stop, "target": target,
            "days_held": days_held, "has_hypothesis": bool(h),
            "stop_kind": h.get("stop_kind"), "suggestion": suggestion, "reasons": reasons,
            "hypothesis": h.get("hypothesis", {}),
        })
    return reviews


def check_stop_health(equity_pos: list, hyps: dict) -> list:
    """For each held whole-share equity position carrying a resting broker stop, verify
    the stop order is still live. public.com stops are DAY orders (no GTC), so they die
    at the session close and leave the position UNPROTECTED until re-placed. Surfaces the
    gap so the skill can restore_stops() pre-market. Read-only (get_order only).
    Fractional/crypto positions use software stops checked in review_positions and are
    skipped here."""
    import order_client as oc
    out = []
    for pos in equity_pos:
        sym = (pos.get("symbol") or (pos.get("instrument") or {}).get("symbol") or "").upper()
        if not sym:
            continue
        h = hyps.get(sym, {})
        if h.get("stop_kind") != "resting_broker":
            continue
        oid = h.get("stop_order_id")
        rec = {"ticker": sym, "stop": _to_float(h.get("stop")), "qty": h.get("qty"),
               "stop_order_id": oid}
        if not oid:
            rec.update({"status": "MISSING", "working": False, "needs_restore": True,
                        "detail": "no resting stop order id on record -- position UNPROTECTED"})
            out.append(rec)
            continue
        try:
            o = oc.get_order(oid)
            status = ((o.get("status") or "UNKNOWN").upper() if isinstance(o, dict) else "UNKNOWN")
        except Exception as e:
            rec.update({"status": "ERROR", "working": False, "needs_restore": False,
                        "error": str(e), "detail": "could not fetch stop status; check manually"})
            out.append(rec)
            continue
        working = status in STOP_WORKING_STATUSES
        rec.update({"status": status, "working": working,
                    "needs_restore": (not working) and status != "FILLED",
                    "detail": ("stop live" if working else
                               "stop FILLED -- position should be exiting" if status == "FILLED"
                               else f"stop not working (status {status}) -- position UNPROTECTED, restore pre-market")})
        out.append(rec)
    return out


def restore_stops(dry_run: bool = False) -> dict:
    """Re-place any dead resting broker stops for currently-held whole-share equity
    positions, restoring protection lost when DAY stops expired at the prior close. Runs
    pre-market each session. Re-places at the position's STORED stop level (never an
    invented price) and persists the new order id. Guard-gated: order_client refuses to
    place unless armed; dry_run forces preflight-only."""
    import order_client as oc
    cfg = guards.load_config()
    offset = float(cfg["risk"].get("stop_limit_offset_pct", 0.5))
    try:
        port = pub.get_portfolio() or {}
    except Exception as e:
        return {"error": f"portfolio fetch failed: {e}", "results": []}
    pos_list = port.get("positions") or []
    hyps = positions.load()

    def _sym(p):
        return (p.get("symbol") or (p.get("instrument") or {}).get("symbol") or "").upper()

    cuniv = crypto_strategy.universe(cfg)
    held = {_sym(p) for p in pos_list}
    equity_pos = [p for p in pos_list
                  if _sym(p) not in cuniv and (hyps.get(_sym(p)) or {}).get("mechanics") != "crypto"]
    health = check_stop_health(equity_pos, hyps)
    armed = guards.is_armed(cfg)
    results = []
    for s in health:
        if not s.get("needs_restore"):
            continue
        sym = s["ticker"]
        if sym not in held:
            results.append({"ticker": sym, "action": "skip", "reason": "position no longer held"})
            continue
        h = hyps.get(sym, {})
        stop = _to_float(h.get("stop"))
        qty = h.get("qty")
        if not stop or not qty:
            results.append({"ticker": sym, "action": "skip", "reason": "no stored stop/qty"})
            continue
        limit = round(stop * (1 - offset / 100), 2)
        try:
            oc.preflight_single(sym, "SELL", "STOP_LIMIT", qty, stop_price=stop,
                                limit_price=limit, open_close="CLOSE", tif="DAY", session="CORE")
        except Exception as e:
            results.append({"ticker": sym, "action": "preflight_error", "error": str(e)})
            continue
        if dry_run or not armed:
            results.append({"ticker": sym, "action": "would_restore", "stop": stop,
                            "limit": limit, "qty": qty,
                            "reason": "dry-run" if dry_run else "disarmed"})
            continue
        try:
            oid, _ = oc.place_single(sym, "SELL", "STOP_LIMIT", qty, stop_price=stop,
                                     limit_price=limit, open_close="CLOSE", tif="DAY", session="CORE")
            positions.save_hypothesis(sym, entry=h["entry"], stop=h["stop"], target=h["target"],
                                      mechanics=h.get("mechanics", "whole"), stop_kind="resting_broker",
                                      qty=h["qty"], stop_order_id=oid, thesis=h.get("hypothesis", {}))
            oc.log_event({"action": "stop_restored", "symbol": sym, "stop": stop, "limit": limit,
                          "qty": qty, "order_id": oid, "prev_status": s.get("status")})
            results.append({"ticker": sym, "action": "restored", "stop": stop, "limit": limit,
                            "qty": qty, "order_id": oid, "prev_status": s.get("status")})
        except Exception as e:
            results.append({"ticker": sym, "action": "place_error", "error": str(e)})
    return {"ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "armed": armed, "dry_run": dry_run,
            "restored": [r for r in results if r.get("action") == "restored"],
            "results": results}


def market_fomo_gate() -> tuple[bool, str]:
    """Market-level FOMO blocker: block ALL new entries when the index itself (SPY)
    trades >2 ATR above its 20MA, regardless of any single name's setup. This is the
    macro 'no new entries while the market is FOMO-extended' rule."""
    m = _price_metrics("SPY")
    last, fomo = m.get("close"), m.get("fomo_ceiling")
    if last is not None and fomo and last > fomo:
        return True, f"market FOMO-extended: SPY {last} > 20MA+2ATR {fomo} (size-demote SPY-correlated longs)"
    return False, f"market OK: SPY {last} within ceiling {fomo}"


def surface_candidates(digest: dict, held: set, settled_cash: float,
                       market_extended: tuple = (False, "")) -> list:
    """Brief-surfaced ideas + /trader watchlist + broad scan + hard blockers + score."""
    recs = (digest.get("recommendations") or {}).get("buy") or []
    cands = digest.get("candidates") or []
    names = []
    for r in recs:
        t = (r.get("ticker") or r.get("tk") or "").upper()
        if t:
            names.append(t)
    for c in cands:
        t = (c.get("ticker") or c.get("tk") or "").upper()
        if t:
            names.append(t)
    # /trader main watchlist: the themes and setups the broader skill is tracking.
    # These may not appear in the morning brief candidates if entry isn't hit yet,
    # but the autonomous system should still price them every run for awareness.
    for t in watchlist_store.active_tickers():
        if t and t.upper() not in BANNED:  # owner trading restriction check
            names.append(t.upper())
    # Self-discovery: merge the broad-universe scan so the system isn't stuck on
    # whatever the (stale, intraday-frozen) brief digest happened to surface.
    for t in discovery_tickers():
        names.append(t)
    out = []
    seen = set()
    tparams = temperament.params()  # deterministic dial-derived params (boldness -> sizing, etc.)
    for sym in names:
        if sym in seen or sym in held:
            continue
        seen.add(sym)
        blockers = []        # HARD: data / compliance -- these veto buyability
        chase_flags = []     # DISCRETIONARY: "extended" signals the mind weighs and MAY override
        if sym in BANNED:
            blockers.append("restricted ticker (owner trading restriction)")
        m = _price_metrics(sym)
        last = m.get("close")
        rsi = m.get("rsi14")
        fomo = m.get("fomo_ceiling")
        if last is None:
            blockers.append("no price")
        # FOMO ceiling + RSI-extreme are SUGGESTIONS, not vetoes: a soaring name in a strong
        # tape can be a legit chase. They flag a name for the mind to deliberate (is momentum
        # strong? is there a catalyst? can we ride the wave?) and override with logged
        # justification -- they do NOT block buyability. The hard rails (mandatory stop,
        # position/drawdown caps, kill-switch, settlement, restricted tickers) run in guards.py
        # at order time and are NOT overridable.
        if rsi is not None and rsi > 80:
            chase_flags.append(f"RSI extreme {rsi} (chase only with justification)")
        if last is not None and fomo and last > fomo:
            chase_flags.append(f"above own FOMO ceiling {fomo} (chase only with justification)")
        strong_uptrend = bool(last and m.get("ma50") and m.get("ma200")
                              and last > m["ma50"] and last > m["ma200"])
        # Sizing is a SUGGESTION the mind can override (within the hard position cap). When SPY
        # itself is >2ATR extended: a mean-revert name (RSI<30) or a strong-uptrend LEADER rides
        # at full size; only a SPY-correlated LAGGARD (not leading) is suggested down to half.
        # Lean INTO the leaders of the move, not away from them.
        size_factor = tparams["size_aggression"]  # boldness-driven base (fraction of cap, never > 1.0)
        market_note = ""
        if market_extended[0] and not blockers:
            if rsi is not None and rsi < 30:
                market_note = "SPY FOMO-extended; name mean-revert (RSI<30) -> full size"
            elif sym in SPY_CORRELATED and not strong_uptrend:
                size_factor = round(size_factor * 0.5, 2)
                market_note = "SPY FOMO-extended + SPY-correlated laggard -> suggest half (override if it is leading)"
            elif sym in SPY_CORRELATED:
                market_note = "SPY FOMO-extended but name is a strong-uptrend leader -> full size (ride the leader)"
            else:
                market_note = "SPY FOMO-extended; name uncorrelated/defensive -> full size"
        score = 0
        if last and m.get("ma50") and last > m["ma50"]:
            score += 20
        if last and m.get("ma200") and last > m["ma200"]:
            score += 10
        if rsi is not None and 40 <= rsi <= 70:
            score += 10
        out.append({
            "ticker": sym, "last": last, "rsi14": rsi, "ma50": m.get("ma50"),
            "ma200": m.get("ma200"), "fomo_ceiling": fomo, "atr14": m.get("atr14"),
            "tech_score": score, "blockers": blockers, "chase_flags": chase_flags,
            "size_factor": size_factor, "market_note": market_note,
            "buyable": not blockers and settled_cash >= 50,
            "history_records": len(history.get(sym)),
        })
    return out


def write_snapshot(equity: float, cash: float, settled: float, positions_list: list) -> None:
    """Persist the latest account snapshot for the dashboard (no live API call there)."""
    snap = {"ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "equity": round(equity, 2), "cash": round(cash, 2),
            "settled_cash": settled, "positions": positions_list}
    (STATE_DIR / "last_snapshot.json").write_text(json.dumps(snap, indent=2))


def append_equity_history(equity: float, cash: float) -> None:
    """Append one equity point per run -> state/equity_history.jsonl. The dashboard
    renders this as the portfolio time-series (public.com has no equity-over-time
    endpoint, only transactions, so we accumulate it ourselves)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
           "equity": round(equity, 2), "cash": round(cash, 2)}
    with open(STATE_DIR / "equity_history.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def consume_approvals() -> list:
    """Apply/surface the user's Decisions-tab responses since last run (never blocks).
    Risk-loosening auto-applies via risk_state. EVERY other decided item (resource
    request like more capital or an API, an opportunity to discuss, a question) is
    surfaced in the returned list so THIS run reads the user's reasoning and acts on
    it -- e.g. approved 'add capital' -> re-evaluate sizing; approved 'use data feed
    X' -> use it. The LLM sees these in context.approvals_consumed."""
    out = []
    for it in approvals.consume_decided():
        cat = it.get("category", "") or "decision"
        reasoning = it.get("reasoning", "")
        if it["status"] == "approved" and cat == "risk_loosen" and it.get("proposal"):
            risk_state.apply_approved(it["proposal"], reasoning or it["summary"])
            change_log.record_change("risk", f"approved loosening: {it['summary']}",
                                     tags=["risk", "approved"], change=it["proposal"], rationale=reasoning)
            out.append({"applied": it["summary"], "category": cat})
        elif it["status"] == "rejected":
            change_log.record_rejection(it["summary"], tags=[cat], reason=reasoning)
            out.append({"rejected": it["summary"], "category": cat, "reasoning": reasoning})
        else:  # approved non-risk: resource / opportunity / question / discussion
            change_log.record_change(cat, f"approved: {it['summary']}",
                                     tags=[cat, "approved"], rationale=reasoning)
            out.append({"approved": it["summary"], "category": cat, "reasoning": reasoning})
    if out:
        change_log.render()
    return out


def amendment_due(not_working_active: list) -> dict:
    """Evidence + random-floor trigger. Running it does NOT mean we change anything."""
    urgent = [e for e in not_working_active if e.get("hits", 1) >= 3]
    if urgent:
        return {"due": True, "trigger": "evidence", "reason": f"recurring issue: {urgent[0]['summary']}"}
    if random.random() < AMENDMENT_RANDOM_FLOOR:
        return {"due": True, "trigger": "random", "reason": "stochastic floor (~1/50)"}
    return {"due": False, "reason": "no recurring pattern; random floor not hit"}


def _trader_watchlist_summary() -> list:
    """Active entries from the /trader main watchlist for self-audit context.

    Returns slim dicts (ticker, angle, direction, added, thesis snippet, levels)
    so the LLM can audit coverage without the full entry payload bloating context.
    """
    out = []
    for e in watchlist_store.active_entries():
        tk = (e.get("ticker") or "").upper()
        if not tk:
            continue
        levels = e.get("levels") or {}
        out.append({
            "ticker": tk,
            "angle": e.get("angle") or e.get("why") or "",
            "direction": e.get("direction", ""),
            "added": e.get("added") or e.get("added_date") or "",
            "thesis_snippet": (e.get("thesis") or "")[:120],
            "entry_trigger": levels.get("entry_trigger"),
            "stop": levels.get("stop"),
            "target": levels.get("target"),
        })
    return out


def _recent_decisions(n: int = 8) -> list:
    """Last N decision entries from trade_log.jsonl for pattern recognition."""
    log_path = STATE_DIR / "trade_log.jsonl"
    if not log_path.exists():
        return []
    try:
        lines = log_path.read_text().splitlines()
        decisions = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("action") == "decision":
                decisions.append({
                    "ts": d.get("ts", ""),
                    "symbol": d.get("symbol", ""),
                    "verb": d.get("verb", ""),
                    "plan": (d.get("plan") or "")[:150],
                })
            if len(decisions) >= n:
                break
        return list(reversed(decisions))
    except Exception:
        return []


def _read_identity() -> str:
    """The mind's self-model (state/mind/identity.md). Loaded whole each run -- it is
    the first thing the mind reads at orientation, before agenda, temperament, tape."""
    try:
        return IDENTITY_PATH.read_text()
    except OSError:
        return ""


def _directive() -> dict:
    """Free-form user directive from the dashboard Controls tab (plain instruction the
    run reads straight, e.g. 'stay defensive into CPI'). Empty until the user sets one."""
    if DIRECTIVE_PATH.exists():
        try:
            d = json.loads(DIRECTIVE_PATH.read_text())
            return {"text": (d.get("text") or "").strip(), "updated": d.get("updated", "")}
        except (json.JSONDecodeError, OSError):
            pass
    return {"text": "", "updated": ""}


def _controls_diff() -> dict:
    """What CHANGED in the dashboard controls since the previous run -- not just the
    level. Moved temperament dials + whether the free-form directive was updated, so
    the mind can react to 'the user pushed boldness up' rather than re-reading a static
    level. Advances the seen-snapshot each run."""
    cur_prof = temperament.load()
    cur_dir = _directive()
    seen = {}
    if CONTROLS_SEEN.exists():
        try:
            seen = json.loads(CONTROLS_SEEN.read_text())
        except (json.JSONDecodeError, OSError):
            seen = {}
    prev_prof = seen.get("temperament") or {}
    moved = []
    for k, v in cur_prof.items():
        pv = prev_prof.get(k)
        if pv is not None and pv != v:
            moved.append({"dial": k, "from": pv, "to": v, "delta": v - pv})
    directive_changed = bool(cur_dir.get("updated")) and \
        cur_dir.get("updated") != seen.get("directive_updated", "")
    cur_agents = {s["key"]: {"enabled": s["enabled"], "cadence": s["cadence"]}
                  for s in agent_controls.status_all()}
    prev_agents = seen.get("agents") or {}
    agents_changed = [{"agent": k, "from": prev_agents[k], "to": cur_agents[k]}
                      for k in cur_agents
                      if prev_agents.get(k) is not None and prev_agents[k] != cur_agents[k]]
    MIND_DIR.mkdir(parents=True, exist_ok=True)
    CONTROLS_SEEN.write_text(json.dumps({
        "temperament": cur_prof, "directive_updated": cur_dir.get("updated", ""),
        "agents": cur_agents,
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }, indent=2))
    return {"dials_moved": moved, "directive_changed": directive_changed,
            "directive": cur_dir.get("text", ""), "agents_changed": agents_changed}


def _regime_snapshot() -> dict:
    """Cheap index backdrop so the mind orients to the whole tape, not just its
    watchlist. SPY / QQQ / IWM: last vs 50/200MA and RSI (three price.py calls)."""
    out = {}
    for idx in ("SPY", "QQQ", "IWM"):
        m = _price_metrics(idx)
        last = m.get("close")
        out[idx] = {
            "last": last, "rsi14": m.get("rsi14"),
            "above_ma50": (last > m["ma50"]) if last is not None and m.get("ma50") else None,
            "above_ma200": (last > m["ma200"]) if last is not None and m.get("ma200") else None,
        }
    return out


def _user_model() -> dict:
    """The mind's evolving model of its owner (state/mind/user_profile.json), read every run
    so the mind steers by who its owner is, reads terse instructions in context, and knows where
    it may challenge and what it must never challenge. Guarded: missing/bad file returns {}."""
    try:
        return json.loads((MIND_DIR / "user_profile.json").read_text())
    except Exception:
        return {}


def _subagents_roster() -> dict:
    """Compact view of the subagents registry so the mind knows which specialists it can spin
    up on demand (full specs live in subagents/<name>.md). Guarded."""
    try:
        reg = json.loads((DIR / "subagents" / "registry.json").read_text())
    except Exception:
        return {}
    return {
        "on_demand": [{"name": a.get("name"), "role": a.get("role"),
                       "status": a.get("status"), "invoke_when": a.get("invoke_when")}
                      for a in reg.get("on_demand", []) if a.get("status") != "retired"],
        "every_run": [a.get("name") for a in reg.get("every_run", [])],
        "debate_panel": [a.get("name") for a in reg.get("debate_panel", [])],
    }


def _mind_state() -> dict:
    """The current mind the LLM orients on BEFORE the tape: self-model, the mind's model of its
    owner, the hot memory index (cards only -- bodies pulled on demand via memory.py during the
    run), per-bucket counts for the self-audit, the open agenda including held tensions, and the
    on-demand subagent roster."""
    open_items = agenda.open_items()
    return {
        "identity": _read_identity(),
        "user_model": _user_model(),
        "memory_index": memory.index(),
        "memory_counts": memory.counts(),
        "agenda_open": open_items,
        "open_tensions": [it for it in open_items if it.get("type") == "tension"],
        "subagents": _subagents_roster(),
    }


def build_context() -> dict:
    cfg = guards.load_config()
    now = datetime.now(timezone.utc).astimezone()
    market_open, market_msg = guards.is_market_open(cfg=cfg)
    crypto_open, crypto_msg = guards.is_crypto_tradeable(cfg)
    armed = guards.is_armed(cfg)
    tparams = temperament.params()  # deterministic dial-derived params (curiosity -> discovery, etc.)

    ctx = {
        "ts": now.isoformat(timespec="seconds"),
        "armed": armed,
        "market_open": market_open,
        "market": market_msg,
        "crypto_open": crypto_open,
        "idea_generation_due": ((not market_open) and tparams["discovery_breadth"] != "lean"),
        "discovery_breadth": tparams["discovery_breadth"],
        "idea_generation_note": (("Equities closed: off-hours is when the Ideas subagent should refresh names and the watchlist. Convene Ideas this run unless you did so very recently." if tparams["discovery_breadth"] != "lean" else "Equities closed, but curiosity dial is low (lean breadth): skip the off-hours idea hunt; run only the proven playbook.") if not market_open else "Market open: focus on live candidates and position management; idea-generation is not the priority."),
        "crypto": crypto_msg,
    }
    # The run does work when EITHER market is open. Crypto is 24/7, so off equity
    # hours we still run the crypto branch instead of returning NO_ACTION.
    if not market_open and not crypto_open:
        ctx["action"] = "NO_ACTION (markets closed)"
        return ctx

    # Account (portfolio state is available 24/7, independent of the equity session)
    try:
        port = pub.get_portfolio()
    except Exception as e:
        ctx["error"] = f"portfolio fetch failed: {e}"
        return ctx
    cash = _to_float((port.get("buyingPower") or {}).get("cashOnlyBuyingPower"), 0.0)
    equity_items = port.get("equity") or []
    equity = sum(_to_float(e.get("value"), 0.0) for e in equity_items) or cash
    pos_list = port.get("positions") or []

    settlement.seed_if_needed(cash)
    settled = settlement.available_settled(cash)
    write_snapshot(equity, cash, settled, pos_list)        # for the dashboard
    append_equity_history(equity, cash)                     # portfolio time-series
    consumed = consume_approvals()                          # apply user's dashboard decisions

    kill, kill_msg = guards.kill_switch_tripped(equity, cfg)
    hyps = positions.load()
    not_working = reflections.active("not_working")

    def _sym(p):
        return (p.get("symbol") or (p.get("instrument") or {}).get("symbol") or "").upper()
    held = {_sym(p) for p in pos_list}
    # Split the book by asset class: crypto if the symbol is in the crypto universe
    # or its stored hypothesis was opened as crypto; everything else is equity.
    cuniv = crypto_strategy.universe(cfg)
    crypto_syms = {_sym(p) for p in pos_list
                   if _sym(p) in cuniv or (hyps.get(_sym(p)) or {}).get("mechanics") == "crypto"}
    crypto_pos = [p for p in pos_list if _sym(p) in crypto_syms]
    equity_pos = [p for p in pos_list if _sym(p) not in crypto_syms]

    ctx.update({
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "settled_cash_available": settled,
        "kill_switch": {"tripped": kill, "detail": kill_msg},
        "open_positions": len(pos_list),
        "caps": cfg["risk"],
        "approvals_consumed": consumed,
        "approvals_to_implement": [
            {"id": it["id"], "category": it.get("category"), "summary": it["summary"],
             "detail": it.get("detail", ""), "proposal": it.get("proposal") or {},
             "reasoning": it.get("reasoning", ""), "approved_ts": it.get("decided_ts")}
            for it in approvals.pending_implementation()],
        "instructions_pending": [
            {"id": i["id"], "ts": i["ts"], "text": i["text"], "tab": i.get("tab", "Other"),
             "images": i.get("images", [])}
            for i in instructions.pending()],
        "amendment_analysis": amendment_due(not_working),
        "whats_not_working_active": [
            {"id": e["id"], "summary": e["summary"], "hits": e.get("hits", 1)} for e in not_working],
        "trader_watchlist": _trader_watchlist_summary(),
        "recent_decisions": _recent_decisions(n=8),
        "mind": _mind_state(),
        "controls_diff": _controls_diff(),
        "agent_controls": agent_controls.status_all(),
        "agent_due": agent_controls.due_set(),
        "install_queue": agent_controls.install_queue(),
    })

    # Resting-stop health -- public.com stops are DAY orders (no GTC) that die at the
    # close, so a held whole-share position can sit UNPROTECTED between sessions until
    # the stop is re-placed. Surface this EVERY run (off-hours included); the skill calls
    # `restore-stops` pre-market to heal anything flagged. Read-only here.
    stop_health = check_stop_health(equity_pos, hyps)
    ctx["stop_health"] = stop_health
    ctx["stops_need_restore"] = [s["ticker"] for s in stop_health if s.get("needs_restore")]

    # EQUITY branch -- only when the stock market is open (no equity fills off-hours;
    # held equities are protected between runs by resting broker stops / next-open review).
    if market_open:
        ctx["positions_review"] = review_positions(equity_pos, hyps)
        ctx["regime"] = _regime_snapshot()
        digest = latest_brief_digest()
        ctx["brief_digest_present"] = bool(digest)
        if kill:
            ctx["candidates"] = []
            ctx["note"] = "kill-switch tripped: manage/sell only, no new buys"
        else:
            mext = market_fomo_gate()
            ctx["market_gate"] = {
                "spy_fomo_extended": mext[0],
                "policy": "size-demote SPY-correlated longs to half (not a block); per-name FOMO still blocks names above their own ceiling",
                "detail": mext[1],
            }
            ctx["candidates"] = surface_candidates(digest, held, settled, market_extended=mext)
    else:
        ctx["equity_session"] = "equities closed -- skipped this run; crypto branch only"

    # Watchlist snapshot -- rebuilt EVERY run (open, closed, or crypto-only) so the
    # dashboard's Watching tab always mirrors the live autonomous_watchlist instead of
    # freezing at the last open-market run. Trigger entries get a fresh price + ready
    # check; monitor entries (no level) just carry their thesis. watching.json is the
    # dashboard's read-only render of this.
    watching = []
    for w in autonomous_watchlist.load():
        wlast = _price_metrics(w["ticker"]).get("close")
        watching.append({**w, "last": wlast,
                         "ready": autonomous_watchlist.is_ready(w, wlast),
                         "already_held": w["ticker"] in held})
    ctx["watching"] = watching
    (STATE_DIR / "watching.json").write_text(
        json.dumps({"updated": ctx["ts"], "watching": watching}, indent=2))  # for the dashboard

    # Advisory signal inbox -- tickers the mind's signal subagents surfaced since the last
    # run (step 2b drains these into the candidate universe). ADVISORY ONLY: never auto-adds.
    # A bad/missing inbox must not break the run.
    try:
        ctx["agent_signals"] = agent_signals.pending()
    except Exception:
        ctx["agent_signals"] = []

    # Mark shadow trades to current prices so the dashboard shows live P&L.
    try:
        st_open = shadow_trades.summary()["open"]
        st_tickers = {t["ticker"] for t in st_open}
        st_prices = {}
        for tkr in st_tickers:
            p = _price_metrics(tkr).get("close")
            if p:
                st_prices[tkr] = float(p)
        if st_prices:
            shadow_trades.mark(st_prices)
    except Exception:
        pass

    # CRYPTO branch -- 24/7. Review held crypto vs its SOFTWARE stop/target (no resting
    # broker stop exists for crypto), then surface fresh crypto candidates per the
    # codified ruleset in crypto_strategy (kill-switch + capacity gated).
    if crypto_open:
        ctx["crypto_positions_review"] = [
            crypto_strategy.review_position(_sym(p), hyps.get(_sym(p)) or {}, cfg) for p in crypto_pos]
        ccap_ok, ccap_msg = guards.crypto_capacity_ok(len(crypto_pos), cfg)
        # Total-crypto-exposure cap (crypto.max_portfolio_pct of equity): cap each
        # candidate's surfaced size to the remaining budget so the size handed to the
        # mind already honors the directive; an exhausted budget blocks new buys. This
        # binds upstream alongside the count cap, matching how capacity is enforced.
        crypto_exposure = sum(_to_float(p.get("currentValue"), 0.0) for p in crypto_pos)
        cbudget = guards.crypto_budget_remaining(crypto_exposure, equity, cfg)
        cmin = float((cfg.get("crypto") or {}).get("min_position_usd", 25))
        budget_ok = cbudget >= cmin
        if kill:
            ctx["crypto_candidates"] = []
            ctx["crypto_note"] = "kill-switch tripped: manage/sell crypto only, no new buys"
        else:
            cands = crypto_strategy.evaluate_universe(held, cfg)
            for c in cands:
                if cbudget != float("inf") and c.get("size_usd") is not None \
                        and float(c["size_usd"]) > cbudget:
                    c["size_usd"] = round(cbudget, 2)
                    c["size_capped_by"] = "crypto_portfolio_pct"
                if not budget_ok:
                    c.setdefault("blockers", []).append(
                        f"crypto portfolio cap reached (budget ${cbudget:.2f} < min ${cmin:.0f})")
                    c["buyable"] = False
            ctx["crypto_candidates"] = cands
        ctx["crypto_capacity"] = {
            "open": len(crypto_pos), "ok_to_add": bool(ccap_ok and budget_ok), "detail": ccap_msg,
            "exposure_usd": round(crypto_exposure, 2),
            "budget_remaining_usd": (None if cbudget == float("inf") else round(cbudget, 2)),
            "max_portfolio_pct": (cfg.get("crypto") or {}).get("max_portfolio_pct")}

    ctx["temperament"] = temperament.context_block()
    # Auto-stamp the deterministic every-run agents now that this is a substantive run; the
    # mind stamps the on-demand/event agents it convenes itself (see SKILL.md "Agent controls").
    try:
        agent_controls.mark_cadence_run()
    except Exception:
        pass
    ctx["action"] = "EVALUATE"
    return ctx


def _equity_positions_and_hyps():
    """Shared helper for the stop-health CLI: fresh held equity positions + hypotheses."""
    cfg = guards.load_config()
    port = pub.get_portfolio() or {}
    pos_list = port.get("positions") or []
    hyps = positions.load()
    cuniv = crypto_strategy.universe(cfg)

    def _sym(p):
        return (p.get("symbol") or (p.get("instrument") or {}).get("symbol") or "").upper()

    equity_pos = [p for p in pos_list
                  if _sym(p) not in cuniv and (hyps.get(_sym(p)) or {}).get("mechanics") != "crypto"]
    return equity_pos, hyps


def main() -> int:
    ap = argparse.ArgumentParser(description="Autonomous trader entry (context builder).")
    ap.add_argument("cmd", nargs="?", default="context",
                    choices=["context", "stop-health", "restore-stops"])
    ap.add_argument("--dry-run", action="store_true",
                    help="restore-stops: preflight only, place nothing")
    args = ap.parse_args()
    if args.cmd == "context":
        print(json.dumps(build_context(), indent=2))
    elif args.cmd == "stop-health":
        equity_pos, hyps = _equity_positions_and_hyps()
        print(json.dumps(check_stop_health(equity_pos, hyps), indent=2))
    elif args.cmd == "restore-stops":
        print(json.dumps(restore_stops(dry_run=args.dry_run), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
