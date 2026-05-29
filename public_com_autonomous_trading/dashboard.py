#!/usr/bin/env python3
"""v2 dashboard - server-rendered stdlib Python, light design from AItrader.html,
port 8787. Run: dashboard_v2.py serve [--port 8787]

The canonical dashboard. Architecture: stdlib HTTP server, all data read fresh
from state/*.json each request, painted in the warm-off-white "AItrader" design
system: a sticky glance header, a tab strip, and one content pane per tab. Light
theme only. Self-contained -- the data-layer helpers it needs are inlined below
rather than imported from another module.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

DIR = Path(__file__).resolve().parent
STATE = DIR / "state"
sys.path.insert(0, str(DIR))

# Sibling modules (same dir, on sys.path). The panes below read from these.
# Kept as plain imports so a missing one fails loudly during development rather
# than silently degrading.
import approvals  # noqa: E402,F401
import temperament  # noqa: E402,F401
import agent_controls  # noqa: E402  (per-agent enable/disable + cadence + install queue)
import instructions  # noqa: E402,F401
import memory  # noqa: E402,F401
import change_log  # noqa: E402,F401
import reflections  # noqa: E402,F401
import autonomous_watchlist  # noqa: E402,F401  (the trader's own watchlist: add/list API)

# settlement computes spendable settled cash on the live-portfolio path. Guarded
# so the dashboard still starts if it fails to import.
try:
    import settlement  # noqa: E402,F401
except Exception:
    settlement = None

# shared_metadata backs the Watch tab's watchlist-sharing (clone of the private
# AiTrader-sharedMetadata repo, scrub + push, browse). Guarded so the dashboard
# still starts if git/gh is unavailable; the Watch tab then just hides sharing.
try:
    import shared_metadata  # noqa: E402,F401
except Exception:
    shared_metadata = None

# The read-only public.com client lives in the sibling scripts/ dir. Guarded so
# the dashboard still starts (and panes can fall back to snapshots) if importing
# it fails (missing deps, path, etc.).
sys.path.insert(0, str(DIR.parent / "scripts"))
try:
    import publicdotcom_api as pub  # noqa: E402,F401
except Exception:
    pub = None


# --- data layer (inlined; formerly imported from the v1 dashboard) ---
# Generic state-reading and formatting helpers. They read from this module's own
# STATE/DIR and stdlib imports above, so the dashboard is self-contained.

PAGE_SIZE = 25


def _read_json(name, default):
    p = STATE / name
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return default


def _fmt_ts(ts: str) -> str:
    """ISO timestamp -> compact 'YYYY-MM-DD HH:MM' (drop seconds + tz offset)."""
    if not ts:
        return ""
    s = str(ts).replace("T", " ")
    return s[:16]


def _trade_status(e: dict) -> str:
    """Status label for a trade row. Prefer the top-level 'status' field; else
    derive a sensible fallback from the order side / open-close indicator."""
    st = str(e.get("status") or "").strip()
    if st:
        return st.upper()
    req = e.get("request") or {}
    side = str(req.get("orderSide") or "").upper()
    if side in ("BUY", "SELL"):
        return side
    return str(e.get("action", "") or "").upper()


def _status_cls(status: str) -> str:
    """Map an order status to a status-pill color variant class."""
    s = (status or "").upper()
    if s in ("FILLED", "BOUGHT", "BUY"):
        return "pill-st-green"
    if s in ("SUBMITTED", "PENDING", "ORDER", "OPEN", "ACCEPTED"):
        return "pill-st-blue"
    if s in ("SOLD", "SELL", "PARTIAL", "CANCELLED", "CANCELED", "EXPIRED"):
        return "pill-st-amber"
    if s in ("REJECTED", "FAILED", "ERROR"):
        return "pill-st-red"
    return ""


def activity_events() -> list:
    """Unified, newest-first timeline. One line per item.

    Each event is (ts, kind, text, symbol, status, detail):
      - symbol is "" unless a structured ticker is available (trade rows),
      - status is "" unless the row carries an order status (trade rows),
      - detail is a dict with the full info, pretty-printed in the popup.
    """
    ev = []
    cl = _read_json("change_log.json", {"changes": [], "rejected": []})
    for c in cl.get("changes", []):
        detail = {
            "area": c.get("area", ""),
            "what": c.get("what", ""),
            "rationale": c.get("rationale", ""),
            "for": c.get("for", ""),
            "against": c.get("against", ""),
            "change": c.get("change", {}),
            "tags": c.get("tags", []),
            "status": c.get("status", ""),
        }
        ev.append((c["ts"], "change", f"Amendment [{c.get('area','')}]: {c.get('what','')}", "", "", detail))
    for r in cl.get("rejected", []):
        detail = {
            "thesis": r.get("thesis", ""),
            "reason": r.get("reason", ""),
            "for": r.get("for", ""),
            "against": r.get("against", ""),
            "tags": r.get("tags", []),
        }
        ev.append((r["ts"], "rejected", f"Rejected: {r.get('thesis','')} -- {r.get('reason','')}", "", "", detail))
    for a in _read_json("approvals.json", []):
        if a.get("decided_ts"):
            detail = {
                "status": a.get("status", ""),
                "category": a.get("category", ""),
                "summary": a.get("summary", ""),
                "detail": a.get("detail", ""),
                "reasoning": a.get("reasoning", ""),
                "proposal": a.get("proposal", {}),
            }
            ev.append((a["decided_ts"], "approval", f"Approval {a['status']}: {a.get('summary','')}", "", "", detail))
    rf = _read_json("reflections.json", {"working": [], "not_working": []})
    for k, label in (("working", "working"), ("not_working", "not-working")):
        for e in rf.get(k, []):
            detail = {
                "flag": label,
                "summary": e.get("summary", ""),
                "detail": e.get("detail", ""),
                "tags": e.get("tags", []),
                "hits": e.get("hits", 0),
                "status": e.get("status", ""),
            }
            ev.append((e.get("updated", e.get("created", "")), "reflect",
                       f"Flagged {label}: {e.get('summary','')}", "", "", detail))
    log = STATE / "trade_log.jsonl"
    if log.exists():
        for line in log.read_text().splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            act = e.get("action", "")
            sym = str(e.get("symbol", "") or "")
            txt = e.get("plan") or f"{act} {sym} {e.get('order_id','')}".strip()
            status = _trade_status(e)
            detail = {
                "action": act,
                "status": e.get("status", ""),
                "plan": e.get("plan", ""),
                "order_id": e.get("order_id", ""),
                "symbol": sym,
                "request": e.get("request", {}),
                "response": e.get("response", {}),
            }
            ev.append((e.get("ts", ""), "trade", f"{act}: {txt}", sym, status, detail))
    ev.sort(key=lambda x: x[0], reverse=True)
    return ev


# Days back for the fixed-length home timeframe windows used by _filter_window.
_WIN_DAYS = {"1w": 7, "1m": 30, "3m": 90}


def _parse_ts(ts: str):
    """Parse an ISO-8601 timestamp (with or without offset) to an aware UTC
    datetime. Returns None on failure."""
    if not ts:
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s[:19])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _equity_history() -> list:
    """Read state/equity_history.jsonl into a list of (datetime, equity, cash),
    oldest-first, skipping unparseable lines. Empty list if file is absent."""
    p = STATE / "equity_history.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        dt = _parse_ts(e.get("ts", ""))
        eq = _num(e.get("equity"))
        if dt is None or eq is None:
            continue
        out.append((dt, eq, _num(e.get("cash"))))
    out.sort(key=lambda x: x[0])
    return out


def _filter_window(pts: list, win: str) -> list:
    """Filter (dt, equity, cash) points to the selected window, measured
    relative to the most recent point in the data."""
    if not pts or win == "all":
        return pts
    last_dt = pts[-1][0]
    if win == "today":
        cutoff_date = last_dt.date()
        return [p for p in pts if p[0].date() == cutoff_date]
    if win == "ytd":
        return [p for p in pts if p[0].year == last_dt.year]
    days = _WIN_DAYS.get(win)
    if days is None:
        return pts
    from datetime import timedelta
    cutoff = last_dt - timedelta(days=days)
    return [p for p in pts if p[0] >= cutoff]


def _num(v, default=None):
    """Best-effort float parse of a possibly-string/None value."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# The AItrader design system, lifted from the embedded template in
