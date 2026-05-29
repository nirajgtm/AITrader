#!/usr/bin/env python3
"""
refresh_site_prices.py — cron-driven price refresh for market-watch briefs.json

Touches only price fields on the most recent brief (no RSI / MA / sector scores
since those need history). Commits + pushes to GitHub Pages only when something
actually changed.

Fields refreshed:
  - macro.indices[].last           (SPY, QQQ, IWM, ...)
  - macro.vol_yields.{vix,ten_year,dxy,oil,gold}
  - stocks.watchlist[].last and .change_pct
  - crypto.coins[].last            (when crypto.status == "live")
  - last_refreshed_at              (ISO timestamp on the brief)

Default behavior is gated to US weekday extended hours (04:00 to 20:00 ET) so
weekend / overnight runs do not fight yfinance's inconsistent session
boundaries. Use --always to bypass the gate.

Usage:
  refresh_site_prices.py                 gated to US weekday 04:00-20:00 ET
  refresh_site_prices.py --always        ignore the trading-hours gate
  refresh_site_prices.py --dry           update file, no git
  refresh_site_prices.py --no-git        same as --dry
  refresh_site_prices.py --verbose       per-symbol logging

Cron line (every 30 min around the clock; the script self-gates):
  */30 * * * * /path/to/trader/scripts/.venv/bin/python3 /path/to/trader/scripts/refresh_site_prices.py >> /path/to/trader/state/site_refresh.log 2>&1
"""

import argparse
import json
import os
import subprocess
import sys
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

SITE_DIR = Path.home() / "claude-configs" / "trader-site"
BRIEFS = SITE_DIR / "briefs.json"
LOCK = Path("/tmp/market-watch-refresh.lock")

PROXY_SYMBOLS = {
    "vix":      "^VIX",
    "ten_year": "^TNX",
    "dxy":      "DX-Y.NYB",
    "oil":      "CL=F",
    "gold":     "GC=F",
}

CRYPTO_MAP = {
    "BTC":  "BTC-USD",
    "ETH":  "ETH-USD",
    "SOL":  "SOL-USD",
    "XRP":  "XRP-USD",
    "DOGE": "DOGE-USD",
}


def log(msg, *, verbose_only=False, verbose=False):
    if verbose_only and not verbose:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def in_extended_hours_now():
    """US weekday + 04:00 to 20:00 ET (covers pre-market, RTH, after-hours)."""
    try:
        from zoneinfo import ZoneInfo
        ny = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        ny = datetime.now(timezone(timedelta(hours=-4)))
    if ny.weekday() >= 5:
        return False
    mins = ny.hour * 60 + ny.minute
    return 240 <= mins <= 1200


def acquire_lock():
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text().strip())
            os.kill(pid, 0)
            log(f"another refresh is running (pid {pid}), exiting")
            sys.exit(0)
        except (ProcessLookupError, ValueError, PermissionError):
            pass
    LOCK.write_text(str(os.getpid()))


def release_lock():
    try:
        LOCK.unlink()
    except FileNotFoundError:
        pass


def fetch_quotes(symbols, verbose=False):
    """Batch-fetch last price + day-pct via yfinance fast_info. Returns {sym: {last, chg_pct}}."""
    import yfinance as yf
    out = {}
    syms = sorted(set(s for s in symbols if s))
    if not syms:
        return out
    log(f"fetching {len(syms)} symbols: {' '.join(syms)}", verbose_only=False, verbose=verbose)
    tickers = yf.Tickers(" ".join(syms))
    for s in syms:
        try:
            t = tickers.tickers[s]
            fi = t.fast_info
            last = None
            prev = None
            for k in ("last_price", "lastPrice", "regular_market_price"):
                if hasattr(fi, k):
                    last = getattr(fi, k)
                    if last is not None:
                        break
                if isinstance(fi, dict) and k in fi:
                    last = fi[k]
                    if last is not None:
                        break
            for k in ("previous_close", "previousClose", "regular_market_previous_close"):
                if hasattr(fi, k):
                    prev = getattr(fi, k)
                    if prev is not None:
                        break
                if isinstance(fi, dict) and k in fi:
                    prev = fi[k]
                    if prev is not None:
                        break
            if last is None:
                log(f"  {s}: no last price", verbose_only=True, verbose=verbose)
                continue
            chg = None
            if prev:
                try:
                    chg = (float(last) - float(prev)) / float(prev) * 100.0
                except Exception:
                    chg = None
            out[s] = {"last": float(last), "chg_pct": chg}
            log(f"  {s}: {last} ({chg:+.2f}% vs prev)" if chg is not None else f"  {s}: {last}",
                verbose_only=True, verbose=verbose)
        except Exception as e:
            log(f"  {s} failed: {e}")
    return out


