#!/usr/bin/env python3
"""Content-sufficiency gate for staging.json.

Run as: validate_brief.py [path/to/staging.json]
Exits 0 if every tab carries the required rich data; non-zero on any gap.

Run by publish_site.sh before merging into briefs.json so that thin or
half-formed briefs never reach the live site. The skill (or a human) is
expected to refill the gaps before the next publish.
"""
import json
import sys
from pathlib import Path


def fail(errors, where, message):
    errors.append(f"  {where}: {message}")


def need_modes(obj, where, errors):
    if not isinstance(obj, dict):
        fail(errors, where, "expected {plain, pro} object")
        return
    for k in ("plain", "pro"):
        if not (obj.get(k) or "").strip():
            fail(errors, where, f"missing .{k}")


def validate_tab_intro(intro, name, min_bullets, errors):
    if not isinstance(intro, dict):
        fail(errors, f"{name}.tab_intro", "missing object")
        return
    bullets = intro.get("bullets") or []
    if len(bullets) < min_bullets:
        fail(errors, f"{name}.tab_intro.bullets",
             f"only {len(bullets)} bullets (need ≥ {min_bullets})")
    for i, b in enumerate(bullets):
        need_modes(b, f"{name}.tab_intro.bullets[{i}]", errors)


def validate_rec(r, where, errors):
    for k in ("ticker", "verdict", "vehicle", "pros", "cons"):
        if not (r.get(k) or ""):
            fail(errors, where, f"missing .{k}")
    if r.get("confidence") is None:
        fail(errors, where, "missing .confidence")
    if not (r.get("confidence_reason") or "").strip():
        fail(errors, where, "missing .confidence_reason")
    need_modes(r.get("action"), f"{where}.action", errors)


def validate_detail_strings(item, where, errors):
    """Detail blocks must be {plain, pro} on every list item the page renders."""
    detail = item.get("detail")
    if detail is None:
        fail(errors, where, "missing .detail")
        return
    need_modes(detail, f"{where}.detail", errors)