# AItrader.html: warm off-white surfaces, signal-green accent, Manrope + JetBrains
# Mono. Light theme only. Includes the [data-accent] and [data-density] variant
# blocks so accent/density toggles work once the panes are built.
_CSS_V2 = """@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=JetBrains+Mono:wght@300;400;500&display=swap');
:root{
  --bg:#f5f4ef;--surface:#ffffff;--bg-soft:#eeece6;--bg-deep:#e3e1d8;
  --ink-1:#0a0a0c;--ink-2:#5a594f;--ink-3:#908c80;--ink-4:#b6b1a3;
  --rule:#e4e0d3;--rule-2:#cdc8b8;
  --accent:#10C24A;--accent-deep:#0AA23C;--accent-soft:rgba(16,194,74,0.10);--accent-line:rgba(16,194,74,0.30);
  --neg:#d83a1c;--neg-soft:rgba(216,58,28,0.08);--pos:#0AA23C;--info:#1f5fc4;
  --sans:"Manrope",system-ui,sans-serif;--mono:"JetBrains Mono",ui-monospace,monospace;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--ink-1);font-family:var(--sans);font-size:14px;line-height:1.5;
  -webkit-font-smoothing:antialiased;letter-spacing:-0.005em}
.wrap{max-width:1280px;margin:0 auto;padding:0 56px}
.wrap--narrow{max-width:920px}

/* The ONLY thing that pulses */
.live{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--accent);
  box-shadow:0 0 6px var(--accent);animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.35}}

/* HEADER (always-on glance + tabs) */
header.chrome{background:var(--bg);position:sticky;top:0;z-index:30}
.chrome-top{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;padding:22px 0 16px;gap:32px}
.brand{display:flex;align-items:baseline;gap:14px;font-family:var(--sans);font-size:22px;font-weight:800;letter-spacing:-0.035em}
.brand .sess{font-family:var(--mono);font-weight:400;font-size:11px;color:var(--ink-3);letter-spacing:0.04em}
.state-pill{justify-self:center;display:inline-flex;align-items:center;gap:12px;padding:7px 18px;background:var(--surface);
  border-radius:999px;box-shadow:0 1px 2px rgba(10,10,12,0.04);font-size:13px;color:var(--ink-2)}
.state-pill b{color:var(--ink-1);font-weight:600}
.state-pill .div{width:3px;height:3px;border-radius:50%;background:var(--ink-4)}
.head-right{display:flex;justify-content:flex-end;align-items:center;gap:18px}
.nav-mini{text-align:right;font-family:var(--mono);font-size:11px;color:var(--ink-3);letter-spacing:0.04em;line-height:1.2}
.nav-mini b{display:block;font-family:var(--sans);font-size:18px;font-weight:700;letter-spacing:-0.025em;color:var(--ink-1);margin-bottom:2px}
.nav-mini .neg-val{color:var(--neg);font-weight:600}
.ctrl-btn{font-family:var(--sans);font-weight:600;font-size:13px;color:var(--ink-1);background:var(--surface);border:0;
  padding:8px 16px;border-radius:999px;cursor:pointer;text-decoration:none;box-shadow:0 1px 2px rgba(10,10,12,0.04);transition:box-shadow .2s}
.ctrl-btn:hover{box-shadow:0 2px 8px rgba(10,10,12,0.08)}
.instruct-btn{display:inline-flex;align-items:center;gap:7px;font-family:var(--sans);font-weight:600;font-size:13px;
  color:var(--ink-1);background:transparent;border:0;padding:6px 12px;border-radius:999px;cursor:pointer;
  letter-spacing:-0.005em;transition:background .2s,box-shadow .2s}
.instruct-btn:hover{background:var(--surface);box-shadow:0 1px 2px rgba(10,10,12,0.06)}
.instruct-badge{display:inline-block;min-width:18px;height:18px;line-height:18px;padding:0 5px;border-radius:10px;
  background:var(--neg);color:#fff;font-family:var(--mono);font-size:11px;font-weight:600;text-align:center}

/* TABS (server-rendered as links; active tab carries data-active) */
.tab-strip{position:relative;display:flex;gap:0}
.tab-strip a{background:transparent;border:0;padding:14px 22px 18px;font-family:var(--sans);font-weight:600;font-size:14px;
  color:var(--ink-3);cursor:pointer;position:relative;transition:color .2s;display:inline-flex;align-items:center;gap:8px;
  letter-spacing:-0.005em;text-decoration:none}
.tab-strip a:first-child{padding-left:0}
.tab-strip a:hover{color:var(--ink-1)}
.tab-strip a[data-active="true"]{color:var(--ink-1)}
.tab-strip a .ct{font-family:var(--mono);font-size:10.5px;padding:1px 6px;background:var(--bg-soft);border-radius:4px;
  color:var(--ink-2);font-weight:500;letter-spacing:0}
.tab-strip a[data-active="true"] .ct{background:var(--accent-soft);color:var(--accent-deep)}
.tab-strip a .attn{width:6px;height:6px;border-radius:50%;background:var(--accent);box-shadow:0 0 5px var(--accent)}
.tab-strip a[data-active="true"]::after{content:"";position:absolute;bottom:0;left:22px;right:22px;height:3px;
  background:var(--accent);border-radius:999px 999px 0 0}
.tab-strip a[data-active="true"]:first-child::after{left:0}
.chrome::after{content:"";display:block;height:1px;background:var(--rule)}

/* PANE SCAFFOLD (heading + placeholder surfaces; panes fill in later chunks) */
.pane-head{padding:56px 0 36px}
.pane-head .eyebrow{font-family:var(--mono);font-size:11.5px;letter-spacing:0.18em;text-transform:uppercase;color:var(--ink-3);margin-bottom:14px}
.pane-head h1{font-family:var(--sans);font-size:48px;font-weight:700;letter-spacing:-0.04em;line-height:1.05;color:var(--ink-1);margin:0}
.pane-head h1 .num{color:var(--accent-deep);font-family:var(--mono);font-weight:600;font-size:32px;letter-spacing:-0.01em}
.pane-head .sub{font-size:16px;color:var(--ink-2);margin-top:14px;max-width:580px}
.panel{background:var(--surface);border-radius:14px;padding:22px 24px}
.panel-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;padding-bottom:12px;border-bottom:1px solid var(--rule)}
.panel-head .l{font-family:var(--sans);font-weight:700;font-size:13px;color:var(--ink-1);letter-spacing:-0.01em;display:inline-flex;align-items:center;gap:8px}
.card{background:var(--surface);border-radius:14px;padding:22px 28px}
.card h2{font-family:var(--sans);font-size:22px;font-weight:700;letter-spacing:-0.02em;margin:0 0 8px;color:var(--ink-1)}
.muted{color:var(--ink-3)}
main{padding-bottom:80px}

/* DENSITY VARIANTS */
[data-density="compact"] .home-hero{padding:36px 0 16px}
[data-density="compact"] .pane-head{padding:36px 0 24px}
[data-density="compact"] .pane-head h1{font-size:40px}
[data-density="compact"] .pane-head .sub{font-size:15px;margin-top:10px}
[data-density="compact"] .panel{padding:18px 20px}
[data-density="compact"] .card{padding:18px 22px}

[data-density="dense"] .home-hero{padding:24px 0 10px}
[data-density="dense"] .pane-head{padding:24px 0 18px}
[data-density="dense"] .pane-head h1{font-size:32px}
[data-density="dense"] .pane-head .sub{font-size:14px;margin-top:8px}
[data-density="dense"] .panel{padding:14px 16px}
[data-density="dense"] .card{padding:14px 18px}

/* ACCENT VARIANTS */
[data-accent="cobalt"]{--accent:#2747e8;--accent-deep:#1d36b8;--accent-soft:rgba(39,71,232,0.10);--accent-line:rgba(39,71,232,0.30);--pos:#1d36b8}
[data-accent="indigo"]{--accent:#6e3d96;--accent-deep:#573074;--accent-soft:rgba(110,61,150,0.10);--accent-line:rgba(110,61,150,0.30);--pos:#573074}
[data-accent="copper"]{--accent:#d97706;--accent-deep:#a55d04;--accent-soft:rgba(217,119,6,0.10);--accent-line:rgba(217,119,6,0.30);--pos:#a55d04}
[data-accent="ink"]{--accent:#1a1a1a;--accent-deep:#000000;--accent-soft:rgba(0,0,0,0.06);--accent-line:rgba(0,0,0,0.18);--pos:#1a1a1a}

/* HOME pane: hero NAV, equity chart, position list. Lifted from the AItrader
   design's home section. Window controls render as links (server nav), so the
   .tf-pills children are <a> here rather than the design's <button>. */
.home-hero{padding:56px 0 24px}
.home-hero .label{font-family:var(--sans);font-weight:600;font-size:13px;color:var(--ink-2);margin-bottom:16px;letter-spacing:-0.005em;display:inline-flex;align-items:center;gap:10px}
.home-hero .nav-row{display:flex;align-items:flex-end;justify-content:space-between;gap:32px;flex-wrap:wrap}
.home-hero .nav-figure{display:flex;align-items:baseline;gap:22px}
.home-hero .nav-figure .num{font-family:var(--sans);font-weight:800;font-size:84px;line-height:0.92;letter-spacing:-0.045em;color:var(--ink-1)}
.home-hero .nav-figure .delta{display:inline-flex;align-items:center;gap:10px;font-family:var(--sans);font-weight:700;font-size:18px;margin-bottom:12px}
.home-hero .nav-figure .delta .pill{padding:4px 12px;border-radius:8px;font-weight:700}
.home-hero .nav-figure .delta.pos{color:var(--pos)}
.home-hero .nav-figure .delta.pos .pill{background:var(--accent-soft);color:var(--accent-deep)}
.home-hero .nav-figure .delta.neg{color:var(--neg)}
.home-hero .nav-figure .delta.neg .pill{background:var(--neg-soft);color:var(--neg)}
.alloc{margin-top:14px}
.alloc-bar{display:flex;height:8px;border-radius:999px;overflow:hidden;background:var(--bg-deep)}
.alloc-seg{height:100%}
.alloc-seg.invested{background:var(--accent)}
.alloc-seg.cash{background:var(--ink-4)}
.alloc-cap{font-size:12.5px;color:var(--ink-2);margin-top:7px}
.alloc-cap b{color:var(--ink-1);font-weight:700}
.alloc-cap .sep{margin:0 8px;color:var(--ink-4)}
.home-hero .meta{font-size:13px;color:var(--ink-2);margin-top:12px;line-height:1.6}
.home-hero .meta b{color:var(--ink-1);font-weight:700}
.home-hero .meta .sep{margin:0 10px;color:var(--ink-4)}
.home-hero .src{display:inline-flex;align-items:center;gap:7px;font-family:var(--mono);font-size:11px;letter-spacing:0.04em;color:var(--ink-3);padding:3px 11px;background:var(--bg-soft);border-radius:999px}
.home-hero .src.src-live{color:var(--accent-deep);background:var(--accent-soft)}
.tf-pills{display:flex;gap:6px}
.tf-pills a{font-family:var(--sans);font-weight:600;font-size:13px;padding:8px 16px;border-radius:999px;color:var(--ink-3);cursor:pointer;text-decoration:none;transition:background .15s,color .15s}
.tf-pills a:hover{background:var(--bg-soft);color:var(--ink-1)}
.tf-pills a.on{color:var(--accent-deep);background:var(--accent-soft)}
.chart-frame{padding:16px 0 32px;height:360px;position:relative}
.chart-frame .yaxis{position:absolute;left:0;right:64px;top:16px;bottom:40px;pointer-events:none}
.chart-frame .yaxis .g{position:absolute;left:0;right:0;border-top:1px dashed var(--rule)}
.chart-frame .yaxis .g span{position:absolute;right:-58px;top:-8px;font-family:var(--mono);font-size:11px;color:var(--ink-3);width:52px;text-align:right}
.chart-frame svg.line{position:absolute;left:0;right:64px;top:16px;bottom:40px;width:calc(100% - 64px);height:calc(100% - 56px)}
.chart-frame .cap{position:absolute;left:0;bottom:8px;font-family:var(--sans);font-weight:600;font-size:13px;color:var(--ink-2)}
.chart-frame .cap b{font-weight:700}
.chart-frame .cx-layer{position:absolute;left:0;right:64px;top:16px;bottom:40px;pointer-events:none}
.cx-line{position:absolute;top:0;bottom:0;width:1px;background:var(--ink-4);opacity:.5;display:none;pointer-events:none}
.cx-dot{position:absolute;width:9px;height:9px;border-radius:50%;background:var(--accent);border:2px solid white;transform:translate(-50%,-50%);display:none;pointer-events:none}
.cx-tip{position:absolute;background:var(--surface);border:1px solid var(--rule);border-radius:6px;padding:4px 8px;font-family:var(--mono);font-size:11px;color:var(--ink-1);box-shadow:0 2px 8px rgba(0,0,0,.12);transform:translate(-50%,-130%);white-space:nowrap;display:none;pointer-events:none;z-index:5}
.chart-empty{color:var(--ink-3);padding:24px 0 8px;font-size:14px}
.pos-list{padding-bottom:80px}
.pos-list .row{display:grid;grid-template-columns:60px 1.3fr 100px 1fr 120px 110px 100px;align-items:center;gap:16px;padding:20px 0;border-bottom:1px solid var(--rule);transition:background .15s}
.pos-list .row:last-child{border-bottom:0}
.pos-list .row .avatar{width:44px;height:44px;border-radius:50%;display:grid;place-items:center;color:white;font-family:var(--sans);font-weight:700;font-size:13px;letter-spacing:-0.01em;background:linear-gradient(135deg,var(--accent),var(--accent-deep))}
.pos-list .row .nm{font-family:var(--sans);font-weight:700;font-size:17px;letter-spacing:-0.015em}
.pos-list .row .nm small{display:block;font-weight:500;font-size:12px;color:var(--ink-3);letter-spacing:0;margin-top:2px}
.pos-list .row .qty{font-family:var(--mono);font-size:12px;color:var(--ink-2)}
.pos-list .row .qty b{color:var(--ink-1);font-weight:600}
.pos-list .row .spark{width:100%;height:36px;display:block}
.pos-list .row .last{font-family:var(--sans);font-weight:700;font-size:18px;text-align:right;letter-spacing:-0.015em}
.pos-list .row .pct{font-family:var(--sans);font-weight:700;font-size:14px;text-align:right}
.pos-list .row .pct small{display:block;font-weight:500;font-size:11.5px;color:var(--ink-3);margin-top:2px}
.pos-list .row .pct.neg{color:var(--neg)}
.pos-list .row .pct.pos{color:var(--pos)}
.pos-list .row .wt{text-align:right;font-family:var(--mono);font-size:14px;color:var(--ink-1);font-weight:600}
.pos-list .row .wt small{display:block;font-weight:400;font-size:10.5px;color:var(--ink-3);margin-top:4px}
.pos-list .row.cash .avatar{background:var(--bg-deep);color:var(--ink-2)}
.pos-list .row.cash .last,.pos-list .row.cash .nm{color:var(--ink-2)}
.pos-list .row .exit-plan{grid-column:1/-1;margin-top:12px;padding:9px 12px;background:var(--bg-deep);border-radius:9px;font-family:var(--mono);font-size:11.5px;color:var(--ink-2);display:flex;flex-wrap:wrap;gap:6px 18px;align-items:center;line-height:1.55}
.pos-list .row .exit-plan .lbl{color:var(--ink-3)}
.pos-list .row .exit-plan b{color:var(--ink-1);font-weight:700}
.pos-list .row .exit-plan .badge{font-family:var(--sans);font-size:10px;font-weight:700;padding:1px 8px;border-radius:99px;letter-spacing:.01em}
.pos-list .row .exit-plan .badge.ok{background:rgba(22,163,74,.13);color:var(--pos)}
.pos-list .row .exit-plan .badge.warn{background:rgba(180,83,9,.15);color:#b45309}
.pos-list .row .exit-plan .badge.bad{background:rgba(220,38,38,.13);color:var(--neg)}
.pos-empty{color:var(--ink-3);padding:32px 0;font-size:15px}
[data-density="compact"] .home-hero{padding:36px 0 16px}
[data-density="compact"] .home-hero .nav-figure .num{font-size:72px}
[data-density="compact"] .pos-list .row{padding:16px 0}
[data-density="compact"] .pos-list .row .last{font-size:16px}
[data-density="dense"] .home-hero{padding:24px 0 10px}
[data-density="dense"] .home-hero .nav-figure .num{font-size:60px}
[data-density="dense"] .pos-list .row{padding:12px 0}
[data-density="dense"] .pos-list .row .last{font-size:15px}
[data-density="dense"] .pos-list .row .nm{font-size:15px}
[data-density="dense"] .pos-list .row .avatar{width:36px;height:36px;font-size:11px}

/* CLICKABLE ROWS (log + watch rows are buttons that open the shared dialog) */
.rowbtn{display:block;width:100%;text-align:left;background:transparent;border:0;cursor:pointer;font-family:var(--sans)}
.rowbtn:hover{background:var(--bg-soft)}

/* LOG pane: timestamped activity list (design .log-list .e). */
.log-list{padding-bottom:80px}
.log-list .e{display:grid;grid-template-columns:110px 1fr 150px;gap:24px;padding:22px 0;border-bottom:1px solid var(--rule);
  font-size:15px;line-height:1.6;align-items:start}
.log-list .e:last-child{border-bottom:0}
.log-list .e .t{font-family:var(--mono);font-size:12px;color:var(--ink-3);padding-top:4px}
.log-list .e .body{color:var(--ink-1)}
.log-list .e .kind{display:inline-block;font-family:var(--sans);font-size:10.5px;font-weight:700;letter-spacing:0.06em;
  text-transform:uppercase;padding:2px 10px;border-radius:999px;margin-right:10px;vertical-align:2px;background:var(--bg-soft);color:var(--ink-2)}
.log-list .e .kind.dec{color:var(--accent-deep);background:var(--accent-soft)}
.log-list .e .kind.amd{color:var(--pos);background:rgba(4,160,9,0.08)}
.log-list .e .kind.apv{color:var(--ink-1);background:var(--bg-soft)}
.log-list .e .kind.flg{color:var(--neg);background:var(--neg-soft)}
.log-list .e .kind.vto{color:var(--ink-3);background:var(--bg-soft)}
.log-list .e .ctx{text-align:right;font-family:var(--mono);font-size:11px;color:var(--ink-3);padding-top:4px}
.log-list .e .ctx b{color:var(--ink-1);font-weight:600}
.pg{display:flex;gap:18px;align-items:center;padding:24px 0 80px;font-family:var(--sans);font-weight:600;font-size:13px}
.pg a{color:var(--accent-deep);text-decoration:none}
.pg a:hover{text-decoration:underline}

/* WATCH pane: own watchlist (design .watch-list .row). */
.watch-list{padding-bottom:80px}
.watch-list .row{display:grid;grid-template-columns:60px 1fr 120px 140px;gap:24px;align-items:center;padding:24px 0;
  border-bottom:1px solid var(--rule);transition:background .15s}
.watch-list .row:last-child{border-bottom:0}
.watch-list .row .avatar{width:52px;height:52px;border-radius:50%;display:grid;place-items:center;color:white;
  font-family:var(--sans);font-weight:700;font-size:14px;background:linear-gradient(135deg,var(--accent),var(--accent-deep))}
.watch-list .row .body .top{display:flex;align-items:center;gap:12px;margin-bottom:6px}
.watch-list .row .sym{font-family:var(--sans);font-weight:700;font-size:22px;letter-spacing:-0.025em}
.watch-list .row .tag{font-size:11px;font-weight:700;padding:3px 10px;border-radius:999px;letter-spacing:0.04em;
  text-transform:uppercase;background:var(--bg-soft);color:var(--ink-2)}
.watch-list .row .tag.ready{color:var(--accent-deep);background:var(--accent-soft)}
.watch-list .row .tag.wait{color:var(--ink-2);background:var(--bg-soft)}
.watch-list .row .tag.armed{color:var(--neg);background:var(--neg-soft)}
.watch-list .row .why{font-size:14px;color:var(--ink-2);line-height:1.5}
.watch-list .row .why b{color:var(--ink-1);font-family:var(--mono);font-size:13px;font-weight:700}
.watch-list .row .last{text-align:right;font-family:var(--mono);font-size:11px;color:var(--ink-3);letter-spacing:0.04em}
.watch-list .row .last b{display:block;font-family:var(--sans);font-weight:700;font-size:20px;color:var(--ink-1);
  letter-spacing:-0.015em;margin-top:2px}
.watch-list .row .trigger{text-align:right;font-family:var(--mono);font-size:11px;color:var(--ink-3);letter-spacing:0.04em}
.watch-list .row .trigger b{display:block;font-family:var(--sans);font-weight:700;font-size:16px;color:var(--accent-deep);margin-top:2px}

/* WATCH pane: a friend's shared-watchlist rows when browsing (the small top-right
   share/view control is styled by .watch-top / .watch-ctrl, below). */
.shared-list{padding-bottom:24px}
.shared-list .row{display:grid;grid-template-columns:52px 1fr auto;gap:18px;align-items:center;padding:16px 0;
  border-bottom:1px solid var(--rule)}
.shared-list .row:last-child{border-bottom:0}
.shared-list .row .avatar{width:44px;height:44px;border-radius:50%;display:grid;place-items:center;color:white;
  font-family:var(--sans);font-weight:700;font-size:13px;background:linear-gradient(135deg,var(--accent),var(--accent-deep))}
.shared-list .row .sym{font-family:var(--sans);font-weight:700;font-size:18px;letter-spacing:-0.02em}
.shared-list .row .why{font-size:13px;color:var(--ink-2);line-height:1.45;margin-top:4px}
.shared-list .row .why b{color:var(--ink-1);font-family:var(--mono);font-size:12px;font-weight:700}
.shared-add{font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:0.04em;padding:7px 14px;
  border-radius:999px;background:var(--accent-deep);color:#fff;text-decoration:none;white-space:nowrap}
.shared-add:hover{opacity:.88}
.watch-top{position:relative}
.watch-ctrl{position:absolute;top:2px;right:0;display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:11px;color:var(--ink-3)}
.watch-ctrl .share-lbl{text-transform:uppercase;letter-spacing:0.07em;color:var(--ink-3);font-family:var(--mono);font-size:10px}
.watch-ctrl select{font-family:var(--mono);font-size:11px;padding:3px 7px;border:1px solid var(--rule-2);border-radius:7px;background:var(--surface);color:var(--ink-1)}
.watch-ctrl .view-form{display:inline-flex;margin-right:6px}
.watch-ctrl .share-refresh{text-decoration:none;font-size:13px;color:var(--ink-3)}
.watch-ctrl .share-refresh:hover{color:var(--ink-1)}
.watch-ctrl .share-pill.off{padding:3px 9px;border-radius:999px;background:var(--bg-soft);color:var(--ink-3)}
.watch-ctrl .share-hint{color:var(--ink-3);font-style:italic}
.shared-have{font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:0.04em;color:var(--ink-3);white-space:nowrap}

/* CONVICTION pane: clickable, expandable idea rows (.conv-item details). */
.conv-list{padding-bottom:80px}
.conv-item{border-bottom:1px solid var(--rule)}
.conv-item:last-child{border-bottom:0}
.conv-row{display:flex;align-items:center;gap:18px;padding:20px 0;cursor:pointer;list-style:none}
.conv-row::-webkit-details-marker{display:none}
.conv-row:hover{background:var(--bg-soft)}
.conv-row .avatar{width:52px;height:52px;border-radius:50%;display:grid;place-items:center;color:white;flex:none;
  font-family:var(--sans);font-weight:700;font-size:14px;background:linear-gradient(135deg,var(--accent),var(--accent-deep))}
.conv-row .conv-sum{flex:1}
.conv-row .sym{font-family:var(--sans);font-weight:700;font-size:20px;letter-spacing:-0.025em;margin-right:10px}
.conv-row .tag{font-size:11px;font-weight:700;padding:3px 10px;border-radius:999px;letter-spacing:0.04em;
  text-transform:uppercase;background:var(--bg-soft);color:var(--ink-2)}
.conv-row .tag.ready{color:var(--accent-deep);background:var(--accent-soft)}
.conv-row .tag.wait{color:var(--ink-2);background:var(--bg-soft)}
.conv-row .tag.armed{color:var(--neg);background:var(--neg-soft)}
.conv-row .why{display:block;font-size:14px;color:var(--ink-2);line-height:1.5;margin-top:6px}
.conv-row .conv-caret{color:var(--ink-3);font-size:20px;font-weight:700;flex:none}
.conv-item[open] .conv-caret{transform:rotate(45deg)}
.conv-detail{padding:4px 0 22px 70px}
.conv-plan{font-size:15px;color:var(--ink-1);margin-bottom:8px}
.conv-how{margin-top:16px}
.conv-how h4{font-size:13px;font-weight:700;letter-spacing:0.04em;color:var(--accent-deep);margin:0 0 8px}
.conv-how ol{margin:0;padding-left:20px}
.conv-how li{font-size:14px;color:var(--ink-2);line-height:1.6;margin-bottom:6px}

/* SHADOW pane: per-stock profit table (compact, mono numerics) + reuses
   .watch-list rows for the open/closed trade lists, .pos/.neg for colored P&L. */
.shadow-table{width:100%;border-collapse:collapse;margin:4px 0 12px;font-size:14px}
.shadow-table th{font-family:var(--mono);font-size:10.5px;font-weight:500;letter-spacing:0.08em;text-transform:uppercase;
  color:var(--ink-3);text-align:left;padding:8px 10px;border-bottom:1px solid var(--rule)}
.shadow-table td{padding:11px 10px;border-bottom:1px solid var(--rule);color:var(--ink-1)}
.shadow-table tr:last-child td{border-bottom:0}
.shadow-table td.sym-cell{font-family:var(--sans);font-weight:700;font-size:15px;letter-spacing:-0.015em}
.shadow-table th.num,.shadow-table td.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
.shadow-table td.num{font-size:13px;font-weight:500;color:var(--ink-2)}
.shadow-table td.num.pos{color:var(--pos)}
.shadow-table td.num.neg{color:var(--neg)}
.watch-list .row .trigger.pos b{color:var(--pos)}
.watch-list .row .trigger.neg b{color:var(--neg)}
.watch-list .row .trigger small{display:block;font-family:var(--sans);font-weight:500;font-size:11.5px;color:var(--ink-3);margin-top:2px}

/* EVOLVE pane: stack of mind-changes (design .evo-list .item). */
.evo-list{padding:24px 0 80px}
.evo-list .item{padding:28px 0;border-bottom:1px solid var(--rule)}
.evo-list .item:last-child{border-bottom:0}
.evo-list .item .head{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;gap:16px}
.evo-list .item .head .kind{font-family:var(--sans);font-weight:700;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;
  padding:4px 12px;background:var(--accent-soft);color:var(--accent-deep);border-radius:999px}
.evo-list .item .head .kind.enf{color:var(--pos);background:rgba(4,160,9,0.08)}
.evo-list .item .head .kind.upd{color:var(--ink-1);background:var(--bg-soft)}
.evo-list .item .head .kind.rej{color:var(--neg);background:var(--neg-soft)}
.evo-list .item .head .when{font-family:var(--mono);font-size:12px;color:var(--ink-3)}
.evo-list .item h3{font-family:var(--sans);font-size:22px;font-weight:700;letter-spacing:-0.025em;line-height:1.25;
  color:var(--ink-1);margin:0 0 12px;max-width:920px}
.evo-list .item p{font-size:16px;color:var(--ink-2);line-height:1.6;margin:0 0 18px;max-width:800px}
.evo-list .item .tags{display:flex;gap:8px;flex-wrap:wrap}
.evo-list .item .tags span{font-family:var(--mono);font-size:11.5px;font-weight:500;color:var(--accent-deep);padding:3px 10px;
  background:var(--accent-soft);border-radius:999px}
.evo-list .item .tags span::before{content:"#";opacity:0.5;margin-right:1px}
.section-label{font-family:var(--mono);font-size:11.5px;letter-spacing:0.18em;text-transform:uppercase;color:var(--ink-3);
  margin:36px 0 4px;padding-top:8px}

/* MIND pane: playbook.md rendered via _md_lite, in editorial type. */
.mind-doc{max-width:760px;padding-bottom:80px}
.mind-doc h1{font-family:var(--sans);font-size:34px;font-weight:700;letter-spacing:-0.03em;line-height:1.1;color:var(--ink-1);margin:24px 0 10px}
.mind-doc h2{font-family:var(--sans);font-size:22px;font-weight:700;letter-spacing:-0.02em;color:var(--ink-1);margin:30px 0 8px}
.mind-doc p{font-size:16.5px;line-height:1.7;color:var(--ink-1);margin:0 0 14px;text-wrap:pretty}
.mind-doc .row{font-size:16px;line-height:1.6;color:var(--ink-1)}

/* AGENTS pane: roster from registry.json. */
.agents-list{padding-bottom:80px}
.agents-list .grp{font-family:var(--mono);font-size:11.5px;letter-spacing:0.18em;text-transform:uppercase;color:var(--ink-3);
  margin:32px 0 8px}
.agents-list details{padding:18px 0;border-bottom:1px solid var(--rule)}
.agents-list details:last-child{border-bottom:0}
.agents-list summary{cursor:pointer;list-style:none;display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.agents-list summary::-webkit-details-marker{display:none}
.agents-list summary .nm{font-family:var(--sans);font-weight:700;font-size:18px;letter-spacing:-0.015em;color:var(--ink-1)}
.agents-list summary .model{font-family:var(--mono);font-size:11px;padding:2px 9px;background:var(--bg-soft);border-radius:999px;color:var(--ink-2)}
.agents-list summary .role{font-size:14px;color:var(--ink-2)}
.agents-list .when{font-family:var(--mono);font-size:12px;color:var(--ink-3);margin-top:6px}
.agents-list .spec{white-space:pre-wrap;font-size:13px;line-height:1.6;color:var(--ink-2);margin-top:12px;padding-top:12px;
  border-top:1px solid var(--rule);font-family:var(--mono)}

/* SHARED DETAIL DIALOG (restyled to the light theme). */
dialog{background:var(--surface);color:var(--ink-1);border:1px solid var(--rule-2);border-radius:14px;max-width:760px;width:92%;
  padding:0;box-shadow:0 20px 60px rgba(10,10,12,0.18)}
dialog::backdrop{background:rgba(10,10,12,0.45)}
.dlg-hd{display:flex;align-items:center;gap:8px;padding:14px 18px;border-bottom:1px solid var(--rule)}
.dlg-hd b{font-size:15px;font-weight:700;letter-spacing:-0.01em}
.dlg-x{margin-left:auto;background:var(--bg-soft);border:0;color:var(--ink-2);border-radius:8px;padding:4px 11px;cursor:pointer;font-size:15px}
.dlg-x:hover{background:var(--bg-deep)}
.dlg-bd{padding:14px 18px;max-height:64vh;overflow:auto}
.kv{margin:0 0 12px}
.kv-k{font-family:var(--mono);font-size:10px;line-height:1.4;text-transform:uppercase;letter-spacing:0.6px;color:var(--ink-3);margin-bottom:3px}
.kv-v{white-space:pre-wrap;word-break:break-word;font-size:13.5px;color:var(--ink-1)}
.kv-nest{border-left:2px solid var(--rule);padding-left:10px;margin-top:4px}
.kv-chips{display:flex;flex-wrap:wrap;gap:5px}
.chipv{font-family:var(--mono);font-size:11.5px;background:var(--bg-soft);color:var(--ink-2);padding:3px 9px;border-radius:999px}
.v-for{border-left:2px solid var(--accent);padding-left:8px;color:var(--accent-deep)}
.v-against{border-left:2px solid var(--neg);padding-left:8px;color:var(--neg)}

/* DECIDE pane (design .decide-hero / .pending / .decide-history). Buttons here
   submit a GET form, so .btn variants are reused on real <button> submits. */
.btn{font-family:var(--sans);font-weight:700;font-size:12.5px;color:var(--ink-1);background:var(--bg-soft);
  border:0;padding:10px 14px;border-radius:10px;cursor:pointer;transition:all .15s;text-align:center;letter-spacing:-0.005em}
.btn:hover{background:var(--bg-deep)}
.btn.good{background:var(--accent);color:var(--ink-1)}
.btn.good:hover{background:var(--accent-deep);color:white}
.btn.bad{background:var(--neg);color:white}
.btn.bad:hover{filter:brightness(1.08)}
.decide-hero{padding:36px 0 56px}
.decide-hero .pending{background:var(--surface);border-radius:18px;padding:32px 40px;border-top:4px solid var(--accent);margin-bottom:18px}
.decide-hero .pending:last-child{margin-bottom:0}
.decide-hero .pending .kind{font-family:var(--mono);font-size:11.5px;letter-spacing:0.18em;text-transform:uppercase;
  color:var(--accent-deep);font-weight:600;margin-bottom:18px;display:inline-flex;align-items:center;gap:10px}
.decide-hero .pending h1{font-family:var(--sans);font-size:32px;font-weight:700;letter-spacing:-0.035em;line-height:1.1;
  color:var(--ink-1);margin:0 0 18px;max-width:820px}
.decide-hero .pending .body{font-size:16px;color:var(--ink-2);line-height:1.6;max-width:780px;margin:0 0 16px}
.decide-form textarea{width:100%;box-sizing:border-box;background:var(--bg);color:var(--ink-1);border:1px solid var(--rule-2);
  border-radius:10px;padding:10px 12px;font:inherit;font-size:14px;margin:8px 0 16px;resize:vertical}
.decide-hero .pending .acts{display:flex;gap:12px;max-width:520px}
.decide-history{padding:16px 0 80px}
.decide-history h3{font-family:var(--sans);font-weight:700;font-size:18px;color:var(--ink-1);letter-spacing:-0.02em;margin:0 0 18px}
.decide-history .h-row{display:grid;grid-template-columns:120px 1fr;gap:18px;padding:16px 0;border-bottom:1px solid var(--rule);
  font-size:14px;line-height:1.5}
.decide-history .h-row:last-child{border:0}
.decide-history .h-row .t{font-family:var(--mono);font-size:11.5px;color:var(--ink-3);padding-top:1px}
.decide-history .h-row .status{display:inline-block;font-size:10.5px;font-weight:700;padding:2px 10px;border-radius:999px;
  margin-right:10px;letter-spacing:0.04em;text-transform:uppercase}
.decide-history .h-row .status.apv{color:var(--accent-deep);background:var(--accent-soft)}
.decide-history .h-row .status.rej{color:var(--neg);background:var(--neg-soft)}
.decide-history .h-row .status.exp{color:var(--ink-3);background:var(--bg-soft)}
.pane-head h1 .num{color:var(--accent-deep);font-family:var(--mono);font-weight:600;font-size:32px;letter-spacing:-0.01em}

/* CONTROLS overlay (design #tweaks aside). */
.tweaks{position:fixed;bottom:20px;right:88px;z-index:100;width:320px;max-height:84vh;overflow:auto;background:var(--surface);
  border:1px solid var(--rule);border-radius:14px;padding:16px 18px 18px;box-shadow:0 16px 40px rgba(10,10,12,0.18);font-family:var(--sans)}
.tweaks[hidden]{display:none}
.tweaks .tw-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;padding-bottom:10px;
  border-bottom:1px solid var(--rule);font-family:var(--mono);font-size:11px;letter-spacing:0.16em;text-transform:uppercase;
  color:var(--ink-1);font-weight:600}
.tweaks .tw-head button{background:transparent;border:0;font-size:18px;cursor:pointer;color:var(--ink-3);line-height:1;
  padding:0;width:24px;height:24px;border-radius:4px;transition:all .15s}
.tweaks .tw-head button:hover{color:var(--ink-1);background:var(--bg-soft)}
.tweaks .tw-section{margin-bottom:18px}
.tweaks .tw-section:last-child{margin-bottom:0}
.tweaks .tw-label{font-family:var(--mono);font-size:9.5px;letter-spacing:0.16em;text-transform:uppercase;color:var(--ink-3);
  font-weight:600;margin-bottom:8px}
.tw-textarea{width:100%;box-sizing:border-box;background:var(--bg);color:var(--ink-1);border:1px solid var(--rule-2);
  border-radius:8px;padding:8px 10px;font:inherit;font-size:13px;resize:vertical}
.tw-radio{display:flex;background:var(--bg-soft);border-radius:8px;padding:3px}
.tw-radio button{flex:1;background:transparent;border:0;padding:7px 8px;font-family:var(--sans);font-weight:600;font-size:12px;
  color:var(--ink-2);border-radius:5px;cursor:pointer;letter-spacing:-0.005em;transition:all .15s}
.tw-radio button:hover:not(.on){color:var(--ink-1)}
.tw-radio button.on{background:var(--surface);color:var(--ink-1);box-shadow:0 1px 2px rgba(0,0,0,0.06)}
.tw-colors{display:flex;gap:8px}
.tw-colors button{width:26px;height:26px;border-radius:50%;border:0;cursor:pointer;padding:0;transition:transform .15s}
.tw-colors button:hover:not(.on){transform:scale(1.1)}
.tw-colors button.on{box-shadow:0 0 0 2px var(--surface),0 0 0 3px var(--ink-1)}
/* temperament dials inside the overlay (adapted from v1) */
.dial{padding:8px 0;border-bottom:1px solid var(--rule)}.dial:last-of-type{border:0}
.dial-hd{display:flex;align-items:baseline;gap:8px;margin-bottom:6px}
.dial-hd b{font-size:13px;font-weight:700}.dial-steer{font-size:10.5px}
.dial-row{display:flex;align-items:center;gap:8px}
.dial-row input[type=range]{-webkit-appearance:none;appearance:none;flex:1;height:6px;border-radius:6px;outline:none;
  cursor:pointer;background:var(--bg-soft)}
.dial-row input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:14px;height:14px;
  border-radius:50%;background:var(--accent-deep);cursor:pointer}
.dial-row input[type=range]::-moz-range-thumb{width:12px;height:12px;border-radius:50%;background:var(--accent-deep);
  border:2px solid var(--surface);cursor:pointer}
.pole{font-size:10px;color:var(--ink-3);width:64px}.pole-hi{text-align:right}
.dial-val{min-width:30px;text-align:center;font-variant-numeric:tabular-nums;font-family:var(--mono);font-size:11px;
  background:var(--bg-soft);color:var(--ink-2);padding:2px 7px;border-radius:999px}
.dial-group{font-size:10px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-3);margin:12px 0 2px;padding-top:10px;border-top:1px solid var(--rule)}
.dial-group:first-child{border-top:0;padding-top:0;margin-top:0}

/* INBOX floating action button + composer dialog. */
.ibx-dlg{width:min(560px,94vw)}
.ibx-form textarea{width:100%;box-sizing:border-box;background:var(--bg);color:var(--ink-1);border:1px solid var(--rule-2);
  border-radius:8px;padding:8px 10px;font:inherit;font-size:14px;resize:vertical}
.ibx-row{display:flex;gap:8px;margin-top:8px}
.ibx-row select{background:var(--bg);color:var(--ink-1);border:1px solid var(--rule-2);border-radius:8px;padding:6px 10px;font:inherit}
.ibx-row button{background:var(--accent);color:white;border:none;border-radius:8px;padding:6px 18px;cursor:pointer;
  font-family:var(--sans);font-weight:700}
.ibx-row button:hover{background:var(--accent-deep)}
.ibx-sec{font-weight:700;margin:16px 0 6px;font-size:13px}
.ibx-item{border-top:1px solid var(--rule);padding:8px 0}
.ibx-meta{display:flex;gap:8px;align-items:center;margin-bottom:3px}
.ibx-meta .pill{font-family:var(--mono);font-size:11px;background:var(--bg-soft);color:var(--ink-2);padding:2px 9px;border-radius:999px}
.ibx-text{white-space:pre-wrap;font-size:13.5px;color:var(--ink-1)}
.ibx-out{margin-top:3px;font-size:12.5px}
.ibx-out a{color:var(--accent-deep)}
.ibx-arch{margin-top:14px}.ibx-arch summary{cursor:pointer;color:var(--ink-3)}
.ibx-thumbs{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.ibx-thumb{width:56px;height:56px;object-fit:cover;border-radius:8px;border:1px solid var(--rule-2)}"""