def round_to(x, dp):
    if x is None:
        return None
    try:
        return round(float(x), dp)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--always", action="store_true", help="bypass the weekday extended-hours gate")
    ap.add_argument("--dry", action="store_true", help="update briefs.json, no git")
    ap.add_argument("--no-git", action="store_true", help="same as --dry")
    ap.add_argument("--verbose", action="store_true", help="per-symbol logging")
    args = ap.parse_args()

    if not args.always and not in_extended_hours_now():
        log("outside US weekday extended hours, skipping (use --always to bypass)")
        return 0

    if not BRIEFS.exists():
        log(f"briefs.json not found at {BRIEFS}, exiting")
        return 1

    acquire_lock()
    try:
        with open(BRIEFS) as f:
            data = json.load(f)
        briefs = data.get("briefs") or []
        if not briefs:
            log("no briefs in briefs.json, exiting")
            return 0
        brief = briefs[0]

        symbols = []
        for i in (brief.get("macro", {}) or {}).get("indices", []) or []:
            if i.get("ticker"):
                symbols.append(i["ticker"])
        vy = (brief.get("macro", {}) or {}).get("vol_yields", {}) or {}
        for key in vy:
            if key in PROXY_SYMBOLS:
                symbols.append(PROXY_SYMBOLS[key])
        for w in (brief.get("stocks", {}) or {}).get("watchlist", []) or []:
            if w.get("ticker"):
                symbols.append(w["ticker"])
        crypto = brief.get("crypto", {}) or {}
        if crypto.get("status") == "live":
            for c in crypto.get("coins", []) or []:
                sym = c.get("symbol")
                if sym:
                    symbols.append(CRYPTO_MAP.get(sym, sym + "-USD"))

        quotes = fetch_quotes(symbols, verbose=args.verbose)
        if not quotes:
            log("no quotes returned, exiting")
            return 0

        changed_fields = []

        for i in (brief.get("macro", {}) or {}).get("indices", []) or []:
            t = i.get("ticker")
            if t in quotes and quotes[t].get("last") is not None:
                new = round_to(quotes[t]["last"], 2)
                if i.get("last") != new:
                    i["last"] = new
                    changed_fields.append(f"index {t} -> {new}")

        for key, sym in PROXY_SYMBOLS.items():
            if key not in vy:
                continue
            if sym in quotes and quotes[sym].get("last") is not None:
                dp = 2 if key != "gold" else 2
                new = round_to(quotes[sym]["last"], dp)
                if vy.get(key) != new:
                    vy[key] = new
                    changed_fields.append(f"{key} -> {new}")

        for w in (brief.get("stocks", {}) or {}).get("watchlist", []) or []:
            t = w.get("ticker")
            if t in quotes and quotes[t].get("last") is not None:
                new_last = round_to(quotes[t]["last"], 2)
                new_chg = round_to(quotes[t].get("chg_pct"), 2)
                if w.get("last") != new_last:
                    w["last"] = new_last
                    changed_fields.append(f"watchlist {t}.last -> {new_last}")
                if new_chg is not None and w.get("change_pct") != new_chg:
                    w["change_pct"] = new_chg
                    changed_fields.append(f"watchlist {t}.change_pct -> {new_chg}")

        if crypto.get("status") == "live":
            for c in crypto.get("coins", []) or []:
                sym = c.get("symbol")
                yf_sym = CRYPTO_MAP.get(sym, (sym + "-USD") if sym else None)
                if yf_sym and yf_sym in quotes and quotes[yf_sym].get("last") is not None:
                    new_last = round_to(quotes[yf_sym]["last"], 2)
                    if c.get("last") != new_last:
                        c["last"] = new_last
                        changed_fields.append(f"crypto {sym}.last -> {new_last}")

        if not changed_fields:
            log("no price changes, exiting")
            return 0

        brief["last_refreshed_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        data["briefs"][0] = brief
        tmp = BRIEFS.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, BRIEFS)
        log(f"updated {len(changed_fields)} field(s): " + ", ".join(changed_fields[:6]) +
            (f" (+{len(changed_fields)-6} more)" if len(changed_fields) > 6 else ""))

        # Refresh sparks + market_status without touching detail/recommendations.
        # --skip-scanners keeps the run fast (no movers/wsb/flow re-fetch); those
        # already update on every publish_site.sh and don't need 30-min cadence.
        enrich = Path(__file__).resolve().parent / "enrich_briefs.py"
        if enrich.exists():
            try:
                subprocess.run([sys.executable, str(enrich), "--skip-scanners"],
                               check=True, capture_output=True, timeout=60)
                log("enriched sparks + market_status")
            except subprocess.CalledProcessError as e:
                log(f"enrich failed (non-fatal): {e}")
            except subprocess.TimeoutExpired:
                log("enrich timeout (non-fatal)")

        if args.dry or args.no_git:
            log("dry mode: skipped git ops")
            return 0

        try:
            subprocess.run(["git", "add", "briefs.json"], cwd=SITE_DIR, check=True)
            msg = f"refresh prices {brief.get('date')} {datetime.now().strftime('%H:%M %Z').strip()}"
            subprocess.run(["git", "commit", "-m", msg], cwd=SITE_DIR, check=True,
                           stdout=subprocess.DEVNULL)
            subprocess.run(["git", "push", "origin", "main"], cwd=SITE_DIR, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            log(f"pushed: {msg}")
        except subprocess.CalledProcessError as e:
            log(f"git error: {e}")
            return 1
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