def validate(brief):
    errors = []

    # --- Top-level ---
    for k in ("date", "updated_at", "regime", "vix_bucket"):
        if not (brief.get(k) or ""):
            fail(errors, "<root>", f"missing .{k}")

    # Per-tab actions[]: same shape as legacy top_actions, with required
    # robinhood on every ACTION-tier row.
    def validate_per_tab_actions(actions, where_prefix):
        for i, a in enumerate(actions or []):
            where = f"{where_prefix}[{i}]"
            for k in ("verb", "target", "text"):
                if not (a.get(k) or ""):
                    fail(errors, where, f"missing .{k}")
            validate_detail_strings(a, where, errors)
            verb = str(a.get("verb") or "").upper()
            if "ACTION" in verb and "NO" not in verb:
                rh = a.get("robinhood")
                if not isinstance(rh, dict):
                    fail(errors, where, "missing .robinhood (required for ACTION-tier)")
                else:
                    need_modes(rh, f"{where}.robinhood", errors)

    for tab in ("macro", "stocks", "options", "crypto", "emerging"):
        actions = (brief.get(tab) or {}).get("actions") or []
        validate_per_tab_actions(actions, f"{tab}.actions")

    # --- MACRO ---
    macro = brief.get("macro") or {}
    validate_tab_intro(macro.get("tab_intro"), "macro", 3, errors)
    if len(macro.get("indices") or []) < 3:
        fail(errors, "macro.indices", f"only {len(macro.get('indices') or [])} entries (need ≥ 3: SPY/QQQ/IWM)")
    for i, x in enumerate(macro.get("indices") or []):
        for k in ("ticker", "last", "note"):
            if not (x.get(k) is not None and x.get(k) != ""):
                fail(errors, f"macro.indices[{i}]", f"missing .{k}")
        validate_detail_strings(x, f"macro.indices[{i}]", errors)

    vy = macro.get("vol_yields") or {}
    for k in ("vix", "vix_term", "ten_year"):
        if vy.get(k) is None:
            fail(errors, f"macro.vol_yields", f"missing .{k}")

    sr = macro.get("sector_rotation") or {}
    if len(sr.get("leaders_5d") or []) < 2:
        fail(errors, "macro.sector_rotation.leaders_5d", "need ≥ 2 leaders")
    if len(sr.get("laggards_5d") or []) < 2:
        fail(errors, "macro.sector_rotation.laggards_5d", "need ≥ 2 laggards")
    if not (sr.get("read") or ""):
        fail(errors, "macro.sector_rotation.read", "empty")

    if len(macro.get("events_14d") or []) < 1:
        fail(errors, "macro.events_14d", "no events listed")
    for i, e in enumerate(macro.get("events_14d") or []):
        validate_detail_strings(e, f"macro.events_14d[{i}]", errors)

    for i, e in enumerate(macro.get("earnings_7d") or []):
        for k in ("ticker", "date", "note"):
            if not (e.get(k) or ""):
                fail(errors, f"macro.earnings_7d[{i}]", f"missing .{k}")
        validate_detail_strings(e, f"macro.earnings_7d[{i}]", errors)

    for side in ("buy", "sell"):
        for i, r in enumerate((macro.get("recommendations") or {}).get(side) or []):
            validate_rec(r, f"macro.recs.{side}[{i}]", errors)

    # --- STOCKS ---
    stocks = brief.get("stocks") or {}
    validate_tab_intro(stocks.get("tab_intro"), "stocks", 3, errors)
    if len(stocks.get("watchlist") or []) < 3:
        fail(errors, "stocks.watchlist", f"only {len(stocks.get('watchlist') or [])} names (need ≥ 3)")
    for i, w in enumerate(stocks.get("watchlist") or []):
        for k in ("ticker", "last", "change_pct", "trigger_zone", "status", "note"):
            if w.get(k) is None or w.get(k) == "":
                fail(errors, f"stocks.watchlist[{i}]", f"missing .{k}")
        validate_detail_strings(w, f"stocks.watchlist[{i}]", errors)
    for side in ("buy", "sell"):
        for i, r in enumerate((stocks.get("recommendations") or {}).get(side) or []):
            validate_rec(r, f"stocks.recs.{side}[{i}]", errors)

    # --- OPTIONS ---
    options = brief.get("options") or {}
    validate_tab_intro(options.get("tab_intro"), "options", 3, errors)
    for i, x in enumerate(options.get("earnings_iv") or []):
        validate_detail_strings(x, f"options.earnings_iv[{i}]", errors)
    for i, x in enumerate(options.get("leaps") or []):
        validate_detail_strings(x, f"options.leaps[{i}]", errors)
    if len(options.get("wheel_candidates") or []) < 3:
        fail(errors, "options.wheel_candidates",
             f"only {len(options.get('wheel_candidates') or [])} ideas (need ≥ 3 from wheel scan)")
    for i, w in enumerate(options.get("wheel_candidates") or []):
        for k in ("ticker", "verdict", "spot", "csp_strike", "csp_premium",
                 "csp_dte", "annualized_yield_pct", "pros", "cons"):
            if w.get(k) is None or w.get(k) == "":
                fail(errors, f"options.wheel_candidates[{i}]", f"missing .{k}")
        if w.get("confidence") is None:
            fail(errors, f"options.wheel_candidates[{i}]", "missing .confidence")
        if not (w.get("confidence_reason") or "").strip():
            fail(errors, f"options.wheel_candidates[{i}]", "missing .confidence_reason")
        need_modes(w.get("action"), f"options.wheel_candidates[{i}].action", errors)
    for side in ("buy", "sell"):
        for i, r in enumerate((options.get("recommendations") or {}).get(side) or []):
            validate_rec(r, f"options.recs.{side}[{i}]", errors)

    # --- CRYPTO ---
    crypto = brief.get("crypto") or {}
    validate_tab_intro(crypto.get("tab_intro"), "crypto", 3, errors)
    if (crypto.get("status") or "live") == "live":
        if len(crypto.get("coins") or []) < 3:
            fail(errors, "crypto.coins", "need ≥ 3 coins when status=live")
        for i, c in enumerate(crypto.get("coins") or []):
            for k in ("symbol", "last", "change_5d_pct", "note"):
                if c.get(k) is None or c.get(k) == "":
                    fail(errors, f"crypto.coins[{i}]", f"missing .{k}")
            validate_detail_strings(c, f"crypto.coins[{i}]", errors)
    # Crypto recommendations are allowed empty.

    # --- EMERGING (forward-looking: accumulation + secular themes) ---
    emerging = brief.get("emerging") or {}
    if emerging:
        validate_tab_intro(emerging.get("tab_intro"), "emerging", 3, errors)
        accum = emerging.get("accumulation") or []
        for i, a in enumerate(accum):
            for k in ("ticker", "last", "score", "buys_60d", "rsi14", "note"):
                if a.get(k) is None or a.get(k) == "":
                    fail(errors, f"emerging.accumulation[{i}]", f"missing .{k}")
            validate_detail_strings(a, f"emerging.accumulation[{i}]", errors)
        for i, t in enumerate(emerging.get("themes") or []):
            for k in ("theme", "names", "horizon", "vehicle_bias", "note"):
                if not (t.get(k) or ""):
                    fail(errors, f"emerging.themes[{i}]", f"missing .{k}")
            validate_detail_strings(t, f"emerging.themes[{i}]", errors)
        for side in ("buy", "sell"):
            for i, r in enumerate((emerging.get("recommendations") or {}).get(side) or []):
                validate_rec(r, f"emerging.recs.{side}[{i}]", errors)

    return errors


def main():
    path = Path(sys.argv[1] if len(sys.argv) > 1 else
                Path(__file__).resolve().parent.parent.parent / "trader-site" / "staging.json")
    brief = json.loads(path.read_text())
    errors = validate(brief)
    if errors:
        print("BRIEF VALIDATION FAILED")
        for e in errors:
            print(e)
        print(f"\n{len(errors)} issue(s).")
        sys.exit(1)
    print(f"OK · {path.name} passes the content-sufficiency gate.")
    sys.exit(0)


if __name__ == "__main__":
    main()