# Applied in <head> before paint so the saved accent/density never flash. Mirrors
# v1's _THEME_HEAD no-flash pattern. Uses v2-scoped keys (v2accent / v2density) so
# it does not collide with v1's own theme keys. Defaults: green / compact.
_ACCENT_DENSITY_HEAD = """<script>
(function(){try{
var a=localStorage.getItem('v2accent')||'green';
var d=localStorage.getItem('v2density')||'compact';
document.documentElement.dataset.accent=a;
document.documentElement.dataset.density=d;
}catch(e){}})();
</script>"""


# Tab strip: (key, label) in design order, plus an `agents` tab the design lacks.
_TABS = (
    ("home", "Home"),
    ("mind", "Mind"),
    ("watch", "Watch"),
    ("conviction", "Conviction"),
    ("shadow", "Shadow Trades"),
    ("decide", "Decide"),
    ("log", "Log"),
    ("evolve", "Evolve"),
    ("agents", "Agents"),
)


def _agent_tabs() -> dict:
    """Map tab_key -> {owner, enabled} for agents that render a tab (ConvictionWriter owns
    Conviction, ShadowTrader owns Shadow Trades). A disabled owner hides the tab and its route
    shows a disabled notice. Reads agent_controls.status_all(); guarded."""
    out = {}
    try:
        for s in agent_controls.status_all():
            tk = s.get("tab")
            if tk:
                out[tk] = {"owner": s["key"], "enabled": s["enabled"]}
    except Exception:
        pass
    return out


def _glance_header(tab: str) -> str:
    """The sticky glance header: brand, an "Instruct the mind" button (opens the
    instruction composer dialog, with a pending-count badge), and a NAV
    mini-readout wired to live portfolio equity (snapshot fallback). Generic
    brand text (no owner name); the NAV value is read from state/API at run
    time."""
    try:
        n_pending = len(instructions.pending())
    except Exception:
        n_pending = 0
    badge = f'<span class="instruct-badge">{n_pending}</span>' if n_pending else ""
    nav = "$--"
    try:
        equity = _portfolio_data_v2().get("equity")
        if isinstance(equity, (int, float)):
            nav = f"${equity:,.2f}"
    except Exception:
        pass
    return (
        '<header class="chrome">'
        '<div class="wrap chrome-top">'
        '<div class="brand">Autonomous Trader</div>'
        '<div class="state-pill">'
        '<button class="instruct-btn" type="button" title="Instruct the mind" '
        'onclick="document.getElementById(\'ibx-dlg\').showModal()">&#9998; '
        f'Instruct the mind{badge}</button>'
        '</div>'
        '<div class="head-right">'
        f'<div class="nav-mini"><b>{nav}</b><span>NAV</span></div>'
        '<button class="ctrl-btn" type="button" onclick="v2openTw()">Controls</button>'
        '</div>'
        '</div>'
        f'{_tab_nav(tab)}'
        '</header>'
    )


def _tab_nav(cur: str) -> str:
    """The tab strip, rendered as links (server-side navigation). The current
    tab carries data-active=true so the CSS underline/ink-1 color applies."""
    out = '<div class="wrap"><nav class="tab-strip" id="tabs">'
    at = _agent_tabs()
    for key, label in _TABS:
        if key in at and not at[key]["enabled"]:
            continue  # owning agent disabled -> hide its tab
        active = ' data-active="true"' if key == cur else ""
        out += f'<a href="/?tab={key}"{active}>{escape(label)}</a>'
    out += "</nav></div>"
    return out


# --- Stub panes (placeholder cards). Later chunks replace these bodies. ---

def _stub(title: str) -> str:
    return (f'<div class="wrap"><div class="pane-head"><h1>{escape(title)}</h1></div>'
            f'<div class="card"><h2>{escape(title)}</h2>'
            f'<p class="muted">Coming soon.</p></div></div>')


def _pos_fields(p: dict) -> dict:
    """Pull the fields v1 uses out of one raw position dict (same nested shape
    for both live API positions and the on-disk snapshot), computing gain /
    gain_pct / weight_pct. All numeric where parseable, else None. Defensive
    about missing/nested fields."""
    if not isinstance(p, dict):
        return {"symbol": str(p), "name": "", "qty": None, "value": None,
                "last": None, "cost": None, "gain": None, "gain_pct": None,
                "weight_pct": None}
    inst = p.get("instrument") or {}
    sym = str(inst.get("symbol") or "?")
    name = str(inst.get("name") or "")
    qty = _num(p.get("quantity"))
    value = _num(p.get("currentValue"))
    lp = p.get("lastPrice")
    last = _num(lp.get("lastPrice")) if isinstance(lp, dict) else None
    cost = p.get("costBasis") or {}
    total_cost = _num(cost.get("totalCost")) if isinstance(cost, dict) else None
    ig = p.get("instrumentGain") or {}
    gain = _num(ig.get("gainValue")) if isinstance(ig, dict) else None
    gain_pct = _num(ig.get("gainPercentage")) if isinstance(ig, dict) else None
    weight_pct = _num(p.get("percentOfPortfolio"))
    return {"symbol": sym, "name": name, "qty": qty, "value": value,
            "last": last, "cost": total_cost, "gain": gain,
            "gain_pct": gain_pct, "weight_pct": weight_pct}


