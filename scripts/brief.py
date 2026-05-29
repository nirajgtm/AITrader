#!/usr/bin/env python3
"""brief.py — single-page JSON digest for the morning brief.

Calls each scanner with --json, collects the structured outputs, compresses
into ONE dict that Claude reads. Replaces full-stdout dumping in runbook.py.

Modes:
  brief.py morning   # full walk; persists state/cache/morning_digest_<date>.json
  brief.py quick     # regime/sentiment/sectors/flow only (FRESH-QUICK)
  brief.py hourly    # intraday refresh; cheap (~30-60s); used by hourly_broadcast.py
  brief.py status    # state + freshness only (FRESH-FULL)

Output: ONE JSON dict, compact. Claude only sees this.

Design principles:
  - No prose. No full data dumps. Only compressed signals + flags.
  - Deterministic logic in scripts (position_review, watchlist_check), not in
    Claude's head.
  - Cross-scanner cluster surfaced as candidates[].
  - Auto-logs research session at end.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PY = SCRIPTS / ".venv" / "bin" / "python3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _call(script: str, args: list[str], timeout: int = 90) -> dict | None:
    """Call a script with --json; parse last line of stdout."""
    cmd = [str(PY), str(SCRIPTS / script), *args, "--json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"step": script, "ok": False, "errors": [f"timeout after {timeout}s"]}
    except Exception as e:
        return {"step": script, "ok": False, "errors": [str(e)]}
    out = (r.stdout or "").strip()
    if not out:
        return {"step": script, "ok": False, "errors": [r.stderr[:200] or "no stdout"]}
    # Take last non-empty line (some scripts may print warnings before JSON)
    last_line = next((l for l in reversed(out.splitlines()) if l.strip().startswith("{")), None)
    if not last_line:
        return {"step": script, "ok": False, "errors": ["no JSON output"]}
    try:
        return json.loads(last_line)
    except Exception as e:
        return {"step": script, "ok": False, "errors": [f"parse error: {e}"]}


def _step(steps: list, results: dict, name: str, script: str, args: list[str], **kwargs) -> None:
    """Run a step, store its summary into results, append to steps log.

    Emits a one-line progress marker on stderr so background runs are
    debuggable. stdout stays a single clean JSON dict for Claude to consume.
    """
    t0 = time.time()
    res = _call(script, args, **kwargs)
    dt = time.time() - t0
    status = "ok" if (res and res.get("ok")) else "err"
    head = (res.get("headline") if res else "") or ""
    print(f"[brief] step={name:18s} {status} in {dt:5.1f}s  {head[:80]}",
          file=sys.stderr, flush=True)
    results[name] = res
    if res:
        steps.append({"name": name, "ok": res.get("ok", False),
                      "headline": res.get("headline", "")})


def _read_freshness() -> dict:
    res = _call("research.py", ["freshness"])
    # research.py freshness doesn't have --json; fall back to running without
    cmd = [str(PY), str(SCRIPTS / "research.py"), "freshness"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    out = (r.stdout or "").strip()
    first_line = out.splitlines()[0] if out else ""
    parts = first_line.split()
    status = parts[0] if parts else "STALE"
    age = parts[1] if len(parts) > 1 else ""
    return {"status": status, "age": age, "raw_first_line": first_line}


def _leap_stock_steps(steps: list, results: dict) -> None:
    """Run leap_check.py for each ticker in config/leap_stocks.json."""
    import json as _json
    cfg_path = ROOT / "config" / "leap_stocks.json"
    if not cfg_path.exists():
        return
    try:
        cfg = _json.loads(cfg_path.read_text())
    except Exception:
        return
    for entry in cfg.get("stocks", []):
        tk = entry.get("ticker", "").upper()
        if not tk:
            continue
        args = [
            "--underlying", tk,
            "--dte-days", str(entry.get("dte_days", 365)),
            "--delta-target", str(entry.get("delta_target", 0.65)),
            "--rsi-extended-skip", str(entry.get("rsi_extended_skip", 72)),
        ]
        if entry.get("iv_mult"):
            args += ["--iv-mult", str(entry["iv_mult"])]
        _step(steps, results, f"leap_stock_{tk.lower()}", "leap_check.py", args)


def _collect_leap_stock_results(results: dict) -> list[dict]:
    """Collect single-stock LEAP results; surface ENTER signals first."""
    out = []
    for key, val in results.items():
        if not key.startswith("leap_stock_") or not val:
            continue
        data = val.get("data") or {}
        if data:
            out.append(data)
    out.sort(key=lambda d: (0 if d.get("signal") == "ENTER" else 1, d.get("underlying", "")))
    return out


def _build_candidates(results: dict) -> list[dict]:
    """Cross-scanner cluster: a name appearing in 2+ signals → candidate.

    Sources scanned:
      - movers.gainers / movers.losers / movers.most_actives
      - flow.by_ticker (unusual options)
      - insider.clusters
      - watchlist_check.items where status TRIGGERED|NEAR
      - sectors.leaders_5d / rotation_in (sectors as candidates)
    """
    score: dict[str, dict] = {}

    def bump(ticker: str, source: str, detail: str = "") -> None:
        ticker = ticker.upper()
        if ticker not in score:
            score[ticker] = {"ticker": ticker, "sources": [], "details": []}
        score[ticker]["sources"].append(source)
        if detail:
            score[ticker]["details"].append(detail)

    movers = results.get("movers", {}).get("data", {}) or {}
    for it in (movers.get("gainers") or [])[:5]:
        bump(it["sym"], "mover_gainer", f"{it.get('pct')}%")
    for it in (movers.get("losers") or [])[:5]:
        bump(it["sym"], "mover_loser", f"{it.get('pct')}%")
    for it in (movers.get("most_actives") or [])[:5]:
        bump(it["sym"], "mover_active", "")

    flow = results.get("flow", {}).get("data", {}) or {}
    for tk, lst in (flow.get("by_ticker") or {}).items():
        max_voi = max((it.get("v_oi", 0) for it in lst), default=0)
        if max_voi >= 5:
            bump(tk, "unusual_flow", f"V/OI={max_voi}")

    insider = results.get("insider", {}).get("data", {}) or {}
    for c in (insider.get("clusters") or [])[:10]:
        # cluster issuer often = company name not ticker; skip if not uppercase ≤6 chars
        issuer = c.get("issuer", "")
        # Many issuers come in like "Apple Inc." — we don't have a ticker map here.
        # For now, pass through as-is and let Claude resolve.
        if 1 <= len(issuer) <= 6 and issuer.isupper():
            bump(issuer, "insider_cluster", f"{c.get('filings')} filings")

    accum = results.get("accumulation", {}).get("data", {}) or {}
    for c in accum.get("candidates", [])[:10]:
        bump(c["ticker"], "accumulation",
             f"score={c.get('score')} buys={c.get('buys')}")

    watchlist = results.get("watchlist_check", {}).get("data", {}) or {}
    for it in watchlist.get("items") or []:
        if it.get("status") in ("TRIGGERED", "NEAR"):
            bump(it["ticker"], f"watchlist_{it['status'].lower()}", f"dist={it.get('dist_pct')}%")

    sectors = results.get("sectors", {}).get("data", {}) or {}
    for r in sectors.get("rotation_in", []):
        if r.get("rotation", 0) >= 10:
            bump(r["etf"], "rotation_in", f"+{r['rotation']}")

    # Scanner signals: breakouts, breakdowns, 52w extremes, vol expansion
    scanner = results.get("scanner", {}).get("data", {}) or {}
    for it in scanner.get("breakouts", [])[:15]:
        bump(it["tk"], "breakout", f"vol×{it.get('vol_x_avg')}")
    for it in scanner.get("breakdowns", [])[:15]:
        bump(it["tk"], "breakdown", f"vol×{it.get('vol_x_avg')}")
    for it in scanner.get("new_52w_highs", [])[:10]:
        bump(it["tk"], "52w_high", "")
    for it in scanner.get("new_52w_lows", [])[:10]:
        bump(it["tk"], "52w_low", "")
    for it in scanner.get("vol_expansion", [])[:10]:
        bump(it["tk"], "vol_expansion", f"ratio={it.get('ratio')}")

    # PEAD candidates (post-earnings drift)
    pead = results.get("pead", {}).get("data", {}) or {}
    for it in pead.get("pead", [])[:10]:
        bump(it["tk"], "pead",
             f"gap={it.get('gap_pct')}% vol×{it.get('vol_x_avg')}")

    # Pre-earnings run-ups
    pre_er = results.get("pre_earnings", {}).get("data", {}) or {}
    for it in pre_er.get("pre_earnings_runup", [])[:10]:
        bump(it["tk"], "pre_earnings_runup",
             f"5d={it.get('ret5d_pct')}% er={it.get('next_earnings')}")

    # Social sentiment signals (Reddit via apewisdom + StockTwits)
    social = results.get("social", {}).get("data", {}) or {}
    for b in social.get("breakouts", [])[:10]:
        bump(b["ticker"], "social_breakout",
             f"#{b.get('rank')} (was #{b.get('rank_24h_ago') or 'new'})")
    for s in social.get("squeeze_candidates", [])[:5]:
        bump(s["ticker"], "wsb_squeeze", f"+{s.get('delta_pct')}% mentions")
    for f in social.get("bull_bear_flips", []):
        bump(f["ticker"], f"bull_bear_flip_{f.get('direction')}",
             f"{f.get('prev_ratio')}->{f.get('ratio')}")

    # Congressional trades (STOCK Act) — purchases surface as buy candidates,
    # large sales ($50k+) surface as bearish flags.
    congress = results.get("congress", {}).get("data", {}) or {}
    for item in (congress.get("items") or []):
        tk = (item.get("Ticker") or "").upper()
        txn = (item.get("Transaction") or "").lower()
        amt = float(item.get("Amount") or 0)
        rep = item.get("Representative") or ""
        if not tk or tk in ("N/A", ""):
            continue
        if "purchase" in txn:
            bump(tk, "congress_buy", f"{rep} ${amt:,.0f}")
        elif "sale" in txn and amt >= 50000:
            bump(tk, "congress_sell", f"{rep} ${amt:,.0f}")

    candidates = sorted(
        score.values(),
        key=lambda x: -len(x["sources"]),
    )
    # Min 2 sources to surface as a candidate (Phase 2.5 cross-scanner cluster)
    # Single-source movers/breakouts are noise; clusters are signal.
    return [c for c in candidates if len(c["sources"]) >= 2][:15]


def _top_movers(results: dict, n: int = 10) -> dict:
    """Top N gainers and losers from movers.py step (raw, not deduped against candidates).

    Surfaces names that may not have hit the 2+ source bar but are still
    notable single-day moves. Companion to `candidates` which filters to
    cross-scanner clusters.
    """
    movers = results.get("movers", {}).get("data", {}) or {}
    return {
        "gainers": [
            {"tk": it["sym"], "pct": it.get("pct"), "px": it.get("px")}
            for it in (movers.get("gainers") or [])[:n]
        ],
        "losers": [
            {"tk": it["sym"], "pct": it.get("pct"), "px": it.get("px")}
            for it in (movers.get("losers") or [])[:n]
        ],
    }


def _vol_confirmed(results: dict, key: str, ascending: bool, vol_min: float = 1.5,
                   limit: int = 15) -> list:
    """Breakouts/breakdowns filtered to vol×>=vol_min, ordered by vol_x_avg.

    Scanner.py emits items shaped {tk, close, high20d|low20d, vol_x_avg}; there
    is no pct field on these. Volume confirmation (vol_x_avg) is the strongest
    signal we have, so order by that descending. The ascending arg is kept for
    interface symmetry but currently unused.
    """
    _ = ascending  # noqa: F841 — reserved for future pct ordering
    scanner = results.get("scanner", {}).get("data", {}) or {}
    items = scanner.get(key) or []
    confirmed = [it for it in items if (it.get("vol_x_avg") or 0) >= vol_min]
    confirmed.sort(key=lambda x: -(x.get("vol_x_avg") or 0))
    return [
        {"tk": it.get("tk"), "close": it.get("close"),
         "vol_x_avg": it.get("vol_x_avg")}
        for it in confirmed[:limit]
    ]


def _holdings_news_block(results: dict) -> list:
    data = (results.get("holdings_news") or {}).get("data") or {}
    if not isinstance(data, dict):
        return []
    block: list = []
    for tk, items in data.items():
        items = items or []
        if not items:
            continue
        headlines = []
        for it in items[:2]:
            headlines.append({
                "title": (it.get("title") or "")[:100],
                "pub": it.get("pub"),
                "age_h": it.get("age_h"),
            })
        block.append({
            "ticker": tk,
            "n_articles": len(items),
            "headlines": headlines,
        })
    block.sort(key=lambda x: -x["n_articles"])
    return block


def _social_per_watchlist_block(results: dict) -> dict:
    social = (results.get("social") or {}).get("data") or {}
    wl = ((social.get("stocktwits") or {}).get("watchlist")) or {}
    flips = social.get("bull_bear_flips") or []
    per_ticker = []
    if isinstance(wl, dict):
        for tk, summ in wl.items():
            summ = summ or {}
            total = summ.get("total") or 0
            if total < 5:
                continue
            per_ticker.append({
                "ticker": tk,
                "bull": summ.get("bull"),
                "bear": summ.get("bear"),
                "ratio": summ.get("ratio"),
                "total": total,
            })
        per_ticker.sort(key=lambda x: -(x.get("total") or 0))
    return {"per_ticker": per_ticker, "flips": flips}


def _portfolio_summary(results: dict) -> dict:
    """Build the portfolio summary block for the digest.

    Schema is dual-shape:
      - v3+ (cash-agnostic): mtm emits `user_positions[]`, `challenge_positions[]`,
        `legacy_challenge{}`. Active book = user_positions.
      - v2 / older: mtm emits `cash`, `equity`, `starting_equity`, `positions[]`.
        Treated as the challenge book.

    Back-compat keys (`cash`, `equity`, ...) are preserved so older brief.py
    consumers don't crash; they read from `legacy_challenge` if v3, else root.
    """
    mtm = results.get("mtm", {}).get("data") or {}
    book_mode = mtm.get("book_mode") or ("legacy_challenge" if mtm.get("cash") is not None else "unknown")
    user_positions = mtm.get("user_positions", [])
    challenge_positions = mtm.get("challenge_positions") or mtm.get("positions") or []
    legacy = mtm.get("legacy_challenge") or {}
    # Back-compat: pre-v3 mtm emitted cash/equity at root.
    if not legacy and mtm.get("cash") is not None:
        legacy = {
            "cash": mtm.get("cash"),
            "equity": mtm.get("equity"),
            "starting_equity": mtm.get("starting_equity"),
            "high_water": mtm.get("high_water"),
            "realized_pnl": mtm.get("realized_pnl"),
        }
    return {
        "schema_version": mtm.get("schema_version"),
        "book_mode": book_mode,
        "user_open_count": len(user_positions),
        "user_cost_basis_total": mtm.get("user_cost_basis_total"),
        "user_mv_total": mtm.get("user_mv_total"),
        "user_pnl_total": mtm.get("user_pnl_total"),
        "user_positions": user_positions,
        "challenge_open_count": len(challenge_positions),
        "legacy_challenge": legacy,
        # Legacy back-compat keys consumed by other code paths in this module:
        "cash": legacy.get("cash"),
        "equity": legacy.get("equity"),
        "starting": legacy.get("starting_equity"),
        "high_water": legacy.get("high_water"),
        "drawdown_pct": None,  # cash-agnostic = not meaningful
        "open_count": len(challenge_positions) + len(user_positions),
    }


def _auto_shadow_fomo_blocks(candidates: list[dict], max_open: int = 5) -> list[dict]:
    """Auto-open fomo_chase_test shadows for candidates the FOMO/RSI rule blocks.

    Per state/behavioral_audit.md (2026-05-05): MU and INTC ripped after the
    FOMO rule blocked entry. Without an audit trail of "rule said no, here's
    what happened next," we can't tell if the rule is calibrated right. This
    opens a deterministic stock shadow for every cross-scanner candidate flagged
    fomo_above_2atr or rsi_overbought, capped at max_open per session, deduped
    per (ticker, strategy) so a multi-day run-up does not accumulate duplicates.

    Shadow shape: stock, qty=1, entry=close, target=close+1*ATR (continuation =
    rule too tight), stop=close-2*ATR (mean reversion = rule was right),
    horizon=10d. Strategy tag: fomo_chase_test.
    """
    try:
        sys.path.insert(0, str(SCRIPTS))
        from _common import load_shadow_positions
    except Exception as e:
        print(f"[brief] WARN fomo auto-shadow import failed: {e}", file=sys.stderr)
        return []

    state = load_shadow_positions()
    already_open = {
        (p.get("ticker", "").upper(), p.get("strategy", ""))
        for p in state.get("positions", [])
    }

    opened: list[dict] = []
    for cand in candidates:
        if len(opened) >= max_open:
            break
        ticker = (cand.get("ticker") or "").upper()
        if not (1 <= len(ticker) <= 5 and ticker.isalpha()):
            continue
        if (ticker, "fomo_chase_test") in already_open:
            continue

        try:
            r = subprocess.run(
                [str(PY), str(SCRIPTS / "price.py"), ticker, "--json"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            continue
        if r.returncode != 0 or not r.stdout:
            continue
        last_line = next((l for l in reversed(r.stdout.splitlines())
                          if l.strip().startswith("{")), None)
        if not last_line:
            continue
        try:
            px = json.loads(last_line)
        except Exception:
            continue
        flags = set(px.get("flags") or [])
        if not (flags & {"fomo_above_2atr", "rsi_overbought"}):
            continue
        data = px.get("data") or {}
        close = data.get("close")
        atr = data.get("atr14")
        if close is None or atr is None:
            continue
        entry = round(float(close), 2)
        atr_v = float(atr)
        target = round(entry + atr_v, 2)
        stop = round(entry - 2 * atr_v, 2)
        rsi = data.get("rsi14")
        ceiling = data.get("fomo_ceiling")
        thesis = (
            f"FOMO_CHASE_TEST: {ticker} blocked at ${entry} "
            f"(RSI {rsi}, ceiling ${ceiling}). Tests whether chase produces "
            f"+1 ATR within 10d. Outcome calibrates rule."
        )
        try:
            r2 = subprocess.run([
                str(PY), str(SCRIPTS / "shadow.py"), "open",
                "--ticker", ticker, "--vehicle", "stock",
                "--qty", "1", "--entry", str(entry),
                "--stop", str(stop), "--target", str(target),
                "--thesis", thesis,
                "--strategy", "fomo_chase_test",
                "--horizon", "10",
            ], capture_output=True, text=True, timeout=15)
        except Exception as e:
            print(f"[brief] WARN fomo auto-shadow {ticker} subprocess: {e}",
                  file=sys.stderr)
            continue
        if r2.returncode != 0:
            print(f"[brief] WARN fomo auto-shadow {ticker}: {r2.stderr[:120]}",
                  file=sys.stderr)
            continue
        opened.append({
            "ticker": ticker, "entry": entry, "stop": stop, "target": target,
            "rsi": rsi, "fomo_ceiling": ceiling, "horizon": 10,
            "blocked_by": sorted(flags & {"fomo_above_2atr", "rsi_overbought"}),
            "sources": cand.get("sources", []),
        })
    return opened


def _fired_alert_actions() -> list:
    """Fired-but-unacknowledged alerts, shaped as digest actions.

    A fired alert is a prompt to RE-VALIDATE, never a blind signal to act -- the
    instruction field says so, and each action carries the alert's hypothesis plus
    the linked watchlist thesis so /trader can check the reasoning against fresh
    data. Surfaced every run so a fire is never missed. Acknowledge after review.
    """
    out: list = []
    try:
        sys.path.insert(0, str(SCRIPTS))
        import alerts as _alerts
        import watchlist_store as _wl
        for a in _alerts.fired_unacknowledged():
            entry = _wl.get_entry(a.get("ticker", "")) or {}
            out.append({
                "kind": "alert_fired",
                "id": a.get("id"),
                "ticker": a.get("ticker"),
                "condition": a.get("condition"),
                "hypothesis": a.get("hypothesis") or a.get("message"),
                "watchlist_thesis": entry.get("thesis"),
                "macro": a.get("macro", False),
                "fired_at": a.get("fired_at"),
                "instruction": ("RE-VALIDATE against fresh data before acting; a fired "
                                "trigger is not a buy/sell signal. After reviewing, run: "
                                f"alerts.py acknowledge {a.get('id')}"),
            })
    except Exception as e:
        print(f"[brief] WARN fired_alert_actions failed: {e}", file=sys.stderr)
    return out


def run_morning() -> dict:
    started = _now()
    results: dict = {}
    steps: list = []

    # Cache hygiene: prune entries older than 7d before starting
    try:
        sys.path.insert(0, str(SCRIPTS))
        from _cache import prune_older_than
        pruned = prune_older_than(7)
        if pruned:
            print(f"[brief] pruned {pruned} stale cache entries", file=sys.stderr)
    except Exception:
        pass

    # Log hygiene: archive prior-month daily logs
    try:
        from archive_logs import archive_prior_months
        archived = archive_prior_months()
        if archived:
            print(f"[brief] archived {archived} prior-month logs", file=sys.stderr)
    except Exception:
        pass

    # State
    _step(steps, results, "mtm", "mtm.py", ["show"])
    _step(steps, results, "shadow_sweep", "shadow.py", ["sweep"])
    _step(steps, results, "shadow_pnl", "shadow.py", ["pnl"])

    # Market
    _step(steps, results, "regime", "regime.py", [])
    _step(steps, results, "sentiment", "sentiment.py", [])
    _step(steps, results, "social", "social_sentiment.py", [], timeout=120)
    _step(steps, results, "breadth", "breadth.py", [], timeout=240)
    _step(steps, results, "sectors", "sector_scan.py", [])

    # Catalysts
    _step(steps, results, "earnings",
          "earnings.py",
          ["NVDA", "AAPL", "MSFT", "META", "AMZN", "GOOGL", "TSLA", "AMD", "AVGO", "NFLX",
           "--no-em"])
    _step(steps, results, "macro", "macro.py", ["--days", "14"])

    # Flow
    _step(steps, results, "flow", "flow_scan.py", ["--majors"])

    # Smart-money (data gap permitted)
    _step(steps, results, "insider", "insider.py", ["--days", "30"])
    _step(steps, results, "congress", "congress.py", ["--days", "7"])

    # Accumulation scanner: ticker-level insider buys (Finnhub Form-4) on
    # technically healthy names (RSI < 60, within 10% of 50DMA). Surfaces
    # "rising star" candidates BEFORE they break out, so the FOMO rule does
    # not block entry when the breakout fires. Cold pull ~5-10 min, cached 12h.
    _step(steps, results, "accumulation", "accumulation_scanner.py",
          ["--top", "15", "--days", "60"], timeout=900)

    # Movers (qualification-filtered to in-universe + price > $5 + 2-25% range)
    _step(steps, results, "movers", "movers.py", ["--gainers", "--losers", "--actives"])

    # Universe scanner (breakouts/breakdowns/52w hi-lo/vol expansion)
    # 21-day window covers 20-day breakout + ATR(14) baseline; cold takes ~4 min
    # at Polygon free-tier 5/min, warm-cache <1s.
    _step(steps, results, "scanner", "scanner.py", ["--days", "21"], timeout=600)

    # PEAD scanner (60d history, reuses Polygon cache from above; ~40 fresh dates)
    _step(steps, results, "pead", "scanner.py", ["--pead", "--days", "60"], timeout=900)

    # Pre-earnings run-up (uses 21d cache from above; nearly free)
    _step(steps, results, "pre_earnings", "scanner.py", ["--pre-earnings", "--days", "21"],
          timeout=120)

    # Universe-wide upcoming earnings (Finnhub bulk; ~free after first call)
    _step(steps, results, "earnings_universe", "earnings.py",
          ["--universe", "--days", "7"], timeout=60)

    # Crypto (RH-tradable list + CoinGecko trending + BTC dominance)
    _step(steps, results, "crypto", "crypto.py", [])

    # Watchlist
    _step(steps, results, "watchlist_check", "watchlist_check.py", [])
    _step(steps, results, "watchlist_hygiene", "watchlist_hygiene.py", [])

    # Open-position deterministic review
    _step(steps, results, "position_review", "position_review.py", [])

    # Option drawdown monitor: flag held option positions down > 70%, classify
    # cause (delta / theta / IV / mixed), suggest BUY_NEW_CONTRACT vs NO_REBUY_THETA
    _step(steps, results, "option_drawdown", "option_drawdown_monitor.py", [])

    # VIX option play -- daily signal for native VIX index options on RH.
    # Spot, term structure (^VIX9D/^VIX/^VIX3M/^VIX6M), real chain pull, suggests
    # one trade or WAIT. Spec: knowledge/strategies/vix_options.md (TBD).
    _step(steps, results, "vix_check", "vix_check.py", [])

    # LEAP / long-dated call signals -- Anchor (540 DTE) + Aggressive (180 DTE)
    # Spec: knowledge/strategies/leap_long.md.
    # Backtest 2019-2024: 540 DTE = +206pp alpha / -43% DD; 180 DTE = +302pp alpha / -55% DD.
    _step(steps, results, "leap_anchor_spy", "leap_check.py",
          ["--underlying", "SPY", "--dte-days", "540"])
    _step(steps, results, "leap_anchor_qqq", "leap_check.py",
          ["--underlying", "QQQ", "--dte-days", "540"])
    _step(steps, results, "leap_aggressive_spy", "leap_check.py",
          ["--underlying", "SPY", "--dte-days", "180"])
    _step(steps, results, "leap_aggressive_qqq", "leap_check.py",
          ["--underlying", "QQQ", "--dte-days", "180"])

    # Single-stock LEAP candidates (config/leap_stocks.json)
    _leap_stock_steps(steps, results)

    # 48-hour news for every ticker the user actually holds
    user_positions = (results.get("mtm", {}).get("data") or {}).get("user_positions") or []
    held_tickers = sorted({(p.get("tk") or "").upper() for p in user_positions if p.get("tk")})
    held_tickers = [t for t in held_tickers if t]
    if held_tickers:
        _step(steps, results, "holdings_news", "news.py",
              [*held_tickers, "--hours", "48"], timeout=180)

    ended = _now()

    # Build digest
    portfolio = _portfolio_summary(results)
    candidates = _build_candidates(results)

    # FOMO chase audit: open deterministic shadow for any candidate the
    # FOMO/RSI rule blocks. Builds an audit trail to calibrate the rule
    # empirically. See state/behavioral_audit.md 2026-05-05.
    auto_shadows = _auto_shadow_fomo_blocks(candidates)
    if auto_shadows:
        print(f"[brief] auto-shadowed {len(auto_shadows)} FOMO-blocked candidates: "
              f"{','.join(s['ticker'] for s in auto_shadows)}", file=sys.stderr)

    # Aggregate flags
    all_flags: list[str] = []
    for r in results.values():
        if r:
            all_flags.extend(r.get("flags", []))

    # Aggregate actions (the structured recommendations from each script)
    actions: list = []
    for r in results.values():
        if r:
            actions.extend(r.get("actions", []))
    # And lift position_review actions to top
    pr = results.get("position_review", {}).get("data", {}) or {}
    for rev in pr.get("reviews", []):
        if rev.get("primary_action") != "HOLD":
            actions.append({
                "kind": "position_action",
                "ticker": rev["ticker"],
                "action": rev["primary_action"],
                "reasons": rev.get("reasons", []),
            })
    # Tag every action with tradable_now + tradable_window_label so consumers
    # know whether the signal is actionable now or has to wait for next session.
    # See knowledge/robinhood_after_hours.md for the source-of-truth matrix.
    try:
        from _tradable import tag_actions
        actions = tag_actions(actions)
    except Exception as e:
        print(f"[brief] WARN tag_actions failed: {e}", file=sys.stderr)

    # Macro events in horizon (next 14d)
    macro = results.get("macro", {}).get("data", {}) or {}
    upcoming = (macro.get("calendar") or [])[:8]

    # Earnings within 7d
    er = results.get("earnings", {}).get("data", {}) or {}
    earnings_within_7d = er.get("within_7d", [])

    # Headline
    spy_reg = (results.get("regime", {}).get("data") or {}).get("spy_regime", "?")
    vix_b = (results.get("regime", {}).get("data") or {}).get("vix_bucket", "?")
    headline = f"{spy_reg} | VIX {vix_b}"
    if portfolio.get("user_open_count"):
        headline += f" | user_book {portfolio['user_open_count']} open"
        if portfolio.get("user_pnl_total") is not None:
            headline += f" pnl {portfolio['user_pnl_total']:+.0f}"
    elif portfolio.get("equity"):
        headline += f" | equity ${portfolio['equity']}"
    if earnings_within_7d:
        headline += f" | earnings 7d: {','.join(earnings_within_7d[:5])}"

    # Data gaps
    data_gaps = [name for name, r in results.items() if r and not r.get("ok")]

    actions.extend(_fired_alert_actions())  # fired alerts: re-validate, never blind-act
    digest = {
        "ts": ended,
        "started": started,
        "mode": "morning",
        "headline": headline,
        "freshness": _read_freshness(),
        "portfolio": portfolio,
        "open_positions_review": pr.get("reviews", []),
        "regime": (results.get("regime", {}).get("data") or {}).get("tickers", {}),
        "regime_summary": {
            "spy_regime": spy_reg,
            "vix_bucket": vix_b,
            "vix_term_structure": (results.get("sentiment", {}).get("data") or {}).get("term_structure"),
        },
        "sectors_top": (results.get("sectors", {}).get("data") or {}).get("rotation_in", [])[:3],
        "sectors_bottom": (results.get("sectors", {}).get("data") or {}).get("rotation_out", [])[:3],
        "earnings_within_7d": earnings_within_7d,
        "macro_upcoming_14d": [
            {"date": e.get("date"), "type": e.get("type"), "days_out": e.get("days_out")}
            for e in upcoming
        ],
        "watchlist": [
            {k: v for k, v in it.items() if k in ("ticker", "status", "last", "entry_trigger", "dist_pct")}
            for it in (results.get("watchlist_check", {}).get("data", {}) or {}).get("items", [])
        ],
        "candidates": candidates,
        "auto_shadows_opened": auto_shadows,
        "accumulation_candidates": (results.get("accumulation", {}).get("data") or {}).get("candidates", [])[:10],
        "congress_trades": (results.get("congress", {}).get("data") or {}).get("items", [])[:30],
        "top_movers": _top_movers(results),
        "bo_with_vol_confirm": _vol_confirmed(results, "breakouts", ascending=False),
        "bd_with_vol_confirm": _vol_confirmed(results, "breakdowns", ascending=True),
        "holdings_news": _holdings_news_block(results),
        "social_per_watchlist": _social_per_watchlist_block(results),
        "leap_signal": {
            "anchor_540dte": {
                "spy": (results.get("leap_anchor_spy") or {}).get("data"),
                "qqq": (results.get("leap_anchor_qqq") or {}).get("data"),
            },
            "aggressive_180dte": {
                "spy": (results.get("leap_aggressive_spy") or {}).get("data"),
                "qqq": (results.get("leap_aggressive_qqq") or {}).get("data"),
            },
            "single_stocks": _collect_leap_stock_results(results),
        },
        "option_drawdown": (results.get("option_drawdown") or {}).get("data"),
        "vix_signal": (results.get("vix_check") or {}).get("data"),
        "flags": sorted(set(all_flags)),
        "actions": actions,
        "data_gaps": data_gaps,
        "steps_log": steps,
    }

    # Auto-log research session
    summary_one = f"{spy_reg}; VIX={vix_b}; earnings7d={len(earnings_within_7d)}; candidates={len(candidates)}"
    decisions = ", ".join(a.get("action", "?") + " " + a.get("ticker", "")
                          for a in actions if a.get("kind") == "position_action") or "none"
    subprocess.run([
        str(PY), str(SCRIPTS / "research.py"), "log",
        "--start", started, "--end", ended, "--kind", "full",
        "--scripts", ",".join(s["name"] for s in steps),
        "--summary", summary_one,
        "--decisions", decisions[:200],
        "--gaps", ",".join(data_gaps),
    ], capture_output=True)

    # SPY benchmark — answers "should I just be in SPY" on every brief
    digest["benchmark"] = _benchmark_block()

    # Persist morning digest as the baseline for hourly_broadcast.py to diff
    # against. Idempotent: same-day re-runs overwrite. Hourly orchestrator
    # reads this file to detect NEW intraday signals vs morning state.
    baseline_path = None
    try:
        local_date = datetime.now().strftime("%Y-%m-%d")
        cache_dir = ROOT / "state" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        baseline_path = cache_dir / f"morning_digest_{local_date}.json"
        with baseline_path.open("w") as f:
            json.dump(digest, f, separators=(",", ":"), default=str)
    except Exception as e:
        print(f"[brief] WARN failed to persist morning_digest cache: {e}",
              file=sys.stderr)

    # Regret logging: auto-log every PEAD candidate the brief surfaced
    # so we have an outcome trail for the FOMO/gate hypothesis. Best-effort.
    if baseline_path is not None:
        try:
            r = subprocess.run(
                [str(PY), str(SCRIPTS / "regret.py"), "from-digest",
                 "--digest", str(baseline_path)],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                print(f"[brief] regret: {r.stdout.strip().splitlines()[-1] if r.stdout else ''}",
                      file=sys.stderr)
        except Exception as e:
            print(f"[brief] WARN regret logging failed: {e}", file=sys.stderr)

    return digest


def _benchmark_block() -> dict | None:
    """Call benchmark.py spy --json. Best-effort; returns None on failure."""
    try:
        r = subprocess.run(
            [str(PY), str(SCRIPTS / "benchmark.py"), "spy",
             "--equal-cap", "10000", "--json"],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0 or not r.stdout:
            return None
        last = next((l for l in reversed(r.stdout.splitlines())
                     if l.strip().startswith("{")), None)
        return json.loads(last) if last else None
    except Exception:
        return None


def run_quick() -> dict:
    started = _now()
    results: dict = {}
    steps: list = []

    _step(steps, results, "mtm", "mtm.py", ["show"])
    _step(steps, results, "regime", "regime.py", [])
    _step(steps, results, "sentiment", "sentiment.py", [])
    _step(steps, results, "social", "social_sentiment.py", ["--no-watchlist"], timeout=60)
    _step(steps, results, "sectors", "sector_scan.py", [])
    _step(steps, results, "flow", "flow_scan.py", ["--majors"])
    _step(steps, results, "watchlist_check", "watchlist_check.py", [])
    _step(steps, results, "position_review", "position_review.py", [])
    _step(steps, results, "leap_anchor_spy", "leap_check.py",
          ["--underlying", "SPY", "--dte-days", "540"])
    _step(steps, results, "leap_aggressive_spy", "leap_check.py",
          ["--underlying", "SPY", "--dte-days", "180"])

    # Single-stock LEAP candidates (config/leap_stocks.json)
    _leap_stock_steps(steps, results)

    ended = _now()
    portfolio = _portfolio_summary(results)
    pr = results.get("position_review", {}).get("data", {}) or {}
    spy_reg = (results.get("regime", {}).get("data") or {}).get("spy_regime", "?")
    vix_b = (results.get("regime", {}).get("data") or {}).get("vix_bucket", "?")
    all_flags = []
    actions = []
    for r in results.values():
        if r:
            all_flags.extend(r.get("flags", []))
    for rev in pr.get("reviews", []):
        if rev.get("primary_action") != "HOLD":
            actions.append({
                "kind": "position_action", "ticker": rev["ticker"],
                "action": rev["primary_action"], "reasons": rev.get("reasons", []),
            })
    try:
        from _tradable import tag_actions
        actions = tag_actions(actions)
    except Exception as e:
        print(f"[brief] WARN tag_actions failed: {e}", file=sys.stderr)
    actions.extend(_fired_alert_actions())  # fired alerts: re-validate, never blind-act
    digest = {
        "ts": ended,
        "started": started,
        "mode": "quick",
        "headline": (f"{spy_reg} | VIX {vix_b} | "
                     f"user_book {portfolio.get('user_open_count', 0)} open"
                     if portfolio.get('user_open_count')
                     else f"{spy_reg} | VIX {vix_b} | equity ${portfolio.get('equity')}"),
        "freshness": _read_freshness(),
        "portfolio": portfolio,
        "open_positions_review": pr.get("reviews", []),
        "regime_summary": {"spy_regime": spy_reg, "vix_bucket": vix_b},
        "leap_signal": {
            "anchor_540dte": (results.get("leap_anchor_spy") or {}).get("data"),
            "aggressive_180dte": (results.get("leap_aggressive_spy") or {}).get("data"),
            "single_stocks": _collect_leap_stock_results(results),
        },
        "flags": sorted(set(all_flags)),
        "actions": actions,
        "steps_log": steps,
    }
    subprocess.run([
        str(PY), str(SCRIPTS / "research.py"), "log",
        "--start", started, "--end", ended, "--kind", "quick",
        "--scripts", ",".join(s["name"] for s in steps),
        "--summary", (f"quick refresh; {spy_reg}; "
                      f"user_book {portfolio.get('user_open_count', 0)} open"
                      if portfolio.get('user_open_count')
                      else f"quick refresh; {spy_reg}; equity ${portfolio.get('equity')}"),
    ], capture_output=True)
    return digest


def run_hourly() -> dict:
    """Hourly intraday refresh. Cheap (~30-60s). Reuses morning's cached
    scanner / breadth / insider / congress / earnings-universe data via
    research.py freshness gating; only re-pulls what changes within the hour
    (regime, sentiment, movers, flow, watchlist, position_review).

    The hourly_broadcast.py orchestrator diffs this digest against today's
    morning_digest cache to surface NEW intraday signals only.
    """
    started = _now()
    results: dict = {}
    steps: list = []

    _step(steps, results, "mtm", "mtm.py", ["show"])
    _step(steps, results, "regime", "regime.py", [])
    _step(steps, results, "sentiment", "sentiment.py", [])
    _step(steps, results, "social", "social_sentiment.py", ["--no-watchlist"], timeout=60)
    _step(steps, results, "flow", "flow_scan.py", ["--majors"])
    _step(steps, results, "movers", "movers.py", ["--gainers", "--losers", "--actives"])
    _step(steps, results, "watchlist_check", "watchlist_check.py", [])
    _step(steps, results, "position_review", "position_review.py", [])

    ended = _now()
    portfolio = _portfolio_summary(results)
    pr = results.get("position_review", {}).get("data", {}) or {}
    spy_reg = (results.get("regime", {}).get("data") or {}).get("spy_regime", "?")
    vix_b = (results.get("regime", {}).get("data") or {}).get("vix_bucket", "?")

    all_flags: list = []
    for r in results.values():
        if r:
            all_flags.extend(r.get("flags", []))

    actions: list = []
    for r in results.values():
        if r:
            actions.extend(r.get("actions", []))
    for rev in pr.get("reviews", []):
        if rev.get("primary_action") != "HOLD":
            actions.append({
                "kind": "position_action", "ticker": rev["ticker"],
                "action": rev["primary_action"], "reasons": rev.get("reasons", []),
            })
    try:
        from _tradable import tag_actions
        actions = tag_actions(actions)
    except Exception as e:
        print(f"[brief] WARN tag_actions failed: {e}", file=sys.stderr)

    candidates = _build_candidates(results)

    actions.extend(_fired_alert_actions())  # fired alerts: re-validate, never blind-act
    digest = {
        "ts": ended,
        "started": started,
        "mode": "hourly",
        "headline": (f"{spy_reg} | VIX {vix_b} | "
                     f"user_book {portfolio.get('user_open_count', 0)} open"
                     if portfolio.get('user_open_count')
                     else f"{spy_reg} | VIX {vix_b}"),
        "freshness": _read_freshness(),
        "portfolio": portfolio,
        "open_positions_review": pr.get("reviews", []),
        "regime_summary": {
            "spy_regime": spy_reg,
            "vix_bucket": vix_b,
            "vix_term_structure": (results.get("sentiment", {}).get("data") or {}).get("term_structure"),
        },
        "watchlist": [
            {k: v for k, v in it.items() if k in ("ticker", "status", "last", "entry_trigger", "dist_pct")}
            for it in (results.get("watchlist_check", {}).get("data", {}) or {}).get("items", [])
        ],
        "candidates": candidates,
        "top_movers": _top_movers(results),
        "flags": sorted(set(all_flags)),
        "actions": actions,
        "steps_log": steps,
    }

    # Persist hourly digest by timestamp for orchestrator + audit.
    try:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        cache_dir = ROOT / "state" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        hourly_path = cache_dir / f"hourly_digest_{stamp}.json"
        with hourly_path.open("w") as f:
            json.dump(digest, f, separators=(",", ":"), default=str)
    except Exception as e:
        print(f"[brief] WARN failed to persist hourly_digest cache: {e}",
              file=sys.stderr)

    summary_one = (f"hourly; {spy_reg}; VIX={vix_b}; "
                   f"candidates={len(candidates)}; "
                   f"actions={len([a for a in actions if a.get('kind') == 'position_action'])}")
    subprocess.run([
        str(PY), str(SCRIPTS / "research.py"), "log",
        "--start", started, "--end", ended, "--kind", "hourly",
        "--scripts", ",".join(s["name"] for s in steps),
        "--summary", summary_one,
    ], capture_output=True)

    return digest


def run_status() -> dict:
    results: dict = {}
    steps: list = []
    _step(steps, results, "mtm", "mtm.py", ["show"])
    _step(steps, results, "position_review", "position_review.py", [])
    pr = results.get("position_review", {}).get("data", {}) or {}
    actions = []
    for rev in pr.get("reviews", []):
        if rev.get("primary_action") != "HOLD":
            actions.append({
                "kind": "position_action", "ticker": rev["ticker"],
                "action": rev["primary_action"], "reasons": rev.get("reasons", []),
            })
    actions.extend(_fired_alert_actions())  # fired alerts: re-validate, never blind-act
    return {
        "ts": _now(),
        "mode": "status",
        "freshness": _read_freshness(),
        "portfolio": _portfolio_summary(results),
        "open_positions_review": pr.get("reviews", []),
        "actions": actions,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["morning", "quick", "hourly", "status"])
    ap.add_argument("--pretty", action="store_true",
                    help="Pretty-print JSON (debug; default is compact one-line)")
    args = ap.parse_args()

    if args.mode == "morning":
        digest = run_morning()
    elif args.mode == "quick":
        digest = run_quick()
    elif args.mode == "hourly":
        digest = run_hourly()
    else:
        digest = run_status()

    if args.pretty:
        print(json.dumps(digest, indent=2, default=str))
    else:
        print(json.dumps(digest, separators=(",", ":"), default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