def _portfolio_data_v2() -> dict:
    """Live portfolio as a DICT (not HTML). Adapts v1's `_portfolio_live()` fetch
    math: equity = sum of equity[].value (or cash), cash = cashOnlyBuyingPower,
    settled via settlement.available_settled(cash). On ANY live-fetch failure,
    falls back to state/last_snapshot.json (source="snapshot"). Never raises.
    Returns {equity, cash, settled, positions, source, ts} where positions is a
    list of normalized dicts from `_pos_fields`."""
    if pub is not None:
        try:
            port = pub.get_portfolio()
            cash = float((port.get("buyingPower") or {}).get("cashOnlyBuyingPower"))
            equity = sum(float(e["value"]) for e in port.get("equity", []) or []) or cash
            equity = round(equity, 2)
            cash = round(cash, 2)
            settled = None
            if settlement is not None:
                try:
                    settled = settlement.available_settled(cash)
                except Exception:
                    settled = None
            positions = [_pos_fields(p) for p in (port.get("positions") or [])]
            return {"equity": equity, "cash": cash, "settled": settled,
                    "positions": positions, "source": "live",
                    "ts": datetime.now().strftime("%H:%M")}
        except Exception:
            pass
    # FALLBACK: last on-disk snapshot (gitignored runtime state).
    snap = _read_json("last_snapshot.json", {}) or {}
    positions = [_pos_fields(p) for p in (snap.get("positions") or [])]
    ts = _fmt_ts(snap.get("ts", ""))[-5:] if snap.get("ts") else ""
    return {"equity": snap.get("equity"), "cash": snap.get("cash"),
            "settled": snap.get("settled_cash"), "positions": positions,
            "source": "snapshot", "ts": ts}


def _spark_svg(points) -> str:
    """Tiny inline line spark over `points` (a list of numbers). Returns an EMPTY
    <svg class="spark"></svg> when there is no/too-little data -- we do NOT
    fabricate per-symbol history (none exists in state/), matching the design's
    empty cash-row spark."""
    pts = [p for p in (points or []) if isinstance(p, (int, float))]
    if len(pts) < 2:
        return '<svg class="spark"></svg>'
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    n = len(pts)
    coords = " ".join(
        f"{(i / (n - 1)) * 100:.1f},{(1 - (v - lo) / span) * 32 + 2:.1f}"
        for i, v in enumerate(pts))
    up = pts[-1] >= pts[0]
    col = "var(--pos)" if up else "var(--neg)"
    return (f'<svg class="spark" viewBox="0 0 100 36" preserveAspectRatio="none">'
            f'<polyline points="{coords}" stroke="{col}" stroke-width="2" '
            f'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>')


# Window keys for the home timeframe links: (key, label). "today" labels as 1D
# to match the design's pill row. Same window math as v1's _filter_window.
_WINDOWS_V2 = (("today", "1D"), ("1w", "1W"), ("1m", "1M"),
               ("3m", "3M"), ("ytd", "YTD"), ("all", "All"))


def _win_pills(win: str) -> str:
    """The timeframe pills as server-nav links (/?tab=home&win=<k>), current on."""
    return '<div class="tf-pills">' + "".join(
        f'<a class="{"on" if k == win else ""}" href="/?tab=home&win={k}">{escape(label)}</a>'
        for k, label in _WINDOWS_V2) + "</div>"


# Hover crosshair for the equity chart. Scoped to each .chart-frame so multiple
# frames on a page are independent; binds once per frame (data-cx flag) and reads
# the per-point .eq-data JSON. Geometry is computed against the .cx-layer rect,
# which shares the SVG's CSS insets, so xp/yp percentages line up with the
# stretched (preserveAspectRatio="none") line.
_CHART_HOVER_JS = """<script>
(function(){
  function clamp(x,a,b){return x<a?a:(x>b?b:x);}
  function wire(frame){
    if(frame.dataset.cx) return; frame.dataset.cx="1";
    var ds=frame.querySelector(".eq-data");
    var layer=frame.querySelector(".cx-layer");
    if(!ds||!layer) return;
    var line=layer.querySelector(".cx-line");
    var dot=layer.querySelector(".cx-dot");
    var tip=layer.querySelector(".cx-tip");
    var pts=null;
    try{pts=JSON.parse(ds.textContent);}catch(e){return;}
    if(!pts||!pts.length) return;
    function show(d){line.style.display=dot.style.display=tip.style.display=d;}
    frame.addEventListener("mousemove",function(e){
      var r=layer.getBoundingClientRect();
      if(!r.width) return;
      var frac=clamp((e.clientX-r.left)/r.width,0,1);
      var idx=Math.round(frac*(pts.length-1));
      var p=pts[idx]; if(!p) return;
      line.style.left=p.xp+"%";
      dot.style.left=p.xp+"%"; dot.style.top=p.yp+"%";
      tip.style.left=p.xp+"%"; tip.style.top=p.yp+"%";
      tip.textContent="$"+p.v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})+" \\u00b7 "+p.t;
      show("block");
    });
    frame.addEventListener("mouseleave",function(){show("none");});
  }
  function init(){
    var frames=document.querySelectorAll(".chart-frame");
    for(var i=0;i<frames.length;i++) wire(frames[i]);
  }
  if(document.readyState==="loading"){document.addEventListener("DOMContentLoaded",init);}else{init();}
})();
</script>"""


def _equity_chart_svg_v2(win: str) -> str:
    """Light-themed inline equity chart (area fill var(--accent-soft) under a
    var(--accent) line) plus a first->last delta caption. Returns ONLY the
    .chart-frame div (the timeframe pills are rendered separately by _win_pills
    so they can sit in the hero's nav-row). Data from `_equity_history()` +
    `_filter_window(pts, win)`; windowing/first-last logic mirrors v1's
    `_equity_chart_html`. New SVG markup matching the design's .chart-frame."""
    win = (win or "all").lower()
    win_keys = dict(_WINDOWS_V2)
    if win not in win_keys:
        win = "all"

    all_pts = _equity_history()
    if not all_pts:
        return ('<div class="chart-frame"><div class="chart-empty">'
                'No equity history yet. A point is recorded each run to '
                'state/equity_history.jsonl.</div></div>')
    pts = _filter_window(all_pts, win)
    if not pts:
        return ('<div class="chart-frame"><div class="chart-empty">'
                'No data points in this window.</div></div>')

    ys = [p[1] for p in pts]
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1.0
    first, cur = ys[0], ys[-1]
    delta = cur - first
    pct = (delta / first * 100.0) if first else 0.0
    n = len(ys)
    W, H = 1000, 360

    def sx(i):
        return (i / (n - 1)) * W if n > 1 else W / 2

    def sy(v):
        return (1 - (v - lo) / span) * H

    # Color the line/area by the window's net change: red on a losing window
    # (last < first), green otherwise. Matches the delta caption's sign coloring.
    losing = cur < first
    stroke_col = "var(--neg)" if losing else "var(--accent)"
    fill_col = "var(--neg-soft)" if losing else "var(--accent-soft)"
    line = " ".join(f"L{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(ys))
    line = ("M" + line[1:]) if line.startswith("L") else line
    if n > 1:
        area = f"{line} L{W},{H} L0,{H} Z"
        shape = (f'<path d="{area}" fill="{fill_col}"/>'
                 f'<path d="{line}" fill="none" stroke="{stroke_col}" '
                 f'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>')
        dot = (f'<circle cx="{sx(n - 1):.1f}" cy="{sy(cur):.1f}" r="6" '
               f'fill="{stroke_col}" stroke="white" stroke-width="2"/>')
    else:
        shape = (f'<circle cx="{sx(0):.1f}" cy="{sy(ys[0]):.1f}" r="5" '
                 f'fill="{stroke_col}"/>')
        dot = ""
    svg = (f'<svg class="line" viewBox="0 0 {W} {H}" preserveAspectRatio="none">'
           f'{shape}{dot}</svg>')

    # Five horizontal grid lines, evenly spaced, labeled top (hi) -> bottom (lo).
    grid = ""
    for i in range(5):
        frac = i / 4
        val = hi - frac * (hi - lo)
        grid += (f'<div class="g" style="top:{frac * 100:.0f}%">'
                 f'<span>${val:,.0f}</span></div>')
    yaxis = f'<div class="yaxis">{grid}</div>'

    sign = "+" if delta >= 0 else "-"
    dcls = "pos" if delta >= 0 else "neg"
    cap = (f'<div class="cap">over {escape(win_keys[win])} &middot; '
           f'<b class="{dcls}">{sign}${abs(delta):,.2f} ({sign}{abs(pct):.2f}%)</b>'
           f'<span class="sep" style="color:var(--ink-4);margin:0 8px">&middot;</span>'
           f'{n} pts</div>')

    # Per-point data for hover crosshair. xp/yp are percentages within the SVG's
    # inset box (the .cx-layer below shares the SVG's CSS insets), so they line up
    # with the stretched preserveAspectRatio="none" line.
    eq_data = [
        {"v": round(ys[i], 2),
         "t": pts[i][0].strftime("%b %d %H:%M"),
         "xp": (sx(i) / W * 100.0),
         "yp": (sy(ys[i]) / H * 100.0)}
        for i in range(n)
    ]
    data_script = (f'<script type="application/json" class="eq-data">'
                   f'{_json_for_html(eq_data)}</script>')
    cx_layer = ('<div class="cx-layer">'
                '<div class="cx-line"></div>'
                '<div class="cx-dot"></div>'
                '<div class="cx-tip"></div>'
                '</div>')
    return (f'<div class="chart-frame">{yaxis}{svg}{cap}'
            f'{cx_layer}{data_script}{_CHART_HOVER_JS}</div>')


def _avatar_text(sym: str) -> str:
    """Two-letter avatar label from a symbol (design uses the first two chars)."""
    s = "".join(c for c in str(sym or "").upper() if c.isalnum())
    return escape((s[:2] or "?"))


def _close_criteria(h: dict) -> str:
    """One-line closing strategy from a stored hypothesis (dict or plain string)."""
    hyp = h.get("hypothesis")
    if isinstance(hyp, str):
        txt = hyp
    elif isinstance(hyp, dict):
        txt = hyp.get("trend") or ""
        horizon = hyp.get("horizon")
        tl = hyp.get("target_logic")
        extra = " / ".join(x for x in (horizon, (f"target {tl}" if tl else "")) if x)
        if extra:
            txt = f"{txt} ({extra})" if txt else extra
    else:
        txt = ""
    txt = (txt or "").strip()
    return (txt[:170] + "...") if len(txt) > 170 else txt


def _exit_plan_html(h: dict | None, last) -> str:
    """Full-width exit-plan strip for a position row: stop (with a badge for whether it
    rests at the broker 24/7 or is software-only and thus unprotected between runs),
    target, and the closing criteria from the stored hypothesis. Renders a red flag when
    there is no stop/target on file (undefined closing strategy)."""
    has_stop = isinstance(h, dict) and isinstance(h.get("stop"), (int, float))
    has_tgt = isinstance(h, dict) and isinstance(h.get("target"), (int, float))
    if not has_stop and not has_tgt:
        return ('<div class="exit-plan"><span class="badge bad">NO STOP / TARGET ON FILE</span>'
                '<span class="lbl">closing strategy undefined &mdash; set a stop and target</span></div>')
    lp = last if isinstance(last, (int, float)) and last else None
    # one-line summary: stop and target only
    summ = []
    if has_stop:
        stop = h["stop"]
        dist = f' <span class="lbl">({(stop - lp) / lp * 100:+.1f}%)</span>' if lp else ''
        summ.append(f'<span class="lbl">Stop</span> <b>${stop:,.2f}</b>{dist}')
    else:
        summ.append('<span class="badge bad">NO STOP</span>')
    if has_tgt:
        tgt = h["target"]
        dist = f' <span class="lbl">(+{(tgt - lp) / lp * 100:.1f}% to go)</span>' if (lp and tgt > lp) else ''
        summ.append(f'<span class="lbl">Target</span> <b>${tgt:,.2f}</b>{dist}')
    summary_line = ' &middot; '.join(summ)
    # detail (expand): stop protection badge + the Plan reasoning
    detail = []
    if has_stop:
        kind = (h.get("stop_kind") or "").lower()
        if kind == "resting_broker":
            detail.append('<span class="badge ok">resting GTC &middot; protects 24/7</span>')
        elif kind == "software":
            detail.append('<span class="badge warn">software-only &middot; not resting at broker</span>')
    crit = _close_criteria(h) if isinstance(h, dict) else ''
    if crit:
        detail.append(f'<span><span class="lbl">Plan</span> {escape(crit)}</span>')
    if detail:
        return ('<details class="exit-plan" style="display:block">'
                f'<summary style="cursor:pointer;color:var(--ink-2)">{summary_line}</summary>'
                f'<div style="margin-top:8px;color:var(--ink-2);line-height:1.55">{" ".join(detail)}</div>'
                '</details>')
    return f'<div class="exit-plan">{summary_line}</div>'


def _position_row_v2(p: dict, h: dict | None = None) -> str:
    """One position row in the design's .pos-list .row style: avatar + symbol/name
    + qty + (empty) spark + last price + gain% (green --pos / red --neg) + weight,
    with value/cost as the qty/last sub-labels. All values HTML-escaped. Per-symbol
    price history does not exist in state/, so the spark stays empty."""
    sym = p.get("symbol") or "?"
    name = p.get("name") or ""
    qty = p.get("qty")
    value = p.get("value")
    last = p.get("last")
    cost = p.get("cost")
    gain = p.get("gain")
    gain_pct = p.get("gain_pct")
    weight = p.get("weight_pct")

    avatar = f'<div class="avatar">{_avatar_text(sym)}</div>'
    sub = f'<small>{escape(str(sym))}</small>' if name else ""
    nm = f'<div class="nm">{escape(str(name) or str(sym))}{sub}</div>'
    qty_s = f"{qty:g}" if isinstance(qty, (int, float)) else escape(str(qty or "?"))
    cost_lbl = (f'<br><small style="color:var(--ink-3)">cost ${cost:,.2f}</small>'
                if isinstance(cost, (int, float)) else
                '<br><small style="color:var(--ink-3)">share</small>')
    qty_cell = f'<div class="qty">&times;<b>{qty_s}</b>{cost_lbl}</div>'
    spark = f'<div>{_spark_svg([])}</div>'
    last_s = f"${last:,.2f}" if isinstance(last, (int, float)) else "&mdash;"
    val_lbl = (f'<small style="display:block;font-weight:500;font-size:11.5px;'
               f'color:var(--ink-3);margin-top:2px">${value:,.2f}</small>'
               if isinstance(value, (int, float)) else "")
    last_cell = f'<div class="last">{last_s}{val_lbl}</div>'

    if isinstance(gain_pct, (int, float)):
        pcls = "pos" if gain_pct >= 0 else "neg"
        gsign = "+" if gain_pct >= 0 else "-"
        gv = (f'<small>{gsign}${abs(gain):,.2f} total</small>'
              if isinstance(gain, (int, float)) else "")
        pct_cell = (f'<div class="pct {pcls}">{gsign}{abs(gain_pct):.2f}%{gv}</div>')
    else:
        pct_cell = '<div class="pct"><span class="muted">&mdash;</span></div>'

    wt_s = (f'{weight:.1f}%' if isinstance(weight, (int, float)) else "&mdash;")
    wt_cell = f'<div class="wt">{wt_s}<small>weight</small></div>'
    exit_line = _exit_plan_html(h, p.get("last"))
    return (f'<div class="row">{avatar}{nm}{qty_cell}{spark}'
            f'{last_cell}{pct_cell}{wt_cell}{exit_line}</div>')


def _cash_row_v2(cash, settled) -> str:
    """A cash row in the same .pos-list grid (empty spark), matching the design's
    muted cash treatment. `settled` shown as the sub-label when present."""
    if not isinstance(cash, (int, float)):
        return ""
    sub = (f'<small style="color:var(--ink-3)">settled ${settled:,.2f}</small>'
           if isinstance(settled, (int, float)) else "<small>uninvested</small>")
    return (f'<div class="row cash"><div class="avatar">$</div>'
            f'<div class="nm">Cash{sub}</div>'
            f'<div class="qty"></div><div>{_spark_svg([])}</div>'
            f'<div class="last">${cash:,.2f}</div>'
            f'<div class="pct"><span class="muted">&mdash;</span></div>'
            f'<div class="wt">&mdash;<small>cash</small></div></div>')


def _home_html(win: str = "all") -> str:
    """HOME (Portfolio): hero NAV (big number + first->last delta + a source pill
    + time), the equity chart, then the position list (one row each, plus a cash
    row). Data fresh each request via `_portfolio_data_v2()`."""
    d = _portfolio_data_v2()
    equity = d.get("equity")
    cash = d.get("cash")
    settled = d.get("settled")
    positions = d.get("positions") or []
    source = d.get("source") or "snapshot"
    ts = d.get("ts") or ""

    # Hero delta from the same equity history that feeds the chart, windowed to
    # match the selected timeframe so the headline tracks the chart.
    win_l = (win or "all").lower()
    pts = _filter_window(_equity_history(), win_l)
    if pts:
        first, last = pts[0][1], pts[-1][1]
        delta = last - first
        pct = (delta / first * 100.0) if first else 0.0
    else:
        delta, pct = 0.0, 0.0
    dcls = "pos" if delta >= 0 else "neg"
    arrow = "&#9652;" if delta >= 0 else "&#9662;"
    sign = "+" if delta >= 0 else "-"
    span_lbl = {"today": "today", "1w": "this week", "1m": "this month",
                "3m": "3 months", "ytd": "YTD", "all": "all time"}.get(win_l, "all time")

    nav_s = f"${equity:,.2f}" if isinstance(equity, (int, float)) else "$&mdash;"
    src_cls = "src src-live" if source == "live" else "src"
    live_dot = '<span class="live"></span>' if source == "live" else ""
    ts_s = f" {escape(ts)}" if ts else ""
    src_pill = f'<span class="{src_cls}">{live_dot}{escape(source)}{ts_s}</span>'

    cash_meta = ""
    if isinstance(cash, (int, float)) and isinstance(equity, (int, float)) and equity:
        cash_meta = (f'cash <b>${cash:,.2f}</b> ({cash / equity * 100:.0f}%)'
                     f'<span class="sep">&middot;</span>')
    elif isinstance(cash, (int, float)):
        cash_meta = f'cash <b>${cash:,.2f}</b><span class="sep">&middot;</span>'
    n_pts = len(_equity_history())
    meta = (f'<div class="meta">{cash_meta}'
            f'<b>{n_pts}</b> data points across the run</div>')

    # Invested-vs-cash split. invested = equity - cash (positions value). Show a thin
    # split bar (accent = invested, muted = cash) + a $ / % caption. When equity is
    # missing or <=0 we can't compute shares -> caption-only (dollar amounts, no bar).
    alloc = ""
    if isinstance(equity, (int, float)) and isinstance(cash, (int, float)):
        invested = equity - cash
        inv_s = escape(f"${invested:,.2f}")
        cash_s = escape(f"${cash:,.2f}")
        if equity > 0:
            inv_pct = max(0.0, min(100.0, invested / equity * 100.0))
            cash_pct = max(0.0, min(100.0, cash / equity * 100.0))
            bar = (f'<div class="alloc-bar">'
                   f'<div class="alloc-seg invested" style="width:{inv_pct:.2f}%"></div>'
                   f'<div class="alloc-seg cash" style="width:{cash_pct:.2f}%"></div></div>')
            cap = (f'<div class="alloc-cap">Invested <b>{inv_s}</b> ({inv_pct:.0f}%)'
                   f'<span class="sep">&middot;</span>Cash <b>{cash_s}</b> ({cash_pct:.0f}%)</div>')
        else:
            bar = ""
            cap = (f'<div class="alloc-cap">Invested <b>{inv_s}</b>'
                   f'<span class="sep">&middot;</span>Cash <b>{cash_s}</b></div>')
        alloc = f'<div class="alloc">{bar}{cap}</div>'

    chart = _equity_chart_svg_v2(win_l)
    hero = (
        '<div class="wrap"><div class="home-hero">'
        f'<div class="label">{src_pill}<span>Net account value</span></div>'
        '<div class="nav-row"><div class="nav-figure">'
        f'<span class="num">{nav_s}</span>'
        f'<span class="delta {dcls}">'
        f'<span class="pill">{arrow} ${abs(delta):,.2f}</span>'
        f'<span>{sign}{abs(pct):.2f}% {span_lbl}</span></span>'
        '</div>'
        f'{_win_pills(win_l)}'
        '</div>'
        f'{chart}{alloc}{meta}'
        '</div></div>'
    )

    if not positions and not isinstance(cash, (int, float)):
        body = ('<div class="wrap"><div class="pos-empty">No open positions, and '
                'no snapshot yet. Run the autonomous loop while the market is open.'
                '</div></div>')
    else:
        _hyp = _read_json("hypotheses.json", {}) or {}
        rows = "".join(_position_row_v2(p, _hyp.get(p.get("symbol") or "")) for p in positions)
        rows += _cash_row_v2(cash, settled)
        if not rows:
            rows = '<div class="pos-empty">No open positions.</div>'
        body = f'<div class="wrap"><div class="pos-list">{rows}</div></div>'
    return hero + body


# --- Presentation-neutral helpers (copied verbatim from v1 dashboard.py). The
# detail dialog CSS is restyled to the light theme inside _CSS_V2 above; the
# markup/JS below is unchanged so the same act-data + showAct(i) pattern works. ---

def _md_lite(txt: str) -> str:
    """Tiny, safe markdown render for the Playbook mind-view: headings, bullets, and
    wrapped paragraphs. Everything is escaped first -- no raw HTML passes through."""
    out = []
    for ln in txt.splitlines():
        s = ln.rstrip()
        if not s:
            out.append('<div style="height:8px"></div>')
        elif s.startswith("## "):
            out.append(f'<h2 style="margin:14px 0 4px">{escape(s[3:])}</h2>')
        elif s.startswith("# "):
            out.append(f'<h1 style="margin:8px 0 6px">{escape(s[2:])}</h1>')
        elif s.startswith(("- ", "* ")):
            out.append(f'<div class="row" style="padding:2px 0">&bull; {escape(s[2:])}</div>')
        else:
            out.append(f'<p style="margin:4px 0;white-space:pre-wrap">{escape(s)}</p>')
    return "\n".join(out)


def _json_for_html(obj) -> str:
    """JSON-encode for safe embedding inside a <script type=application/json>
    block: neutralize <, >, & so the payload can't break out of the tag/HTML."""
    s = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    return s.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


_ACTIVITY_DIALOG = """<dialog id="dlg"><div class="dlg-hd"><b id="dlg-title"></b>
<button class="dlg-x" type="button" onclick="document.getElementById('dlg').close()">&times;</button></div>
<div class="dlg-bd" id="dlg-body"></div></dialog>
<script>
var _act = JSON.parse(document.getElementById('act-data').textContent);
function _esc(s){return String(s).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
function _lbl(k){var u={for:'For',against:'Against',why:'Why',decision:'Decision',rsi:'RSI',
order_id:'Order ID',ts:'Time',pnl:'P&L'};k=String(k);
if(u[k.toLowerCase()])return u[k.toLowerCase()];
return k.replace(/_/g,' ').replace(/\\b\\w/g,function(m){return m.toUpperCase();});}
function _empty(v){return v==null||v===''||(Array.isArray(v)&&!v.length)
||(typeof v==='object'&&!Array.isArray(v)&&v&&!Object.keys(v).length);}
function _val(k,v){
if(Array.isArray(v))return '<div class="kv-chips">'+v.map(function(x){
return '<span class="tag chipv">'+_esc(x)+'</span>';}).join('')+'</div>';
if(v&&typeof v==='object')return '<div class="kv-nest">'+_obj(v)+'</div>';
var cls='kv-v',kl=String(k).toLowerCase();
if(kl==='for')cls+=' v-for';else if(kl==='against')cls+=' v-against';
return '<div class="'+cls+'">'+_esc(v)+'</div>';}
function _obj(o){var h='';for(var k in o){if(!o.hasOwnProperty(k)||_empty(o[k]))continue;
h+='<div class="kv"><div class="kv-k">'+_esc(_lbl(k))+'</div>'+_val(k,o[k])+'</div>';}
return h||'<div class="muted">no details</div>';}
function showAct(i){var d=_act[i];if(!d)return;
document.getElementById('dlg-title').textContent=d.title;
document.getElementById('dlg-body').innerHTML=_obj(d.detail||{});
var dlg=document.getElementById('dlg');if(dlg.showModal){dlg.showModal();}else{dlg.setAttribute('open','');}}
</script>"""


def _pane_head(eyebrow: str, title_html: str, sub: str = "") -> str:
    """The design's pane header block (eyebrow + h1 + optional sub). title_html is
    inserted as-is so callers can embed a styled <span class="num"> count; it must
    already be escaped/safe."""
    sub_html = f'<p class="sub">{escape(sub)}</p>' if sub else ""
    return (f'<div class="pane-head"><div class="eyebrow">{escape(eyebrow)}</div>'
            f'<h1>{title_html}</h1>{sub_html}</div>')


# Map an order/activity status to the design's .kind pill variant (dec/amd/apv/
# flg/vto). Mirrors the intent of v1's _status_cls but emits v2 design classes.
def _kind_cls_v2(status: str) -> str:
    s = (status or "").upper()
    if s in ("FILLED", "BOUGHT", "BUY"):
        return "amd"
    if s in ("SUBMITTED", "PENDING", "ORDER", "OPEN", "ACCEPTED"):
        return "dec"
    if s in ("REJECTED", "FAILED", "ERROR", "VETO", "VETOED"):
        return "flg"
    if s in ("SOLD", "SELL", "PARTIAL", "CANCELLED", "CANCELED", "EXPIRED"):
        return "vto"
    return "apv"


def _mind_html() -> str:
    """MIND: the live mind-view (state/mind/playbook.md, fallback state/playbook.md)
    rendered via the copied _md_lite -- the convictions, doubts, and feelings the run
    operated on, in plain language. Mirrors v1's _playbook_html. Read fresh each load."""
    head = _pane_head("The Mind", "What I'm thinking right now.",
                      "The current state the run wrote: convictions, doubts, what I'm "
                      "watching, and how I'm feeling. Rewritten each run.")
    p = STATE / "mind" / "playbook.md"
    if not p.exists():
        legacy = STATE / "playbook.md"
        if legacy.exists():
            p = legacy
    if not p.exists():
        body = ('<div class="card muted">No Playbook yet. The mind writes its current '
                'state here each run -- what it is thinking, watching, doubting, and how '
                'it is feeling -- once the autonomous cycle has run.</div>')
    else:
        try:
            txt = p.read_text()
        except OSError:
            txt = ""
        body = f'<div class="mind-doc">{_md_lite(txt)}</div>'
    return f'<div class="wrap wrap--narrow">{head}{body}</div>'


def _shared_why(e: dict) -> str:
    """One-line 'why' for a shared watch entry, mirroring the own-watchlist row: a buy
    level for a trigger, or what it's watching for on a monitor, plus the target."""
    kind = str(e.get("kind") or ("trigger" if e.get("level") is not None else "monitor"))
    if kind == "monitor":
        wf = str(e.get("watch_for") or "")
        why = f"watching for: {escape(wf)}" if wf else "monitoring"
    else:
        cond = str(e.get("condition") or "")
        word = {"at_or_above": "buy at or above", "at_or_below": "buy at or below"}.get(
            cond, escape(cond) if cond else "buy near")
        lvl = e.get("level")
        why = f"{word} <b>${escape(str(lvl))}</b>" if lvl is not None else word
    tgt = e.get("target")
    if tgt is not None:
        why += f' &mdash; target <b>${escape(str(tgt))}</b>'
    return why


def _watch_share_control() -> tuple:
    """Small top-right control for the Watch tab: a Share setting (Only me / Friends),
    gated on shared_metadata access, plus a View picker (Me / a friend) when others are
    sharing. Returns (control_html, selected_username, viewing_friend). Cache-only, no git.
    The View picker drives which watchlist the tab body shows; viewing_friend is True only
    when a friend (not the owner) is selected."""
    if shared_metadata is None:
        return "", "", False
    try:
        has_access = shared_metadata.has_access()
        me = shared_metadata.my_username()
        mode = shared_metadata.share_mode()
    except Exception:
        has_access, me, mode = False, "", "only_me"
    if not has_access:
        return ('<div class="watch-ctrl"><span class="share-lbl">Share</span>'
                '<span class="share-pill off" title="No access to the shared repo">Only me</span></div>',
                me, False)
    try:
        users = shared_metadata.usernames()
    except Exception:
        users = []
    ordered = ([me] if me else []) + [u for u in users if u != me]
    sel = _SHARE_SELECTED if _SHARE_SELECTED in ordered else me
    viewing_friend = bool(sel and me and sel != me)
    sopts = "".join(f'<option value="{v}"{" selected" if mode == v else ""}>{escape(lbl)}</option>'
                    for v, lbl in (("only_me", "Only me"), ("friends", "Friends")))
    share_sel = (f'<select class="share-sel" title="Who can see your watchlist" '
                 f'onchange="location.href=\'/api/share_watchlist?mode=\'+this.value">{sopts}</select>')
    if ordered:
        vopts = "".join(
            f'<option value="{escape(u)}"{" selected" if u == sel else ""}>'
            f'{escape(u) + (" (me)" if u == me else "")}</option>' for u in ordered)
        view = ('<span class="share-lbl">View</span>'
                '<form method="get" action="/" class="view-form">'
                '<input type="hidden" name="tab" value="watch">'
                f'<select name="who" title="Whose watchlist to view" onchange="this.form.submit()">{vopts}</select>'
                '</form>')
        if len(ordered) <= 1:
            view += '<span class="share-hint">no friends sharing yet</span>'
    else:
        view = '<span class="share-hint">sign in to gh to browse</span>'
    ctrl = (f'<div class="watch-ctrl">{view}<span class="share-lbl">Share</span>{share_sel}'
            f'<a class="share-refresh" href="/api/refresh_shared" title="Refresh friends">&#8635;</a></div>')
    return ctrl, sel, viewing_friend


def _shared_rows_html(entries: list, sel: str, own_tickers: set) -> str:
    """A friend's shared (scrubbed) watch entries as rows, each with 'Add to my watchlist'
    unless the ticker is already on the owner's list (compared by NAME only)."""
    if not entries:
        return '<div class="card muted">Nothing shared here yet.</div>'
    rows = ""
    for e in entries:
        sym = str(e.get("ticker") or "?")
        if sym.upper() in own_tickers:
            action = '<span class="shared-have">on your list</span>'
        else:
            action = (f'<a class="shared-add" '
                      f'href="/api/add_from_shared?username={escape(sel)}&ticker={escape(sym)}">'
                      f'Add to my watchlist</a>')
        rows += (f'<div class="row"><div class="avatar">{_avatar_text(sym)}</div>'
                 f'<div class="body"><div class="sym">{escape(sym)}</div>'
                 f'<div class="why">{_shared_why(e)}</div></div>{action}</div>')
    return f'<div class="shared-list">{rows}</div>'


# The username selected in the browse panel, threaded in from the request's ?who=.
# Set per-request in render_page before _watch_html() runs; reset to "" otherwise.
_SHARE_SELECTED = ""


def _safe_refresh() -> None:
    """Background clone-or-pull of the shared repo (TTL-gated). Swallows everything so a
    failed network call never surfaces; the cached access result just stays as it was."""
    try:
        shared_metadata.refresh()
    except Exception:
        pass


def _add_shared_entry(entry: dict) -> None:
    """Add a friend's shared (scrubbed) watch entry to the owner's own autonomous
    watchlist, preserving its WHOLE thesis (target, stop, hypothesis, notes, trend).
    Routes to the watchlist's trigger or monitor upsert by the entry's kind, so adds
    flow through the same API (and dashboard-mirror sync) as the mind's own adds."""
    ticker = str(entry.get("ticker") or "")
    if not ticker:
        return
    kind = str(entry.get("kind") or ("trigger" if entry.get("level") is not None else "monitor"))
    common = dict(hypothesis=str(entry.get("hypothesis") or ""),
                  expected_trend=str(entry.get("expected_trend") or ""),
                  target=entry.get("target"), stop=entry.get("stop"),
                  notes=str(entry.get("notes") or ""))
    cond = entry.get("condition")
    if kind == "trigger" and cond in ("at_or_below", "at_or_above") and entry.get("level") is not None:
        autonomous_watchlist.add(ticker, cond, entry["level"], **common)
    else:
        autonomous_watchlist.watch(ticker, str(entry.get("watch_for") or "shared idea"), **common)


def _watch_html() -> str:
    """WATCH: the trader's OWN watchlist (state/watching.json), or a friend's shared
    watchlist when one is picked in the small top-right View control. Own rows are
    clickable for the full hypothesis/target/stop; a friend's rows offer 'Add to my
    watchlist'. Selecting a friend swaps the body to theirs (not both). Read fresh."""
    data = _read_json("watching.json", {})
    items = ([w for w in (data.get("watching") or []) if isinstance(w, dict)]
             if isinstance(data, dict) else [])
    # Tickers already on the owner's list (by NAME only), from the rendered mirror and the
    # live autonomous watchlist, so a just-added name counts.
    own_tickers = {str(w.get("ticker")).upper() for w in items if w.get("ticker")}
    try:
        own_tickers |= {str(e.get("ticker")).upper() for e in autonomous_watchlist.load()
                        if isinstance(e, dict) and e.get("ticker")}
    except Exception:
        pass
    ctrl, sel, viewing_friend = _watch_share_control()

    # A friend is selected: show ONLY their shared list, with their name in the head.
    if viewing_friend:
        try:
            entries = shared_metadata.read_watchlist(sel)
        except Exception:
            entries = []
        n = len(entries)
        fhead = _pane_head("Watching",
                           f'<b>{escape(sel)}</b> is watching <span class="num">{n}</span> ticker{"" if n == 1 else "s"} right now.',
                           f"{escape(sel)}&rsquo;s shared watchlist. Add any name you do not already track.")
        return (f'<div class="wrap wrap--narrow"><div class="watch-top">{fhead}{ctrl}</div>'
                f'{_shared_rows_html(entries, sel, own_tickers)}</div>')

    head = _pane_head("Watching",
                      f'I&rsquo;m watching <span class="num">{len(items)}</span> tickers right now.',
                      "Names I'm tracking to enter at a level or just to monitor a thesis. "
                      "Click a row for the full hypothesis, target, and stop.")
    if not items:
        body = ('<div class="card muted">Nothing on the watchlist yet. The mind adds '
                'names here as it forms entry theses.</div>')
        return f'<div class="wrap wrap--narrow"><div class="watch-top">{head}{ctrl}</div>{body}</div>'

    payload = []  # per-row detail, indexed parallel to rendered rows
    rows = ""
    for i, w in enumerate(items):
        if not isinstance(w, dict):
            continue
        sym = str(w.get("ticker") or "?")
        level = w.get("level")
        kind = str(w.get("kind") or ("trigger" if level is not None else "monitor"))
        cond = str(w.get("condition") or "")
        last = w.get("last")
        ready = bool(w.get("ready"))
        held = bool(w.get("already_held"))
        trend = str(w.get("expected_trend") or "")
        watch_for = str(w.get("watch_for") or "")

        # Status tag + the "why" line differ by kind. Monitor entries have no price
        # trigger, so they show what they are watching for instead of a buy level.
        if kind == "monitor":
            tag = '<span class="tag">Monitoring</span>'
            why = (f'watching for: {escape(watch_for)}' if watch_for else 'monitoring')
            trig_label, trig_val = "watch", "&mdash;"
        else:
            if cond == "at_or_above":
                cond_word = "buy at or above"
            elif cond == "at_or_below":
                cond_word = "buy at or below"
            else:
                cond_word = escape(cond) if cond else "buy near"
            lvl_txt = f"${escape(str(level))}" if level is not None else ""
            why = f'{cond_word} <b>{lvl_txt}</b>' if lvl_txt else cond_word
            if trend:
                why += f' &mdash; {escape(trend)}'
            if ready:
                tag = '<span class="tag ready">Ready</span>'
            else:
                tag = '<span class="tag wait">Waiting</span>'
            trig_label = "trigger"
            trig_val = f"${escape(str(level))}" if level is not None else "&mdash;"
        held_tag = '<span class="tag">held</span>' if held else ""
        last_val = f"${escape(str(last))}" if last is not None else "&mdash;"

        rows += (
            f'<button type="button" class="rowbtn" onclick="showAct({i})">'
            f'<div class="row">'
            f'<div class="avatar">{_avatar_text(sym)}</div>'
            f'<div class="body"><div class="top"><span class="sym">{escape(sym)}</span>'
            f'{tag}{held_tag}</div><div class="why">{why}</div></div>'
            f'<div class="last">last<b>{last_val}</b></div>'
            f'<div class="trigger">{trig_label}<b>{trig_val}</b></div>'
            f'</div></button>'
        )
        payload.append({"title": f"Watching {sym}", "detail": {
            "ticker": sym,
            "kind": kind,
            "watch_for": watch_for,
            "condition": cond,
            "level": level,
            "last": last,
            "ready": ready,
            "already_held": held,
            "expected_trend": trend,
            "hypothesis": w.get("hypothesis", ""),
            "target": w.get("target"),
            "stop": w.get("stop"),
            "notes": w.get("notes", ""),
            "added": w.get("added", ""),
        }})

    body = (f'<div class="watch-list">{rows}</div>'
            f'<script type="application/json" id="act-data">{_json_for_html(payload)}</script>'
            + _ACTIVITY_DIALOG)
    updated = data.get("updated") if isinstance(data, dict) else ""
    if updated:
        body += f'<div class="pg"><span class="muted">updated {escape(_fmt_ts(updated))}</span></div>'
    return f'<div class="wrap wrap--narrow"><div class="watch-top">{head}{ctrl}</div>{body}</div>'


def _execution_steps(idea_type: str) -> dict:
    """Plain-language how-to-place steps per idea type, for Robinhood and public.com.
    Intermediate-trader level, no heavy jargon. Robinhood rules baked in: one open
    sell order per stock (a stop OR a price-alert-for-the-target, never both); options
    and short options have NO stop orders (use a price alert plus Sell/Buy to Close)."""
    t = (idea_type or "").lower()
    long_ = {
        "robinhood": [
            "Search the ticker, tap Trade, then Buy.",
            "Enter the amount in dollars or shares (Robinhood does fractional shares).",
            "Pick Limit (the most you will pay) to stay in control, or Market to fill right now.",
            "Review and swipe up to submit.",
            "Protect it: once it fills, set ONE Stop Loss sell order at your stop. For the target, set a price Alert (not a second sell order) -- Robinhood allows only one open sell order per position, so you sell by hand when the alert fires.",
        ],
        "public": [
            "Search the ticker, tap Buy.",
            "Enter dollars (public.com does fractional) or shares.",
            "Pick Limit or Market, review, submit.",
            "Protect it: set a Stop order at your stop and a price alert at your target.",
        ],
    }
    call_put = {
        "robinhood": [
            "Open the ticker, tap Trade, then Trade Options.",
            "Pick the expiration first (give it room, e.g. 2-3 months out), then tap the strike.",
            "Make sure it says Buy and the right side (Call or Put). This is Buy to Open.",
            "Enter contracts (1 contract = 100 shares of exposure), set a Limit on the premium you will pay, review, submit.",
            "Managing it: options have NO stop orders in Robinhood. Set a price Alert on the stock instead. To get out, Sell to Close with a Limit.",
        ],
        "public": [
            "Open the ticker, tap Options.",
            "Pick the expiration, then the strike. Choose Buy and the right side (Call or Put).",
            "Set a Limit on the premium, review, submit.",
            "To exit: Sell to Close. Watch it with a price alert on the stock.",
        ],
    }
    debit = {
        "robinhood": [
            "Open the ticker, tap Trade, Trade Options, pick the expiration.",
            "Use the strategy builder and choose the debit spread, or build it: Buy the closer strike and Sell the farther strike (same expiration).",
            "You pay a net debit (less than buying the option outright). Set the Limit at that net price, submit.",
            "Your math: max loss = what you paid; max gain = the gap between the two strikes minus what you paid. Risk is defined both ways.",
            "To exit: close the whole spread in one order, or let it settle at expiration.",
        ],
        "public": [
            "Open the ticker, tap Options, pick the expiration.",
            "Choose the debit spread (public.com supports spreads): buy the closer strike, sell the farther one.",
            "Set the Limit at the net debit, review, submit.",
            "Exit by closing the spread.",
        ],
    }
    credit = {
        "robinhood": [
            "Open the ticker, tap Trade, Trade Options, pick the expiration.",
            "Choose the credit spread: Sell the strike near the price and Buy the farther strike for protection (a put credit spread is the bullish one, a call credit spread is bearish).",
            "You collect a net credit. Review, submit.",
            "Your math: max gain = the credit you collect; max loss = the gap between strikes minus the credit.",
            "Managing it: this is a short option, so Robinhood has NO stop orders here. Set a price Alert on the stock at your bail-out level. To exit, Buy to Close the spread with a Limit.",
        ],
        "public": [
            "Open the ticker, tap Options, pick the expiration.",
            "Choose the credit spread (public.com supports these): sell the near strike, buy the farther one for protection.",
            "Collect the credit, review, submit.",
            "Exit by Buying to Close the spread; use a price alert on the stock to watch your risk.",
        ],
    }
    if t == "long":
        return long_
    if t in ("call", "put"):
        return call_put
    if t == "debit_spread":
        return debit
    if t == "credit_spread":
        return credit
    return {"robinhood": ["Place it as a standard order on the ticker; set a stop or a price alert to manage it."],
            "public": ["Place it as a standard order on the ticker; set a stop or a price alert to manage it."]}


def _conviction_html() -> str:
    """CONVICTION: the mind's unconstrained shadow ideas (state/mind/conviction_board.json)
    -- what it would trade with no capital or position limits, including options and
    spreads. Shadow only, never executed. Each idea is a clickable .conv-item that
    expands to the thesis, the structure, why it stays shadow, and plain how-to-place
    steps for Robinhood and public.com. Read fresh."""
    data = _read_json("mind/conviction_board.json", {})
    ideas = data.get("ideas") or [] if isinstance(data, dict) else []
    note = (data.get("note") if isinstance(data, dict) else "") or (
        "Unconstrained conviction -- what I would trade with no capital limits. Shadow only.")
    as_of = (data.get("as_of") if isinstance(data, dict) else "") or ""
    head = _pane_head("Conviction board",
                      f'I&rsquo;m carrying <span class="num">{len([d for d in ideas if isinstance(d, dict)])}</span> shadow ideas.',
                      note + (f" As of {as_of}." if as_of else ""))
    if not ideas:
        body = ('<div class="card muted">No conviction-board ideas yet. The mind writes '
                'these each substantive run.</div>')
        return f'<div class="wrap wrap--narrow">{head}{body}</div>'

    # Conviction -> the existing watch tag color classes.
    _conv_cls = {"high": "ready", "medium": "wait", "speculative": "armed"}
    rows = ""
    for idea in ideas:
        if not isinstance(idea, dict):
            continue
        sym = str(idea.get("ticker") or "?")
        typ = str(idea.get("type") or "")
        conv = str(idea.get("conviction") or "")
        thesis = str(idea.get("thesis") or "")
        structure = str(idea.get("structure") or "")
        why_shadow = str(idea.get("why_shadow") or "")

        type_tag = f'<span class="tag">{escape(typ)}</span>' if typ else ""
        conv_cls = _conv_cls.get(conv.lower(), "wait")
        conv_tag = f'<span class="tag {conv_cls}">{escape(conv)}</span>' if conv else ""

        steps = _execution_steps(typ)
        rh_items = "".join(f"<li>{escape(s)}</li>" for s in steps.get("robinhood", []))
        pub_items = "".join(f"<li>{escape(s)}</li>" for s in steps.get("public", []))

        plan = (f'<div class="conv-plan"><b>The plan:</b> {escape(structure)}</div>'
                if structure else "")
        shadow = (f'<div class="muted">Shadow-only: {escape(why_shadow)}</div>'
                  if why_shadow else "")

        rows += (
            f'<details class="conv-item">'
            f'<summary class="conv-row">'
            f'<span class="avatar">{_avatar_text(sym)}</span>'
            f'<span class="conv-sum"><span class="sym">{escape(sym)}</span> {type_tag}{conv_tag}'
            f'<span class="why">{escape(thesis)}</span></span>'
            f'<span class="conv-caret">+</span>'
            f'</summary>'
            f'<div class="conv-detail">'
            f'{plan}{shadow}'
            f'<div class="conv-how"><h4>How to place it in Robinhood</h4><ol>{rh_items}</ol></div>'
            f'<div class="conv-how"><h4>How to place it in public.com</h4><ol>{pub_items}</ol></div>'
            f'</div>'
            f'</details>'
        )

    body = f'<div class="conv-list">{rows}</div>'
    updated = data.get("updated") if isinstance(data, dict) else ""
    if updated:
        body += f'<div class="pg"><span class="muted">updated {escape(_fmt_ts(updated))}</span></div>'
    return f'<div class="wrap wrap--narrow">{head}{body}</div>'


def _signed_dollars(v) -> str:
    """Format a dollar amount as a signed whole-dollar string: '+$120', '-$45',
    '$0'. None/unparseable -> '$0'. Used by the shadow-trade views."""
    n = _num(v)
    if n is None:
        return "$0"
    n = round(n)
    if n == 0:
        return "$0"
    sign = "+" if n > 0 else "-"
    return f"{sign}${abs(int(n)):,}"


def _pnl_cls(v) -> str:
    """'pos' / 'neg' / '' class for a P&L value, by sign."""
    n = _num(v)
    if n is None or round(n) == 0:
        return ""
    return "pos" if n > 0 else "neg"


def _signed_pct(pnl, notional) -> str:
    """P&L as a percent of notional, signed (e.g. '+5.0%'). '' if not computable."""
    p = _num(pnl)
    base = _num(notional)
    if p is None or not base:
        return ""
    pct = p / base * 100.0
    sign = "+" if pct >= 0 else "-"
    return f"{sign}{abs(pct):.1f}%"


def _shadow_html() -> str:
    """SHADOW TRADES: the paper-trade book the Shadow Trader keeps for the
    conviction stocks (shadow_trades.summary()). No real money -- this is the
    track record of the convictions. Renders a per-stock profit table (the key
    view) plus open and closed trade lists. Read fresh each load."""
    head = _pane_head(
        "Shadow trades", "",
        "Paper trades the Shadow Trader opens and closes for the conviction "
        "stocks. No real money -- this is the track record of the convictions.")
    empty = ('<div class="card muted">No shadow trades yet. The Shadow Trader '
             'opens these from the conviction board on each run.</div>')
    try:
        import shadow_trades
        s = shadow_trades.summary()
    except Exception:
        s = None
    if not s or not isinstance(s, dict):
        return f'<div class="wrap wrap--narrow">{head}{empty}</div>'

    per_stock = s.get("per_stock") or {}
    open_trades = [t for t in (s.get("open") or []) if isinstance(t, dict)]
    closed_trades = [t for t in (s.get("closed") or []) if isinstance(t, dict)]
    if not per_stock and not open_trades and not closed_trades:
        return f'<div class="wrap wrap--narrow">{head}{empty}</div>'

    totals = s.get("totals") or {}
    t_windows = totals.get("windows") or {}
    all_total = t_windows.get("all")
    open_unreal = totals.get("open_unrealized")
    sub = (f'All-time {_signed_dollars(all_total)}, '
           f'open {_signed_dollars(open_unreal)}')
    head = _pane_head(
        "Shadow trades", escape(sub),
        "Paper trades the Shadow Trader opens and closes for the conviction "
        "stocks. No real money -- this is the track record of the convictions.")

    # --- Profit by stock (the key view): one row per ticker, dollar windows. ---
    body_rows = ""
    for tk in sorted(per_stock):
        ps = per_stock.get(tk) or {}
        w = ps.get("windows") or {}
        cells = ""
        for key in ("day", "week", "month", "year", "all"):
            val = w.get(key)
            cells += (f'<td class="num {_pnl_cls(val)}">'
                      f'{escape(_signed_dollars(val))}</td>')
        unreal = ps.get("unrealized")
        cells += (f'<td class="num {_pnl_cls(unreal)}">'
                  f'{escape(_signed_dollars(unreal))}</td>')
        body_rows += f'<tr><td class="sym-cell">{escape(str(tk))}</td>{cells}</tr>'
    if body_rows:
        table = (
            '<div class="section-label">Profit by stock</div>'
            '<table class="shadow-table"><thead><tr>'
            '<th>Ticker</th><th class="num">Day</th><th class="num">Week</th>'
            '<th class="num">Month</th><th class="num">Year</th>'
            '<th class="num">All</th><th class="num">Open</th>'
            f'</tr></thead><tbody>{body_rows}</tbody></table>')
    else:
        table = ""

    # --- Open trades: avatar + ticker + type tag + thesis, entry->last + P&L. ---
    open_html = ""
    if open_trades:
        rows = ""
        for t in open_trades:
            sym = str(t.get("ticker") or "?")
            typ = str(t.get("type") or "")
            thesis = str(t.get("thesis") or "")
            entry = t.get("entry")
            last = t.get("last_price")
            pnl = t.get("unrealized_pnl")
            type_tag = (f'<span class="tag">{escape(typ)}</span>' if typ else "")
            entry_txt = f"${escape(str(entry))}" if entry is not None else "&mdash;"
            last_txt = f"${escape(str(last))}" if last is not None else "&mdash;"
            pct = _signed_pct(pnl, t.get("notional"))
            pct_sub = (f'<small>{escape(pct)}</small>' if pct else "")
            rows += (
                f'<div class="row">'
                f'<div class="avatar">{_avatar_text(sym)}</div>'
                f'<div class="body"><div class="top">'
                f'<span class="sym">{escape(sym)}</span>{type_tag}</div>'
                f'<div class="why">{escape(thesis)}</div></div>'
                f'<div class="last">{entry_txt} &rarr;<b>{last_txt}</b></div>'
                f'<div class="trigger {_pnl_cls(pnl)}">P&amp;L'
                f'<b>{escape(_signed_dollars(pnl))}</b>{pct_sub}</div>'
                f'</div>')
        open_html = (f'<div class="section-label">Open</div>'
                     f'<div class="watch-list">{rows}</div>')

    # --- Closed trades: ticker + type + dates, entry->exit, realized P&L, why. ---
    closed_html = ""
    if closed_trades:
        rows = ""
        for t in closed_trades:
            sym = str(t.get("ticker") or "?")
            typ = str(t.get("type") or "")
            thesis = str(t.get("close_reason") or "")
            opened = str(t.get("opened") or "")
            closed = str(t.get("closed") or "")
            entry = t.get("entry")
            exit_ = t.get("exit")
            pnl = t.get("realized_pnl")
            type_tag = (f'<span class="tag">{escape(typ)}</span>' if typ else "")
            dates = " &rarr; ".join(escape(x) for x in (opened, closed) if x)
            entry_txt = f"${escape(str(entry))}" if entry is not None else "&mdash;"
            exit_txt = f"${escape(str(exit_))}" if exit_ is not None else "&mdash;"
            pct = _signed_pct(pnl, t.get("notional"))
            pct_sub = (f'<small>{escape(pct)}</small>' if pct else "")
            rows += (
                f'<div class="row">'
                f'<div class="avatar">{_avatar_text(sym)}</div>'
                f'<div class="body"><div class="top">'
                f'<span class="sym">{escape(sym)}</span>{type_tag}'
                f'<span class="tag wait">{dates}</span></div>'
                f'<div class="why">{escape(thesis)}</div></div>'
                f'<div class="last">{entry_txt} &rarr;<b>{exit_txt}</b></div>'
                f'<div class="trigger {_pnl_cls(pnl)}">realized'
                f'<b>{escape(_signed_dollars(pnl))}</b>{pct_sub}</div>'
                f'</div>')
        closed_html = (f'<div class="section-label">Closed</div>'
                       f'<div class="watch-list">{rows}</div>')

    body = table + open_html + closed_html
    if not body:
        body = empty
    return f'<div class="wrap wrap--narrow">{head}{body}</div>'


def _decide_html() -> str:
    """DECIDE: the queue of things the mind escalated for a human call. One
    .decide-hero .pending card per pending approval (category + summary + detail),
    each a GET form to /api/decide carrying a hidden id, a reason textarea, and
    Approve / Reject buttons. Below, a .decide-history list of the last ~10 decided
    items (status pill + reasoning). Mirrors v1's _decisions_html, repainted to the
    design's decide pane. Read fresh each load."""
    try:
        pend = approvals.pending()
    except Exception:
        pend = []
    n = len([a for a in pend if isinstance(a, dict)])
    title = ("Nothing needs you." if n == 0
             else ("One decision is open." if n == 1
                   else f'<span class="num">{n}</span> decisions are open.'))
    head = _pane_head("Needs you", title,
                      "Calls the mind escalated for your judgment. Approve or reject, "
                      "and add a reason or a custom instruction.")

    if not pend:
        body = ('<div class="card muted">Nothing needs you right now. When the mind '
                'hits a call it wants a human on -- a sizing exception, a guardrail '
                'change -- it shows up here.</div>')
    else:
        cards = ""
        for a in pend:
            if not isinstance(a, dict):
                continue
            cat = escape(str(a.get("category", "") or "decision"))
            summ = escape(str(a.get("summary", "")))
            detail = escape(str(a.get("detail", "")))
            rid = escape(str(a.get("id", "")))
            detail_html = f'<p class="body">{detail}</p>' if detail else ""
            cards += (
                '<div class="pending">'
                f'<div class="kind"><span class="live"></span>{cat}</div>'
                f'<h1>{summ}</h1>{detail_html}'
                '<form method="GET" action="/api/decide" class="decide-form">'
                f'<input type="hidden" name="id" value="{rid}">'
                '<textarea name="reason" rows="2" '
                'placeholder="Reasoning, or a custom instruction (optional)"></textarea>'
                '<div class="acts">'
                '<button class="btn good" type="submit" name="decision" value="approved">'
                '&#10003; Approve</button>'
                '<button class="btn bad" type="submit" name="decision" value="rejected">'
                '&#10007; Reject</button>'
                '</div></form></div>'
            )
        body = f'<div class="decide-hero">{cards}</div>'

    # Recent decided items (newest first), last ~10.
    try:
        decided = [a for a in approvals.all_items()
                   if isinstance(a, dict) and a.get("status") != "pending"]
    except Exception:
        decided = []
    decided.sort(key=lambda x: x.get("decided_ts") or "", reverse=True)
    if decided:
        rows = ""
        for a in decided[:10]:
            scls = {"approved": "apv", "rejected": "rej"}.get(
                str(a.get("status", "")).lower(), "exp")
            status = escape(str(a.get("status", "") or "decided")).title()
            summ = escape(str(a.get("summary", "")))
            reason = escape(str(a.get("reasoning", "") or ""))
            reason_html = f' <span class="muted">{reason}</span>' if reason else ""
            rows += (f'<div class="h-row"><div class="t">'
                     f'{escape(_fmt_ts(a.get("decided_ts") or ""))}</div>'
                     f'<div><span class="status {scls}">{status}</span>'
                     f'{summ}{reason_html}</div></div>')
        body += (f'<div class="decide-history"><h3>Recent decisions</h3>{rows}</div>')
    return f'<div class="wrap wrap--narrow">{head}{body}</div>'


def _controls_overlay_html() -> str:
    """The design's #tweaks overlay (a fixed aside opened by a header Controls
    button). Sections: Temperament (DIALS as 0-100 range sliders, GET /api/temperament,
    with the current summary), Accent swatches and Density radio (client-only,
    localStorage v2accent / v2density, no server). Temperament logic mirrors v1's
    _controls_html. Read fresh."""
    # Temperament dials.
    try:
        prof = temperament.load()
    except Exception:
        prof = {}
    dials = ""
    _group_titles = {"judgment": "Judgment", "voice": "Voice"}
    _seen_groups = set()
    for d in temperament.DIALS:
        g = d.get("group", "")
        if g and g not in _seen_groups:
            _seen_groups.add(g)
            dials += f'<div class="dial-group">{escape(_group_titles.get(g, g.title()))}</div>'
        v = prof.get(d["key"], 50)
        dials += (
            '<div class="dial">'
            f'<div class="dial-hd"><b>{escape(d["label"])}</b>'
            f'<span class="muted dial-steer">{escape(d["steers"])}</span></div>'
            '<div class="dial-row">'
            f'<span class="pole">{escape(d["low"])}</span>'
            f'<input type="range" min="0" max="100" name="{escape(d["key"])}" value="{v}" '
            "oninput=\"this.parentNode.querySelector('.dial-val').textContent=this.value\">"
            f'<span class="pole pole-hi">{escape(d["high"])}</span>'
            f'<span class="dial-val pill">{v}</span>'
            '</div></div>'
        )
    try:
        summ = escape(temperament.summary(prof))
    except Exception:
        summ = ""
    temperament_sec = (
        '<div class="tw-section"><div class="tw-label">Temperament</div>'
        '<p class="muted" style="margin:0 0 10px;font-size:12px">How I think, talk, '
        'and introspect. Shapes judgment and tone only -- never the math, caps, or '
        'guardrails.</p>'
        '<form method="GET" action="/api/temperament">'
        f'{dials}'
        '<button class="btn good" type="submit" style="margin-top:10px">Save temperament</button>'
        f'<div class="muted" style="margin-top:8px;font-size:12px">Now: {summ}</div>'
        '</form></div>'
    )

    # Accent + density (client-only).
    accents = (("green", "#10C24A", "Signal Green"), ("cobalt", "#2747e8", "Cobalt"),
               ("indigo", "#6e3d96", "Indigo"), ("copper", "#d97706", "Copper"),
               ("ink", "#1a1a1a", "Ink"))
    sw = "".join(
        f'<button type="button" data-val="{k}" style="background:{col}" title="{escape(t)}" '
        f'onclick="v2setAccent(\'{k}\')"></button>' for k, col, t in accents)
    accent_sec = ('<div class="tw-section"><div class="tw-label">Accent</div>'
                  f'<div class="tw-colors" id="tw-accent">{sw}</div></div>')
    dens = (("comfortable", "Comfortable"), ("compact", "Compact"), ("dense", "Dense"))
    db = "".join(
        f'<button type="button" data-val="{k}" onclick="v2setDensity(\'{k}\')">{escape(label)}</button>'
        for k, label in dens)
    density_sec = ('<div class="tw-section"><div class="tw-label">Density</div>'
                   f'<div class="tw-radio" id="tw-density">{db}</div></div>')

    js = """<script>
function v2setAccent(a){try{localStorage.setItem('v2accent',a);}catch(e){}
document.documentElement.dataset.accent=a;v2syncTw();}
function v2setDensity(d){try{localStorage.setItem('v2density',d);}catch(e){}
document.documentElement.dataset.density=d;v2syncTw();}
function v2syncTw(){var a=document.documentElement.dataset.accent||'green';
var d=document.documentElement.dataset.density||'compact';
document.querySelectorAll('#tw-accent button').forEach(function(b){
b.classList.toggle('on',b.dataset.val===a);});
document.querySelectorAll('#tw-density button').forEach(function(b){
b.classList.toggle('on',b.dataset.val===d);});}
function v2openTw(){document.getElementById('tweaks').hidden=false;v2syncTw();}
function v2closeTw(){document.getElementById('tweaks').hidden=true;}
document.addEventListener('click',function(e){var tw=document.getElementById('tweaks');if(!tw||tw.hidden)return;if(!tw.contains(e.target)&&!e.target.closest('.ctrl-btn'))v2closeTw();});
v2syncTw();
</script>"""

    return (
        '<aside class="tweaks" id="tweaks" hidden>'
        '<div class="tw-head"><span>Controls</span>'
        '<button type="button" aria-label="Close" onclick="v2closeTw()">&times;</button></div>'
        f'{temperament_sec}{accent_sec}{density_sec}'
        '</aside>'
        f'{js}'
    )


def _inbox_fab_html() -> str:
    """Instruction inbox dialog: a composer (textarea + tab dropdown + Send,
    POST /api/instruct) and the pending + archived lists. The dialog is opened by
    the "Instruct the mind" button in the glance header (`_glance_header`). The
    mind ingests pending items each run and archives them with an outcome. Mirrors
    v1's _inbox_html, repainted to the light theme. Read fresh each load."""
    try:
        items = instructions.all_items()
    except Exception:
        items = []
    items = [i for i in items if isinstance(i, dict)]
    pend = [i for i in items if i.get("status") == "pending"]
    arch = [i for i in items if i.get("status") == "processed"]
    tab_opts = ["Other", "Portfolio", "Controls", "Watching", "Activity",
                "Decisions", "Evolution", "Agents"]
    opts = "".join(f'<option value="{escape(t)}">{escape(t)}</option>' for t in tab_opts)

    def _item(i, processed):
        when = escape(_fmt_ts(i.get("ts", "")))
        tab = escape(str(i.get("tab", "Other")))
        text = escape(str(i.get("text", "")))
        meta = (f'<div class="ibx-meta"><span class="pill">{tab}</span>'
                f'<span class="muted">{when}</span></div>')
        imgs = ""
        img_list = i.get("images") or []
        if isinstance(img_list, list) and img_list:
            thumbs = "".join(
                f'<img class="ibx-thumb" src="/img/{escape(str(fn))}" alt="">'
                for fn in img_list if isinstance(fn, str) and fn)
            if thumbs:
                imgs = f'<div class="ibx-thumbs">{thumbs}</div>'
        out = ""
        if processed:
            oc = escape(str(i.get("outcome", "") or "processed"))
            ref = str(i.get("ref", "") or "")
            link = ' <a href="/?tab=decide">[decision]</a>' if ref else ""
            out = f'<div class="muted ibx-out">{oc}{link}</div>'
        return f'<div class="ibx-item">{meta}<div class="ibx-text">{text}</div>{imgs}{out}</div>'

    pend_html = ("".join(_item(i, False) for i in reversed(pend))
                 or '<div class="muted">Nothing pending.</div>')
    arch_html = ("".join(_item(i, True) for i in reversed(arch))
                 or '<div class="muted">No archived instructions yet.</div>')
    return (
        '<dialog id="ibx-dlg" class="ibx-dlg">'
        '<div class="dlg-hd"><b>Instruct the mind</b>'
        '<button class="dlg-x" type="button" '
        'onclick="document.getElementById(\'ibx-dlg\').close()">&times;</button></div>'
        '<div class="dlg-bd">'
        '<div class="muted" style="margin-bottom:8px">I pick this up on my next run, '
        'act or escalate it to Decide, then archive it.</div>'
        '<form class="ibx-form" method="POST" action="/api/instruct">'
        '<textarea name="text" id="ibx-text" rows="3" '
        'placeholder="Tell the mind something... (you can paste an image)"></textarea>'
        '<textarea name="images_b64" id="ibx-images" style="display:none"></textarea>'
        '<div id="ibx-thumbs" class="ibx-thumbs"></div>'
        f'<div class="ibx-row"><select name="tab">{opts}</select>'
        '<button type="submit">Send</button></div></form>'
        '<script>(function(){'
        "var ta=document.getElementById('ibx-text');"
        "var hid=document.getElementById('ibx-images');"
        "var strip=document.getElementById('ibx-thumbs');"
        'if(!ta||!hid||!strip)return;var arr=[];'
        "ta.addEventListener('paste',function(ev){"
        'var items=(ev.clipboardData||{}).items||[];'
        'for(var k=0;k<items.length;k++){var it=items[k];'
        "if(it.type&&it.type.indexOf('image/')===0){"
        'var blob=it.getAsFile();if(!blob)continue;'
        'var r=new FileReader();'
        'r.onload=function(e){var url=e.target.result;'
        'if(url.length>5*1024*1024*1.4){alert(\'image too large\');return;}'
        'arr.push(url);hid.value=JSON.stringify(arr);'
        "var img=document.createElement('img');img.className='ibx-thumb';"
        'img.src=url;strip.appendChild(img);};'
        'r.readAsDataURL(blob);}}});})();</script>'
        '<div class="ibx-sec">Pending</div>'
        f'{pend_html}'
        f'<details class="ibx-arch"><summary>Archived ({len(arch)})</summary>{arch_html}</details>'
        '</div></dialog>'
    )


def _log_html(page: int = 0) -> str:
    """LOG: the unified newest-first activity timeline (activity_events()), paginated
    PAGE_SIZE per page with newer/older nav. Each row is clickable -> showAct(i) with
    its detail dict; emits the act-data payload + shared dialog. Mirrors v1's
    _activity_html, repainted to the design's .log-list .e. Read fresh each load."""
    ev = activity_events()
    total = len(ev)
    start = page * PAGE_SIZE
    chunk = ev[start:start + PAGE_SIZE]
    head = _pane_head("Activity log", "Everything I&rsquo;ve done.",
                      "Decisions, trades, vetoes, and self-flags -- every action with its "
                      "context. Click a row for the full reasoning.")
    if not chunk:
        body = '<div class="card muted">No activity yet.</div>'
        return f'<div class="wrap">{head}{body}</div>'

    payload = []  # per-row detail, indexed parallel to rendered rows
    rows = ""
    for i, (ts, kind, text, sym, status, detail) in enumerate(chunk):
        kind_lbl = (status or kind or "log")
        kcls = _kind_cls_v2(status or kind)
        ctx = (f'<b>{escape(str(sym))}</b>' if sym else '')
        rows += (
            f'<button type="button" class="rowbtn" onclick="showAct({i})">'
            f'<div class="e"><div class="t">{escape(_fmt_ts(ts))}</div>'
            f'<div class="body"><span class="kind {kcls}">{escape(str(kind_lbl))}</span>'
            f'{escape(text)}</div>'
            f'<div class="ctx">{ctx}</div></div></button>'
        )
        payload.append({"title": text, "detail": detail})

    pg = '<div class="pg">'
    if page > 0:
        pg += f'<a href="/?tab=log&page={page - 1}">&larr; newer</a>'
    if start + PAGE_SIZE < total:
        pg += f'<a href="/?tab=log&page={page + 1}">older &rarr;</a>'
    pg += f'<span class="muted">{total} events</span></div>'

    body = (f'<div class="log-list">{rows}</div>'
            f'<script type="application/json" id="act-data">{_json_for_html(payload)}</script>'
            + _ACTIVITY_DIALOG + pg)
    return f'<div class="wrap">{head}{body}</div>'


def _evolve_html() -> str:
    """EVOLVE: how the mind has CHANGED -- belief flips from memory cards (history>1),
    plus applied + rejected changes from change_log.json. Past-tense and auditable.
    Mirrors v1's _evolution_html, repainted to the design's .evo-list .item. Read fresh."""
    head = _pane_head("Evolution", "How my mind has changed.",
                      "Beliefs I've flipped, rules I've enforced in code, and proposals I "
                      "rejected. Newest first.")
    items = ""  # rendered .item blocks

    # Recent mind-changes: memory cards updated/flipped more than once (history > 1).
    try:
        flips = [c for c in memory.all_cards() if len(c.get("history", [])) > 1]
        flips.sort(key=lambda c: c.get("touched", ""), reverse=True)
    except Exception:
        flips = []
    for c in flips[:20]:
        last = (c.get("history") or [{}])[-1].get("note", "")
        tags = "".join(f'<span>{escape(t)}</span>' for t in c.get("tags", []))
        tags_html = f'<div class="tags">{tags}</div>' if tags else ""
        bucket = c.get("bucket", "") or "belief"
        items += (f'<div class="item"><div class="head">'
                  f'<span class="kind">{escape(bucket)} &middot; updated</span>'
                  f'<span class="when">{escape(_fmt_ts(c.get("touched", "")))}</span></div>'
                  f'<h3>{escape(c.get("headline", ""))}</h3>'
                  f'<p>{escape(last)}</p>{tags_html}</div>')

    # Applied changes + rejected registry from change_log.json.
    cl = _read_json("change_log.json", {"changes": [], "rejected": []})
    ch = sorted(cl.get("changes", []), key=lambda e: e.get("ts", ""), reverse=True)
    for e in ch[:30]:
        tags = "".join(f'<span>{escape(t)}</span>' for t in e.get("tags", []))
        tags_html = f'<div class="tags">{tags}</div>' if tags else ""
        items += (f'<div class="item"><div class="head">'
                  f'<span class="kind enf">{escape(e.get("area", "") or "change")} &middot; applied</span>'
                  f'<span class="when">{escape(e.get("ts", "")[:10])}</span></div>'
                  f'<h3>{escape(e.get("what", ""))}</h3>{tags_html}</div>')

    rj = sorted(cl.get("rejected", []), key=lambda e: e.get("ts", ""), reverse=True)
    for e in rj[:20]:
        items += (f'<div class="item"><div class="head">'
                  f'<span class="kind rej">Rejected</span>'
                  f'<span class="when">{escape(e.get("ts", "")[:10])}</span></div>'
                  f'<h3>{escape(e.get("thesis", ""))}</h3>'
                  f'<p>{escape(e.get("reason", ""))}</p></div>')

    if not items:
        body = ('<div class="card muted">No mind-changes recorded yet. As the mind flips '
                'beliefs or enforces new rules, they show up here.</div>')
    else:
        body = f'<div class="evo-list">{items}</div>'
    return f'<div class="wrap">{head}{body}</div>'


_CAD_CHOICES = [("every run", "every_run", None), ("1h", "interval", 1), ("2h", "interval", 2),
                ("6h", "interval", 6), ("12h", "interval", 12), ("24h", "interval", 24)]


def _marketplace_catalog() -> list:
    """Plugins available to install. Prefer the local marketplace cache (fast, populated once
    `claude plugin marketplace add` has run); fall back to a short GitHub raw fetch. Guarded."""
    import glob
    import os
    from pathlib import Path
    home = os.path.expanduser("~")
    for mp in glob.glob(home + "/.claude/plugins/marketplaces/*/.claude-plugin/marketplace.json"):
        try:
            mk = json.loads(Path(mp).read_text())
            if mk.get("name") == "aitrader-plugins":
                return mk.get("plugins") or []
        except (json.JSONDecodeError, OSError):
            continue
    try:
        import urllib.request
        url = "https://raw.githubusercontent.com/nirajgtm/AiTrader-plugins/main/.claude-plugin/marketplace.json"
        with urllib.request.urlopen(url, timeout=2.5) as r:
            return (json.loads(r.read().decode()).get("plugins") or [])
    except Exception:
        return []


def _marketplace_section_html(installed_keys: set) -> str:
    """The Marketplace: catalog plugins not yet installed, each with an Install button (which
    virtually installs on click and queues the real install for next run)."""
    queued = {it.get("plugin") for it in agent_controls.install_queue()}
    cards = ""
    for p in _marketplace_catalog():
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "")
        if not name or name == "example-agent" or name in installed_keys:
            continue
        desc = escape(str(p.get("description") or ""))
        auth = p.get("author")
        auth_name = auth.get("name") if isinstance(auth, dict) else (auth or "")
        by = f'<div class="ag-by">by {escape(str(auth_name))}</div>' if auth_name else ""
        if name in queued:
            action = '<span class="ag-install queued">will install next run</span>'
        else:
            action = (f'<a class="ag-install" href="/api/install_plugin?plugin={escape(name)}" '
                      f'onclick="return confirm(\'WARNING: installing a plugin runs third-party code on '
                      f'your machine. It can ship scripts and add new dashboard tabs that execute locally, '
                      f'and could read or exfiltrate your personal data such as positions, account, and '
                      f'balances. Install only from an author you trust. Continue?\')">Install</a>')
        cards += (f'<div class="ag-card mkt">{action}<div class="ag-name">{escape(name)}</div>'
                  f'{by}<div class="ag-role">{desc}</div></div>')
    if not cards:
        body = ('<div class="card muted">No new plugins to install (catalog empty, offline, or all '
                'installed). The catalog comes from github.com/nirajgtm/AiTrader-plugins.</div>')
    else:
        body = f'<div class="ag-grid">{cards}</div>'
    return '<div class="grp" style="margin-top:24px">Marketplace</div>' + body


_AGENTS_CSS = """
.ag-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px;margin:6px 0 8px}
.ag-card{position:relative;border:1px solid var(--rule);border-radius:14px;padding:15px 16px 13px;background:var(--surface);cursor:pointer;transition:box-shadow .15s}
.ag-card:hover{box-shadow:0 6px 18px rgba(10,10,12,0.08)}
.ag-card.off{opacity:.5;background:var(--bg-soft);cursor:default}
.ag-card .ag-name{font-family:var(--sans);font-weight:700;font-size:15.5px;letter-spacing:-0.01em;color:var(--ink-1);padding-right:46px;line-height:1.25}
.ag-card .ag-role{font-size:12px;color:var(--ink-2);margin-top:6px;line-height:1.45}
.ag-card .ag-meta{font-family:var(--mono);font-size:10px;color:var(--ink-3);margin-top:9px;text-transform:uppercase;letter-spacing:0.04em}
.ag-pill{display:inline-block;font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:0.07em;padding:2px 7px;border-radius:999px;background:#e8f3ec;color:#2a6a4a;margin-top:8px}
.ag-switch{position:absolute;top:14px;right:14px;width:38px;height:21px;border-radius:999px;background:var(--rule-2);display:block;transition:background .15s;text-decoration:none}
.ag-switch.on{background:#3a9d6a}
.ag-switch::after{content:"";position:absolute;top:2px;left:2px;width:17px;height:17px;border-radius:50%;background:#fff;transition:left .15s;box-shadow:0 1px 2px rgba(0,0,0,.25)}
.ag-switch.on::after{left:19px}
.ag-modal{max-width:680px;width:92%;border:1px solid var(--rule-2);border-radius:14px;padding:22px 24px}
.ag-modal-title{font-family:var(--sans);font-weight:700;font-size:20px;margin:0 0 6px}
.ag-modal .md{font-size:13.5px;line-height:1.65;color:var(--ink-1);margin-top:14px;border-top:1px solid var(--rule);padding-top:14px;max-height:50vh;overflow:auto}
.ag-cad{background:var(--bg-soft);border-radius:10px;padding:12px 14px;margin-top:6px}
.ag-cad-row{margin:5px 0;font-size:13px;color:var(--ink-1)}
.ag-num{width:56px;padding:3px 6px;border:1px solid var(--rule-2);border-radius:6px;font-family:var(--mono)}
.ag-remove{display:inline-block;margin-top:10px;font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:0.04em;color:#a44;text-decoration:none}
.ag-remove:hover{text-decoration:underline}
.ag-card.mkt{cursor:default}
.ag-card.mkt .ag-name{padding-right:80px}
.ag-by{font-family:var(--mono);font-size:10px;color:var(--ink-3);margin-top:4px}
.ag-install{position:absolute;top:13px;right:13px;font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:0.04em;padding:6px 13px;border-radius:999px;background:var(--accent-deep,#2a6a4a);color:#fff;text-decoration:none}
.ag-install:hover{opacity:.88}
.ag-install.queued{position:static;display:inline-block;background:var(--bg-soft);color:var(--ink-3);padding:4px 10px;margin-bottom:6px}
"""


def _agents_html_v2() -> str:
    """AGENTS: a card per agent with a corner enable/disable switch. Click an enabled card for a
    modal with its spec MD and a cadence form (every run, or every N hours, N configurable).
    Disabled agents gray out into a Disabled section. Plus a marketplace section to install new
    plugins. Reads agent_controls.status_all() (core roster + installed plugins) and registry.json."""
    sub_dir = DIR / "subagents"
    head = _pane_head("Agents", "Who I can convene, and when.",
                      "Flip the switch to enable or disable an agent. A disabled agent is never "
                      "convened. Click an enabled card for its spec and cadence.")

    reg_by_key = {}
    try:
        reg = json.loads((sub_dir / "registry.json").read_text())
        for grp in ("every_run", "debate_panel", "on_demand"):
            for a in reg.get(grp, []):
                if not isinstance(a, dict):
                    continue
                spec = str(a.get("spec") or "")
                key = spec[:-3] if spec.endswith(".md") else spec
                if key:
                    reg_by_key[key] = {"name": str(a.get("name") or key),
                                       "role": str(a.get("role") or ""), "spec": spec}
    except (json.JSONDecodeError, OSError):
        pass

    def _spec_text(spec: str) -> str:
        try:
            return (sub_dir / spec).read_text().strip() if spec else ""
        except OSError:
            return ""

    def _cadence_label(cad) -> str:
        if not cad:
            return "event-driven"
        if cad.get("type") == "every_run":
            return "every run"
        if cad.get("type") == "interval":
            return f"every {cad.get('hours', 24)}h"
        return "event-driven"

    dialogs = []

    def _modal(dlg_id: str, st: dict, name: str, spec_text: str) -> str:
        cad = st["cadence"]
        md = _md_lite(spec_text) if spec_text else \
            '<p style="color:var(--ink-3)">No local spec (marketplace plugin; its spec lives in the plugin repo).</p>'
        cad_form = ""
        if st["tier"] in ("cadence", "plugin"):
            is_interval = (cad or {}).get("type") == "interval"
            hrs = (cad or {}).get("hours", 1) if is_interval else 1
            cad_form = (
                f'<form method="get" action="/api/agent_control" class="ag-cad">'
                f'<input type="hidden" name="key" value="{escape(st["key"])}">'
                f'<div class="ag-cad-row"><label><input type="radio" name="cadence_type" value="every_run"'
                f'{" checked" if not is_interval else ""}> Every run</label></div>'
                f'<div class="ag-cad-row"><label><input type="radio" name="cadence_type" value="interval"'
                f'{" checked" if is_interval else ""}> Every '
                f'<input type="number" name="cadence_hours" min="1" value="{escape(str(hrs))}" class="ag-num"> hours</label></div>'
                f'<button type="submit" class="ctrl-btn" style="margin-top:8px">Save cadence</button></form>')
        return (f'<dialog id="{dlg_id}" class="ag-modal">'
                f'<h3 class="ag-modal-title">{name}</h3>{cad_form}'
                f'<div class="md">{md}</div>'
                f'<form method="dialog" style="margin-top:14px"><button class="ctrl-btn">Close</button></form>'
                f'</dialog>')

    def _card(st: dict, removable: bool = False) -> str:
        key = st["key"]
        meta = reg_by_key.get(key, {})
        name = escape(meta.get("name") or key.replace("_", " ").title())
        role = escape(meta.get("role") or ("marketplace plugin" if st["tier"] == "plugin" else ""))
        enabled = st["enabled"]
        tabk = st.get("tab")
        tablabel = dict(_TABS).get(tabk, tabk) if tabk else ""
        warn = (f' onclick="return confirm(\'Disabling this also hides the {tablabel} tab. Continue?\')"'
                if tabk and enabled else "")
        sw = (f'<a class="ag-switch {"on" if enabled else ""}" title="{"Disable" if enabled else "Enable"}" '
              f'href="/api/agent_control?key={escape(key)}&enabled={0 if enabled else 1}"{warn}></a>')
        pill = '<span class="ag-pill">recommended</span>' if st["recommended"] else ""
        tab_badge = f'<div class="ag-by">renders the {escape(tablabel)} tab</div>' if tabk else ""
        lc = st.get("last_convened")
        cad_lbl = _cadence_label(st["cadence"]) if st["tier"] in ("cadence", "plugin") else "event-driven"
        meta_line = (f'<div class="ag-meta">{escape(cad_lbl)} &middot; '
                     f'last {escape(lc[:16]) if lc else "never"}</div>')
        body = f'{sw}<div class="ag-name">{name}</div>{pill}{tab_badge}<div class="ag-role">{role}</div>{meta_line}'
        if removable:
            body += f'<a class="ag-remove" title="Remove this plugin" href="/api/remove_plugin?plugin={escape(key)}">Remove</a>'
        if enabled:
            dlg_id = "agdlg-" + key
            dialogs.append(_modal(dlg_id, st, name, _spec_text(meta.get("spec", ""))))
            return f'<div class="ag-card" data-dlg="{dlg_id}">{body}</div>'
        return f'<div class="ag-card off">{body}</div>'

    by_tier = {"recommended": [], "cadence": []}
    downloaded = []   # marketplace plugins (their own section, any enabled state)
    disabled = []     # disabled core agents
    for st in agent_controls.status_all():
        if st["tier"] == "plugin":
            downloaded.append(st)
        elif not st["enabled"]:
            disabled.append(st)
        else:
            by_tier.get(st["tier"], by_tier["cadence"]).append(st)

    def _grid(items):
        return '<div class="ag-grid">' + "".join(_card(s) for s in items) + "</div>"

    inner = ""
    if by_tier["recommended"]:
        inner += '<div class="grp">Recommended -- feed the decision</div>' + _grid(by_tier["recommended"])
    if by_tier["cadence"]:
        inner += '<div class="grp">Scheduled</div>' + _grid(by_tier["cadence"])
    if downloaded:
        inner += ('<div class="grp">Downloaded (marketplace)</div><div class="ag-grid">'
                  + "".join(_card(s, removable=True) for s in downloaded) + "</div>")
    if disabled:
        inner += '<div class="grp" style="margin-top:22px">Disabled</div>' + _grid(disabled)
    inner += _marketplace_section_html({s["key"] for s in downloaded})

    script = ("<script>(function(){document.querySelectorAll('.ag-card[data-dlg]').forEach(function(c){"
              "c.addEventListener('click',function(e){if(e.target.closest('a'))return;"
              "var d=document.getElementById(c.dataset.dlg);if(d&&d.showModal)d.showModal();});});})();</script>")
    return (f'<div class="wrap wrap--narrow">{head}<style>{_AGENTS_CSS}</style>'
            f'{inner}{"".join(dialogs)}</div>{script}')


def render_page(tab: str, page: int = 0, win: str = "all", who: str = "") -> str:
    """Route a tab key to its pane and wrap it in the full light-design document.
    `who` is the username picked in the Watch tab's browse panel (?who=)."""
    global _SHARE_SELECTED
    _SHARE_SELECTED = who if tab == "watch" else ""
    _at = _agent_tabs()
    if tab in _at and not _at[tab]["enabled"]:
        owner = _at[tab]["owner"].replace("_", " ").title()
        inner = (f'<div class="wrap wrap--narrow"><div class="pane-head"><h1>{escape(dict(_TABS).get(tab, tab))}</h1></div>'
                 f'<div class="card muted">This tab is off because its agent (<b>{escape(owner)}</b>) is '
                 f'disabled. Enable it on the <a href="/?tab=agents">Agents</a> tab to bring it back.</div></div>')
    elif tab == "home":
        inner = _home_html(win)
    elif tab == "mind":
        inner = _mind_html()
    elif tab == "watch":
        inner = _watch_html()
    elif tab == "conviction":
        inner = _conviction_html()
    elif tab == "shadow":
        inner = _shadow_html()
    elif tab == "decide":
        inner = _decide_html()
    elif tab == "log":
        inner = _log_html(page)
    elif tab == "evolve":
        inner = _evolve_html()
    elif tab == "agents":
        inner = _agents_html_v2()
    else:
        tab = "home"
        inner = _home_html(win)
    return (
        '<!doctype html><html data-accent=green data-density=compact lang=en>'
        '<head><meta charset=utf-8>'
        '<meta name=viewport content="width=1280">'
        '<title>Autonomous Trader</title>'
        f'{_ACCENT_DENSITY_HEAD}'
        f'<style>{_CSS_V2}</style>'
        '</head><body>'
        f'{_glance_header(tab)}'
        f'<main>{inner}</main>'
        f'{_controls_overlay_html()}'
        f'{_inbox_fab_html()}'
        "<script>(function(){document.querySelectorAll('dialog').forEach("
        "function(d){if(d.dataset.bxc)return;d.dataset.bxc='1';"
        "d.addEventListener('click',function(e){if(e.target===d)d.close();});});})();</script>"
        '</body></html>'
    )


# Pasted-image handling. Map a data-URL mime to a stored extension, and an
# extension back to a Content-Type for serving the saved thumbnails.
_MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
             "image/gif": "gif", "image/webp": "webp"}
_EXT_CT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
           "gif": "image/gif", "webp": "image/webp"}


def _decode_image_blobs(images_b64: str) -> list:
    """Parse the composer's images_b64 field (a JSON array of data-URL strings) into
    a list of (bytes, ext). Each entry looks like 'data:<mime>;base64,<payload>'. The
    mime picks the extension (png/jpeg->jpg/gif/webp; default png). Bad/empty entries
    are skipped; never raises."""
    if not images_b64:
        return []
    try:
        urls = json.loads(images_b64)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(urls, list):
        return []
    blobs = []
    for url in urls:
        if not isinstance(url, str) or "," not in url:
            continue
        header, payload = url.split(",", 1)
        mime = ""
        if header.startswith("data:"):
            mime = header[5:].split(";", 1)[0].strip().lower()
        ext = _MIME_EXT.get(mime, "png")
        try:
            data = base64.b64decode(payload)
        except (ValueError, base64.binascii.Error):
            continue
        if data:
            blobs.append((data, ext))
    return blobs


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _serve_image(self, name: str):
        """Serve a pasted instruction image from state/instruction_images/. `name` must
        be a safe basename (no path separators or '..') so it can't escape the dir;
        404 on invalid/missing. Content-Type from the extension."""
        from urllib.parse import unquote
        name = unquote(name or "")
        if not name or "/" in name or "\\" in name or ".." in name:
            self.send_response(404)
            self.end_headers()
            return
        path = STATE / "instruction_images" / name
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        try:
            data = path.read_bytes()
        except (OSError, ValueError):
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", _EXT_CT.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/img/"):
            return self._serve_image(u.path[len("/img/"):])
        if u.path == "/api/img":
            return self._serve_image((q.get("f") or [""])[0])
        if u.path == "/api/decide":
            rid = (q.get("id") or [""])[0]
            decision = (q.get("decision") or [""])[0]
            reason = (q.get("reason") or [""])[0]
            try:
                approvals.decide(rid, decision, reason)
            except Exception:
                pass
            return self._redirect("/?tab=decide")
        if u.path == "/api/temperament":
            updates = {d["key"]: int((q.get(d["key"]) or [0])[0])
                       for d in temperament.DIALS if d["key"] in q}
            try:
                temperament.save(updates)
            except Exception:
                pass
            return self._redirect("/?tab=home")
        if u.path == "/api/agent_control":
            key = (q.get("key") or [""])[0]
            if key:
                kw = {}
                if "enabled" in q:
                    kw["enabled"] = (q.get("enabled") or ["1"])[0] == "1"
                ctype = (q.get("cadence_type") or [""])[0]
                if ctype == "every_run":
                    kw["cadence"] = {"type": "every_run"}
                elif ctype == "interval":
                    try:
                        kw["cadence"] = {"type": "interval",
                                         "hours": int((q.get("cadence_hours") or ["24"])[0])}
                    except ValueError:
                        pass
                try:
                    agent_controls.set_control(key, **kw)
                except Exception:
                    pass
            return self._redirect("/?tab=agents")
        if u.path == "/api/install_plugin":
            plugin = (q.get("plugin") or [""])[0]
            if plugin:
                try:
                    agent_controls.record_installed(plugin)  # virtual install: instant tile, gate-allowed
                    agent_controls.queue_install(plugin)      # real `claude plugin install` next run
                except Exception:
                    pass
            return self._redirect("/?tab=agents")
        if u.path == "/api/remove_plugin":
            plugin = (q.get("plugin") or [""])[0]
            if plugin:
                try:
                    agent_controls.remove_installed(plugin)
                    agent_controls.clear_install(plugin)
                except Exception:
                    pass
            return self._redirect("/?tab=agents")
        if u.path == "/api/share_watchlist":
            # Set the owner's sharing mode; on "friends" scrub + push their watchlist,
            # on "only_me" unshare it. The scrub/push lives in shared_metadata.
            mode = (q.get("mode") or ["only_me"])[0]
            mode = "friends" if mode == "friends" else "only_me"
            if shared_metadata is not None:
                try:
                    shared_metadata.set_share_mode(mode)
                    me = shared_metadata.my_username()
                    if me and mode == "friends":
                        shared_metadata.share(me, autonomous_watchlist.load())
                    elif me:
                        shared_metadata.unshare(me)
                except Exception:
                    pass
            return self._redirect("/?tab=watch")
        if u.path == "/api/add_from_shared":
            # Add a friend's shared entry (whole JSON: target, thesis, ...) to the
            # owner's autonomous watchlist, but only if that ticker isn't already on it.
            username = (q.get("username") or [""])[0]
            ticker = (q.get("ticker") or [""])[0]
            if shared_metadata is not None and username and ticker:
                try:
                    have = {str(e.get("ticker")).upper()
                            for e in autonomous_watchlist.load() if isinstance(e, dict)}
                    entry = next((e for e in shared_metadata.read_watchlist(username)
                                  if str(e.get("ticker") or "").upper() == ticker.upper()), None)
                    if entry and ticker.upper() not in have:
                        _add_shared_entry(entry)
                except Exception:
                    pass
            return self._redirect(f"/?tab=watch&who={quote(username)}")
        if u.path == "/api/refresh_shared":
            if shared_metadata is not None:
                try:
                    shared_metadata.refresh(force=True)
                except Exception:
                    pass
            return self._redirect("/?tab=watch")
        tab = (q.get("tab") or ["home"])[0]
        page = int((q.get("page") or ["0"])[0] or 0)
        win = (q.get("win") or ["all"])[0]
        who = (q.get("who") or [""])[0]
        # On the Watch tab, refresh the clone in the background (honors a short TTL in
        # shared_metadata, so it's a no-op when fresh) to keep git off the render path.
        if tab == "watch" and shared_metadata is not None:
            threading.Thread(target=_safe_refresh, daemon=True).start()
        html = render_page(tab, page, win, who).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/instruct":
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            q = parse_qs(body)
            text = (q.get("text") or [""])[0]
            tab = (q.get("tab") or ["Other"])[0]
            blobs = _decode_image_blobs((q.get("images_b64") or [""])[0])
            try:
                instructions.add(text, tab, image_blobs=blobs or None)
            except Exception:
                pass
            return self._redirect("/")
        self.send_response(404)
        self.end_headers()


def serve(port: int) -> int:
    print(f"Dashboard v2: http://localhost:{port}  (Ctrl-C to stop)")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("serve")
    ps.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    if args.cmd == "serve":
        return serve(args.port)
    return 1


if __name__ == "__main__":
    sys.exit(main())
