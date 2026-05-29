#!/usr/bin/env python3
"""enrich_briefs.py — refresh the active brief in briefs.json with live data
   pulled from the production scanners, plus computed fields the new newsroom
   site needs but the brief schema doesn't yet produce.

Adds / refreshes on the newest brief:
  - sparks            14-trading-day close history per ticker (macro indices,
                      futures proxies, watchlist) — for inline sparklines
  - stocks.movers     gainers/losers from movers.py (Yahoo/FMP screeners)
                      with sparks attached
  - stocks.wsb_top    refreshed from social_sentiment.py (apewisdom)
  - options.unusual   refreshed from flow_scan.py --majors
  - market_status     {tier, text} computed from current ET time

Does not invent thesis text — `detail` fields stay absent until the /trader
skill produces them on the next daily brief.

Usage:
  enrich_briefs.py                     refresh everything, write in place
  enrich_briefs.py --dry               show what would change, do not write
  enrich_briefs.py --verbose           per-symbol logging
  enrich_briefs.py --skip-scanners     only sparks + market_status (fast)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

SCRIPTS_DIR = Path(__file__).resolve().parent
SITE_DIR = Path.home() / "claude-configs" / "trader-site"
BRIEFS = SITE_DIR / "briefs.json"
PRICES = SITE_DIR / "prices.json"
PYBIN = SCRIPTS_DIR / ".venv" / "bin" / "python3"

# Words that look like tickers (uppercase, ≤6 chars) but aren't tradable —
# they appear as topic targets on Today's Actions ("INDEX: rotation", "VIX:
# spike fading"). yfinance can return data for some, but we never want them
# in the price archive.
NON_TICKER_WORDS = {
    "INDEX", "MARKET", "MACRO", "GLOBAL", "SECTOR", "REGIME",
    "EQUITY", "BONDS", "RATES", "CRYPTO", "VIX",
}

PROXY_SYMBOLS = {
    "vix":      "^VIX",
    "ten_year": "^TNX",
    "dxy":      "DX-Y.NYB",
    "oil":      "CL=F",
    "gold":     "GC=F",
}
PROXY_REVERSE = {v: k for k, v in PROXY_SYMBOLS.items()}

SPARK_DAYS = 14


def log(msg, *, verbose_only=False, verbose=False):
    if verbose_only and not verbose:
        return
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def market_status_now():
    """Return {tier, text} describing current US equity session state."""
    try:
        from zoneinfo import ZoneInfo
        ny = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        ny = datetime.now(timezone(timedelta(hours=-4)))

    weekday = ny.weekday()
    mins = ny.hour * 60 + ny.minute

    def next_weekday(d):
        nxt = d + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt = nxt + timedelta(days=1)
        return nxt

    if weekday >= 5:
        nxt = next_weekday(ny.replace(hour=9, minute=30, second=0, microsecond=0))
        return {"tier": "MARKET CLOSED",
                "text": f"Weekend. Next live session {nxt.strftime('%A, %b %-d')} at 09:30 ET."}
    if mins < 240:
        return {"tier": "MARKET CLOSED", "text": "Pre-market opens at 04:00 ET."}
    if mins < 570:
        opens_in = 570 - mins
        return {"tier": "PRE-MARKET",
                "text": f"Pre-market trading. Opening bell in {opens_in // 60}h {opens_in % 60:02d}m at 09:30 ET."}
    if mins < 960:
        closes_in = 960 - mins
        return {"tier": "MARKET OPEN",
                "text": f"Regular session. Closing bell in {closes_in // 60}h {closes_in % 60:02d}m at 16:00 ET."}
    if mins < 1200:
        return {"tier": "AFTER-HOURS",
                "text": "After-hours trading. Session closes 20:00 ET."}
    nxt = ny + timedelta(days=1)
    if nxt.weekday() >= 5:
        nxt = next_weekday(nxt)
    return {"tier": "MARKET CLOSED",
            "text": f"After-hours over. Next pre-market opens {nxt.strftime('%A, %b %-d')} at 04:00 ET."}


def fetch_history(symbols, days, verbose=False):
    """Fetch the last `days` daily closes per symbol. {sym: [closes...]}, oldest first."""
    import yfinance as yf
    syms = sorted(set(s for s in symbols if s))
    if not syms:
        return {}
    log(f"fetching {days}-day history for {len(syms)} symbols")
    period = f"{max(days * 2, 30)}d"
    df = yf.download(" ".join(syms), period=period, interval="1d",
                     progress=False, auto_adjust=False, threads=True)
    if df is None or df.empty:
        log("yfinance returned empty frame")
        return {}
    out = {}
    if len(syms) == 1:
        closes = df["Close"].dropna().tolist()[-days:]
        if closes:
            out[syms[0]] = [round(float(x), 4) for x in closes]
        return out
    try:
        close_block = df["Close"]
    except KeyError:
        return {}
    for s in syms:
        if s not in close_block.columns:
            continue
        series = close_block[s].dropna().tolist()
        if not series:
            continue
        out[s] = [round(float(x), 4) for x in series[-days:]]
        log(f"  {s}: {len(out[s])} pts, last {out[s][-1]}", verbose_only=True, verbose=verbose)
    return out


def collect_brief_symbols(brief):
    syms = set()
    for i in (brief.get("macro") or {}).get("indices", []) or []:
        if i.get("ticker"):
            syms.add(i["ticker"])
    vy = (brief.get("macro") or {}).get("vol_yields") or {}
    for key in vy:
        if key in PROXY_SYMBOLS:
            syms.add(PROXY_SYMBOLS[key])
    for w in (brief.get("stocks") or {}).get("watchlist", []) or []:
        if w.get("ticker"):
            syms.add(w["ticker"])
    for a in brief.get("top_actions", []) or []:
        t = (a or {}).get("target") or ""
        if re.fullmatch(r"[A-Z]{1,6}", t):
            syms.add(t)
    return syms


def build_sparks(history):
    out = {}
    for sym, closes in history.items():
        out[sym] = closes
        if sym in PROXY_REVERSE:
            out[PROXY_REVERSE[sym]] = closes
    return out


# ── Per-ticker price archive (prices.json) ─────────────────────────────
# Folded into enrich_briefs.py so the daily/30-min runs that already exist
# also grow the chart archive. No separate cron needed.

def discover_archive_universe(brief):
    """Walk every ticker-bearing field across the brief — recommendations,
    per-tab actions, leaps, wheel candidates, social, watchlist, indices,
    sector rotation, earnings, movers, wsb. The fetcher drops anything
    yfinance can't resolve, so this side stays inclusive."""
    out = set()
    def add(s):
        if not isinstance(s, str):
            return
        s = s.strip()
        if s and s not in NON_TICKER_WORDS and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,7}", s):
            out.add(s)
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("ticker", "target", "etf", "sym"):
                    add(v)
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(brief)
    out.update(PROXY_SYMBOLS.values())
    return out


def _ser_pairs(ser):
    """Series(date -> close) into [(YYYY-MM-DD, int_close), ...].
    Guards the index in case a stray non-datetime label slips through."""
    pairs = []
    for d, c in ser.dropna().items():
        ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        pairs.append((ds, int(round(float(c)))))
    return pairs


def fetch_dated(syms, period):
    """yfinance closes as [(YYYY-MM-DD, int_close), ...] per symbol.
    Whole-dollar rounding — the chart doesn't display decimals."""
    import yfinance as yf
    syms = sorted(set(s for s in syms if s))
    if not syms:
        return {}
    df = yf.download(" ".join(syms), period=period, interval="1d",
                     progress=False, auto_adjust=False, threads=True)
    if df is None or df.empty:
        return {}
    out = {}
    if "Close" not in df.columns:
        return {}
    close_block = df["Close"]
    # Newer yfinance returns MultiIndex columns even for a single ticker, so
    # df["Close"] is a 1-column DataFrame, not a Series. Iterating .items() on a
    # DataFrame yields column names (the ticker string), which then fail
    # .strftime(). Normalize the single-symbol case to a real Series.
    if len(syms) == 1:
        if getattr(close_block, "ndim", 1) == 2:
            close_block = close_block.iloc[:, 0]
        pairs = _ser_pairs(close_block)
        if pairs:
            out[syms[0]] = pairs
        return out
    for s in syms:
        if s not in close_block.columns:
            continue
        pairs = _ser_pairs(close_block[s])
        if pairs:
            out[s] = pairs
    return out


def merge_dated(existing, new_pts):
    by_date = {}
    for entry in existing:
        if isinstance(entry, list) and len(entry) >= 2:
            by_date[entry[0]] = entry[1]
    for d, c in new_pts:
        by_date[d] = c
    return [[d, by_date[d]] for d in sorted(by_date.keys())]


def update_price_archive(brief, *, reseed=False, verbose=False):
    """Append today's closes into prices.json. New tickers seed with 1y;
    existing tickers get a 30d sweep merged in. Called at the end of
    every enrich_briefs.py run, so the archive grows on every publish
    AND every 30-min refresh_site_prices.py call."""
    discovered = discover_archive_universe(brief)
    if not discovered:
        return
    if PRICES.exists():
        try:
            with open(PRICES) as f:
                archive = json.load(f)
        except (json.JSONDecodeError, OSError):
            archive = {"version": 1, "tickers": {}}
    else:
        archive = {"version": 1, "tickers": {}}
    tickers_dict = archive.setdefault("tickers", {})

    if reseed:
        seed_syms = sorted(discovered)
        upd_syms = []
    else:
        seed_syms = sorted(discovered - set(tickers_dict.keys()))
        upd_syms = sorted(discovered & set(tickers_dict.keys()))

    if seed_syms:
        log(f"prices archive: seeding {len(seed_syms)} ticker(s) with 1y")
        seed = fetch_dated(seed_syms, period="1y")
        for sym, pts in seed.items():
            tickers_dict[sym] = merge_dated(tickers_dict.get(sym, []), pts)

    added = 0
    if upd_syms:
        upd = fetch_dated(upd_syms, period="30d")
        for sym, pts in upd.items():
            prev = tickers_dict.get(sym, [])
            merged = merge_dated(prev, pts)
            delta = len(merged) - len(prev)
            if delta > 0:
                added += delta
                tickers_dict[sym] = merged
                log(f"  {sym}: +{delta} pt(s) (last {merged[-1][0]} = {merged[-1][1]})",
                    verbose_only=True, verbose=verbose)
        log(f"prices archive: +{added} datapoint(s) across {len(upd_syms)} existing ticker(s)")

    archive["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    tmp = PRICES.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(archive, f, separators=(",", ":"))
    os.replace(tmp, PRICES)
    log(f"prices archive: {len(tickers_dict)} ticker(s) tracked, wrote {PRICES.name}")


def run_script(rel_path, args=None, timeout=120):
    """Run a sibling script with --json, parse, return data dict or None."""
    cmd = [str(PYBIN), str(SCRIPTS_DIR / rel_path)]
    if args:
        cmd.extend(args)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout, cwd=str(SCRIPTS_DIR))
    except subprocess.TimeoutExpired:
        log(f"{rel_path}: timeout after {timeout}s")
        return None
    if out.returncode != 0:
        log(f"{rel_path}: rc={out.returncode}, stderr={out.stderr[:200]}")
        return None
    line = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    if not line:
        log(f"{rel_path}: empty stdout")
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError as e:
        log(f"{rel_path}: bad JSON: {e}")
        return None


def fetch_movers(history, verbose=False):
    """Run movers.py and reshape into the prototype's stocks.movers structure."""
    log("running movers.py --json")
    payload = run_script("movers.py", ["--json"], timeout=60)
    if not payload or not payload.get("ok"):
        log("movers.py failed; skipping")
        return None
    data = payload.get("data") or {}
    gainers = data.get("gainers") or []
    losers = data.get("losers") or []
    most_actives = data.get("most_actives") or []
    if not gainers:
        gainers = [a for a in most_actives if (a.get("pct") or 0) > 0][:4]
    if not losers:
        losers = [a for a in most_actives if (a.get("pct") or 0) < 0][:4]

    def fmt_vol(v):
        if not v:
            return ""
        if v >= 1_000_000:
            return f"{v/1_000_000:.1f}M"
        if v >= 1_000:
            return f"{v/1_000:.0f}K"
        return str(v)

    def make_detail(sym, last, chg, vol, headline):
        dir_word = "up" if chg > 0 else "down" if chg < 0 else "flat"
        chg_abs = abs(chg)
        vol_str = fmt_vol(vol)
        plain_parts = [
            f"{sym} closed {dir_word} {chg_abs:.2f} percent at {last:.2f}",
        ]
        if vol_str:
            plain_parts.append(f"on volume of {vol_str} shares")
        plain_parts.append("today.")
        plain = " ".join(plain_parts)
        if headline:
            plain += f" Headline: {headline}"

        pro_parts = [f"{sym} {('+' if chg >= 0 else '')}{chg:.2f} pct, close {last:.2f}"]
        if vol:
            pro_parts.append(f"vol {vol_str}")
        pro = ", ".join(pro_parts) + "."
        if headline:
            pro += f" Headline: {headline}"
        return {"plain": plain, "pro": pro}

    def shape(rows):
        out = []
        for r in rows[:4]:
            sym = r.get("sym") or r.get("ticker")
            if not sym:
                continue
            spark = history.get(sym, [])
            last = round(float(r.get("px") or 0), 2)
            chg = round(float(r.get("pct") or 0), 2)
            vol = int(r.get("vol") or 0)
            headline = r.get("top_headline")
            row = {
                "ticker": sym,
                "last": last,
                "chg": chg,
                "vol": vol,
                "spark": [round(float(x), 2) for x in spark[-8:]] if spark else [],
                "detail": make_detail(sym, last, chg, vol, headline),
            }
            if headline:
                row["note"] = headline
            out.append(row)
        return out

    return {"gainers": shape(gainers), "losers": shape(losers)}


def fetch_social_payload():
    """One call to social_sentiment.py, cached on first invocation per process."""
    if not hasattr(fetch_social_payload, "_cache"):
        log("running social_sentiment.py --json --no-watchlist")
        fetch_social_payload._cache = run_script(
            "social_sentiment.py", ["--json", "--no-watchlist"], timeout=180)
    return fetch_social_payload._cache


def fetch_wsb(verbose=False):
    payload = fetch_social_payload()
    if not payload or not payload.get("ok"):
        log("social_sentiment.py failed; skipping wsb_top")
        return None
    wsb = (payload.get("data") or {}).get("reddit", {}).get("wallstreetbets") or []
    out = []
    for r in wsb[:5]:
        sym = r.get("ticker")
        if not sym:
            continue
        name = r.get("name") or sym
        score = int(r.get("mentions") or 0)
        rank = int(r.get("rank") or 0)
        rank_prev = int(r.get("rank_24h_ago") or 0)
        delta_pct = r.get("delta_pct")
        delta_str = ""
        if isinstance(delta_pct, (int, float)) and abs(delta_pct) > 1:
            delta_str = f" Mentions are up {delta_pct:.0f} percent versus 24 hours ago"
        rank_change = ""
        if rank_prev and rank_prev != rank:
            direction = "up" if rank < rank_prev else "down"
            rank_change = f", and the name jumped {direction} from rank {rank_prev} to rank {rank}"
        plain = (
            f"{name} ({sym}) has {score} mentions on r/wallstreetbets in the last 24 hours.{delta_str}{rank_change}. "
            f"This is a retail-attention signal, not a directional thesis on its own."
        )
        pro = (
            f"{sym} WSB rank {rank} ({score} mentions)"
            + (f", prior rank {rank_prev}" if rank_prev else "")
            + (f", mentions delta {delta_pct:+.0f} pct" if isinstance(delta_pct, (int, float)) else "")
            + ". Retail attention only; pair with price/flow before treating as actionable."
        )
        out.append({
            "ticker": sym,
            "score": score,
            "rank": rank,
            "rank_prev": rank_prev,
            "delta_pct": round(delta_pct, 1) if isinstance(delta_pct, (int, float)) else None,
            "name": name,
            "detail": {"plain": plain, "pro": pro},
        })
    return out or None


# Per-tab classification universes
MACRO_ETFS = {
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "VTV", "VUG",
    "XLE", "XLK", "XLV", "XLU", "XLY", "XLRE", "XLP", "XLF", "XLB", "XLI", "XLC",
    "GLD", "SLV", "TLT", "IEF", "USO", "UNG", "SMH",
}
CRYPTO_PROXIES = {
    "IBIT", "FBTC", "ETHA", "ETHE", "GBTC",
    "MSTR", "COIN", "MARA", "RIOT", "CLSK", "HUT",
}
# Subs whose top tickers we treat as options-flavored
OPTIONS_SUBS = {"options", "thetagang"}


def _sentiment_label(reddit_subs, st_ratio, mention_delta):
    """Crude sentiment label for display. Conservative: defaults to MIXED."""
    bull = 0
    bear = 0
    if st_ratio is not None:
        if st_ratio >= 0.7:   bull += 2
        elif st_ratio >= 0.55: bull += 1
        elif st_ratio <= 0.3: bear += 2
        elif st_ratio <= 0.45: bear += 1
    if mention_delta is not None:
        if mention_delta > 100:  bull += 1
        elif mention_delta < -50: bear += 1
    if bull > bear and bull >= 2: return "BULLISH"
    if bear > bull and bear >= 2: return "BEARISH"
    if bull or bear: return "MIXED"
    return "NEUTRAL"


def build_per_tab_social():
    """Classify the social payload into per-tab buckets with detail.{plain,pro}.
    Each item describes what people are actually saying (Reddit post titles +
    StockTwits AI summary), not just mention counts."""
    payload = fetch_social_payload()
    if not payload or not payload.get("ok"):
        return None
    data = payload.get("data") or {}
    reddit = data.get("reddit") or {}
    stocktwits_trending = (data.get("stocktwits") or {}).get("trending") or []

    # Build per-ticker aggregate
    agg = {}
    for label, rows in reddit.items():
        for r in (rows or [])[:10]:
            tk = r.get("ticker")
            if not tk:
                continue
            cur = agg.setdefault(tk, {
                "ticker": tk, "mentions": 0, "sources": set(),
                "st_summary": "", "mention_delta": None,
                "top_posts": [],
            })
            cur["mentions"] += int(r.get("mentions") or 0)
            cur["sources"].add(label)
            d = r.get("delta_pct")
            if d is not None:
                if cur["mention_delta"] is None or abs(d) > abs(cur["mention_delta"]):
                    cur["mention_delta"] = d
            # Reddit-direct rows carry top_posts (apewisdom rows don't)
            for tp in r.get("top_posts", []) or []:
                cur["top_posts"].append(tp)

    for r in stocktwits_trending[:15]:
        tk = r.get("symbol") or ""
        if not tk or "." in tk:
            continue
        cur = agg.setdefault(tk, {
            "ticker": tk, "mentions": 0, "sources": set(),
            "st_summary": "", "mention_delta": None,
            "top_posts": [],
        })
        cur["sources"].add("stocktwits")
        if r.get("summary") and not cur["st_summary"]:
            cur["st_summary"] = r["summary"]

    # De-dup posts per ticker by (sub,title), keep highest score
    for tk, row in agg.items():
        seen = {}
        for p in row["top_posts"]:
            key = (p.get("sub", ""), p.get("title", ""))
            if key not in seen or p.get("score", 0) > seen[key].get("score", 0):
                seen[key] = p
        row["top_posts"] = sorted(seen.values(), key=lambda p: p.get("score", 0), reverse=True)[:3]

    # Tab routing (a ticker can appear in multiple tabs)
    buckets = {"macro": [], "stocks": [], "options": [], "crypto": []}
    for tk, row in agg.items():
        sources = row["sources"]
        is_options_focus = bool(sources & OPTIONS_SUBS)
        if tk in MACRO_ETFS:
            buckets["macro"].append(row)
        if tk in CRYPTO_PROXIES:
            buckets["crypto"].append(row)
        if tk not in MACRO_ETFS and tk not in CRYPTO_PROXIES:
            buckets["stocks"].append(row)
        if is_options_focus and tk not in MACRO_ETFS and tk not in CRYPTO_PROXIES:
            buckets["options"].append(row)

    # Sort + cap + compose detail
    out = {}
    for tab, rows in buckets.items():
        rows.sort(key=lambda r: (len(r["sources"]), r["mentions"]), reverse=True)
        top = rows[:5]
        items = []
        for row in top:
            tk = row["ticker"]
            srcs = sorted(row["sources"])
            mentions = row["mentions"]
            delta = row["mention_delta"]
            top_posts = row["top_posts"]
            st_summary = (row["st_summary"] or "").strip()
            if len(st_summary) > 220:
                st_summary = st_summary[:217] + "..."

            # Sentiment: prefer post-derived if we have posts, else mention-delta hint
            if top_posts:
                discussion = derive_discussion_from_posts(top_posts)
                sent = discussion["sentiment"]
            else:
                sent = mention_delta_sentiment(delta)

            # Plain: lead with the strongest post quote, then context
            plain = compose_plain(tk, top_posts, st_summary, srcs, delta, sent)
            # Pro: tighter, includes scores and source labels
            pro = compose_pro(tk, top_posts, st_summary, srcs, mentions, delta, sent)

            items.append({
                "ticker": tk,
                "mentions": mentions,
                "sources": srcs,
                "sentiment": sent,
                "summary": st_summary,
                "top_posts": top_posts,
                "detail": {"plain": plain, "pro": pro},
            })
        out[tab] = items
    return out


def derive_discussion_from_posts(posts):
    """Aggregate sentiment across the top posts using a careful phrase classifier.
    Avoids standalone 'call'/'put' since those are non-directional in option chatter.
    Income strategies (covered calls, cash-secured puts, wheels) read as NEUTRAL."""
    # Multi-word phrases first (more reliable than single words)
    bull_phrases = [
        "buy calls", "long calls", "buying calls", "buy puts" ,  # contradiction protection: "buy puts" later filtered
        "long the dip", "buy the dip", "going long", "all in on", "moon",
        "rally", "breakout", "uptrend", "all-time high", "ath", "lfg",
        "reclaim", "upside surprise", "beat earnings", "beats earnings",
        "accumulate", "ripping", "to the moon", "bullish",
        "strong uptrend", "good growth", "love this stock", "loading up",
    ]
    bear_phrases = [
        "buy puts", "long puts", "buying puts", "shorting", "short the",
        "going short", "crash", "dump", "rejection", "guidance cut",
        "missed earnings", "downgrade", "downgraded", "rolling over",
        "breakdown", "topping out", "rug pull", "bearish", "dead cat",
        "falling knife", "warning sign", "weak guidance", "fade the rally",
    ]
    # Phrases that explicitly read as NEUTRAL income/strategy chatter
    neutral_phrases = [
        "covered call", "cash secured put", "cash-secured put", "csp",
        "wheel", "wheeling", "iron condor", "calendar spread", "strangle",
        "straddle", "premium selling", "writing puts", "writing calls",
        "selling puts", "selling calls", "theta gang", "rolled my",
    ]
    bull = bear = neutral = 0
    for p in posts:
        t = (p.get("title", "") or "").lower()
        # Check neutrals first; they suppress the bull/bear count
        is_neutral_post = any(ph in t for ph in neutral_phrases)
        if is_neutral_post:
            neutral += 1
            continue
        b_hits = sum(1 for ph in bull_phrases if ph in t)
        x_hits = sum(1 for ph in bear_phrases if ph in t)
        # Subtract any "buy puts" double-count from bull
        if "buy puts" in t:
            b_hits -= 1
            x_hits += 1
        if b_hits > x_hits: bull += 1
        elif x_hits > b_hits: bear += 1
        else: neutral += 1
    if bull > bear and bull >= 1: sent = "BULLISH"
    elif bear > bull and bear >= 1: sent = "BEARISH"
    elif bull > 0 and bear > 0: sent = "MIXED"
    else: sent = "NEUTRAL"
    return {"sentiment": sent, "bull_score": bull, "bear_score": bear, "neutral_score": neutral}


def _strip_emoji(s: str) -> str:
    """Remove emoji and other non-text characters per the no-emoji style rule."""
    if not s: return s
    import re as _re
    # Drop chars in common emoji unicode blocks
    out = _re.sub(r'[\U0001F300-\U0001FAFF\U00002600-\U000027BF‍️]', '', s)
    return out.strip()


def mention_delta_sentiment(delta):
    if delta is None: return "NEUTRAL"
    if delta > 100: return "BULLISH"
    if delta < -50: return "BEARISH"
    return "MIXED" if abs(delta) > 0 else "NEUTRAL"


# Social pulse composer — synthesizes a paragraph about WHAT people are saying,
# the lean, and what they're doing (buying, writing puts, wheeling, etc.).
# Subreddit names are intentionally dropped: the reader cares about the pulse,
# not the venue. Keyword matching is intentionally simple — better than naming
# subs by hand, weaker than an LLM, but cheap and deterministic.
TRADE_VERBS = [
    ("writing puts to collect premium",
        ["writing put", "selling put", "sell put", "csp", "cash secured put",
         "short put", "short put vertical", "spv", "put credit"]),
    ("buying calls for upside",
        ["buy call", "buying call", "long call", "calls on", "yolo call"]),
    ("buying puts for downside",
        ["buying put", "long put", "puts on", "yolo put"]),
    ("running the wheel",
        ["wheel", "wheeling"]),
    ("selling covered calls",
        ["covered call", "sell call", "writing call", "call credit"]),
    ("playing defined-risk spreads",
        ["debit spread", "credit spread", "iron condor", "vertical spread"]),
    ("holding through the noise",
        ["holding", "diamond hand", "hodl", "bag hold"]),
    ("trimming or taking profit",
        ["trim", "took profit", "taking profit", "scaling out", "scaled out"]),
    ("adding on weakness",
        ["loaded up", "added", "dca", "scaled in", "scaling in", "buying the dip"]),
]

THEMES = [
    ("AI and data-center demand",     ["ai ", "artificial intelligence", "data center", "datacenter", "gpu", "hbm"]),
    ("memory and storage upcycle",    ["memory", "storage", "ddr", "wafer", "nand", "sndk", "wdc"]),
    ("the earnings reaction",         ["earnings", "guidance", "report", " eps", "beat", " miss", "post-earnings"]),
    ("a fresh breakout",              ["breakout", "ath", "all time high", "all-time high", "new high", "parabolic"]),
    ("a sharp drawdown",              ["dump", "crash", "tank", "rug", " drop", "selloff", "sell-off", "puked"]),
    ("the Fed and rates backdrop",    ["fed ", "fomc", "rate cut", "rate hike", "cpi", "yields"]),
    ("a potential squeeze setup",     ["squeeze", "short interest", "gamma squeeze", "sq potential"]),
    ("rich option premium",           ["high iv", "iv crush", "rich premium", "elevated iv"]),
    ("a sector comparison trade",     [" vs ", "head to head", "head-to-head", "compared to"]),
]


def _signal_text(top_posts):
    """Concatenate titles + body excerpts into a single lowercase blob for keyword matching."""
    if not top_posts: return ""
    blobs = []
    for p in top_posts:
        blobs.append((p.get("title", "") or "").lower())
        blobs.append((p.get("body_excerpt", "") or "").lower())
    return " " + " ".join(blobs) + " "


def derive_pulse(top_posts):
    """Returns ({themes:[str]}, {actions:[str]}) — human-readable fragments, not raw keys."""
    blob = _signal_text(top_posts)
    themes = [label for label, kws in THEMES if any(kw in blob for kw in kws)]
    actions = [label for label, kws in TRADE_VERBS if any(kw in blob for kw in kws)]
    return themes, actions


def _volume_word(mentions, n_sources):
    """Heuristic volume read used as the lead-in."""
    if mentions >= 200 or n_sources >= 6: return "Heavy"
    if mentions >= 80  or n_sources >= 4: return "Moderate"
    if mentions >= 20  or n_sources >= 2: return "Light"
    return "Thin"


def _direction_clause(delta):
    if delta is None: return ""
    if delta >= 100:  return ", up sharply versus yesterday"
    if delta >= 30:   return ", running hotter than yesterday"
    if delta <= -50:  return ", fading versus yesterday"
    if delta <= -20:  return ", cooler than yesterday"
    return ""


def _lean_sentence(sent):
    return {
        "BULLISH": "Tone leans bullish.",
        "BEARISH": "Tone leans bearish.",
        "MIXED":   "Tone is split between bulls and bears.",
        "NEUTRAL": "Tone reads neutral.",
    }.get(sent, "")


def _top_thesis_quote(top_posts, max_len=110):
    if not top_posts: return ""
    title = _strip_emoji((top_posts[0].get("title", "") or "").strip().rstrip("."))
    if not title or len(title) > max_len: return ""
    return title


def compose_plain(ticker, top_posts, st_summary, sources, delta, sent):
    """Plain-mode social pulse: a short paragraph on attention, lean, themes,
    and what people seem to be doing. No subreddit names — the reader cares
    about the pulse, not the venue."""
    if not top_posts and not st_summary:
        return (f"{ticker} is showing up across {len(sources)} communities but no specific "
                f"discussion content surfaced. Treat as observation, not a signal.")
    n_sources = len(sources)
    mentions_guess = sum(p.get("score", 0) for p in (top_posts or []))  # rough proxy when mentions not passed
    parts = []
    parts.append(f"{_volume_word(mentions_guess if mentions_guess > 50 else 50, n_sources)} "
                 f"chatter today{_direction_clause(delta)}.")
    lean = _lean_sentence(sent)
    if lean: parts.append(lean)
    themes, actions = derive_pulse(top_posts)
    if themes:
        parts.append(f"The conversation is centered on {_join_human(themes[:3])}.")
    if actions:
        parts.append(f"Traders are {_join_human(actions[:2])}.")
    quote = _top_thesis_quote(top_posts)
    if quote:
        parts.append(f"The loudest take is: \"{quote}\".")
    return " ".join(parts)


def compose_pro(ticker, top_posts, st_summary, sources, mentions, delta, sent):
    """Pro-mode social pulse: same shape, more compact, uses raw counts."""
    parts = []
    head = f"{mentions} mentions across {len(sources)} communities"
    if delta is not None:
        head += f", {delta:+.0f}% vs prior session"
    parts.append(head + ".")
    parts.append(f"Sentiment {sent.lower()}.")
    themes, actions = derive_pulse(top_posts)
    if themes:
        parts.append("Themes: " + "; ".join(themes[:3]) + ".")
    if actions:
        parts.append("Strategy mix: " + "; ".join(actions[:3]) + ".")
    quote = _top_thesis_quote(top_posts, max_len=140)
    if quote:
        parts.append(f"Top thesis: \"{quote}\".")
    if not top_posts and not st_summary:
        parts.append("No post content cached; treat as a ranking, not a thesis.")
    return " ".join(parts)


def _join_human(items):
    """['a','b','c'] -> 'a, b, and c'. Two items joined with 'and'."""
    items = [i for i in items if i]
    if not items: return ""
    if len(items) == 1: return items[0]
    if len(items) == 2: return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + ", and " + items[-1]


def fetch_unusual(verbose=False):
    log("running flow_scan.py --majors --json")
    payload = run_script("flow_scan.py", ["--majors", "--json"], timeout=180)
    if not payload or not payload.get("ok"):
        log("flow_scan.py failed; skipping")
        return None
    items = (payload.get("data") or {}).get("items") or []
    by_tk = {}
    for it in items:
        tk = it.get("tk")
        if not tk:
            continue
        by_tk.setdefault(tk, []).append(it)
    out = []
    for tk, rows in by_tk.items():
        rows.sort(key=lambda r: r.get("v_oi") or 0, reverse=True)
        top = rows[0]
        side = "calls" if top.get("side") == "C" else "puts"
        exp = top.get("exp") or ""
        try:
            exp_short = datetime.strptime(exp, "%Y-%m-%d").strftime("%b %-d")
        except Exception:
            exp_short = exp
        out.append({
            "ticker": tk,
            "vol_oi": int(top.get("v_oi") or 0),
            "context": f"{exp_short} {side} ${top.get('strike')} · {len(rows)} unusual strike(s)",
        })
    out.sort(key=lambda r: r["vol_oi"], reverse=True)
    return out[:6]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--skip-scanners", action="store_true",
                    help="only sparks + market_status; skip movers/wsb/flow")
    ap.add_argument("--reseed-prices", action="store_true",
                    help="force a 1y refetch for every ticker in prices.json")
    args = ap.parse_args()

    if not BRIEFS.exists():
        log(f"briefs.json not found at {BRIEFS}")
        return 1
    with open(BRIEFS) as f:
        data = json.load(f)
    briefs = data.get("briefs") or []
    if not briefs:
        log("no briefs to enrich")
        return 0
    brief = briefs[0]
    log(f"enriching brief {brief.get('date')}")

    # --- sparks ---
    brief_syms = collect_brief_symbols(brief)

    # also fetch sparks for movers / wsb names so the page has them everywhere
    extra_for_sparks = set()
    if not args.skip_scanners:
        log("pre-fetching universe for sparks (movers + wsb need them)")
        movers_payload = run_script("movers.py", ["--json"], timeout=60)
        if movers_payload and movers_payload.get("ok"):
            d = movers_payload.get("data") or {}
            for bucket in ("gainers", "losers", "most_actives"):
                for r in d.get(bucket, []) or []:
                    if r.get("sym"):
                        extra_for_sparks.add(r["sym"])
        wsb_payload = run_script("social_sentiment.py", ["--json", "--no-watchlist"], timeout=120)
        if wsb_payload and wsb_payload.get("ok"):
            for r in (wsb_payload.get("data") or {}).get("reddit", {}).get("wallstreetbets", [])[:5]:
                if r.get("ticker"):
                    extra_for_sparks.add(r["ticker"])

    history = fetch_history(brief_syms | extra_for_sparks, SPARK_DAYS, verbose=args.verbose)
    if not history:
        log("no history fetched, aborting")
        return 1

    sparks = build_sparks({s: history[s] for s in brief_syms if s in history})
    log(f"built sparks for {len(sparks)} keys")
    brief["sparks"] = sparks
    # The full-resolution 1y price history that powers the mobile chart now
    # lives in prices.json (owned by refresh_price_history.py), not in the
    # brief. Don't write history_1y here — strip any leftover so the brief
    # stays lean.
    brief.pop("history_1y", None)

    # --- movers ---
    # Preserve `detail` (and `note`) fields from the prior brief when overwriting
    # by ticker — the trader skill writes thesis copy that scanners don't produce.
    if not args.skip_scanners:
        prior_movers = (brief.get("stocks") or {}).get("movers") or {}

        def merge_keep_detail(new_rows, prior_rows):
            prior_by_tk = {r.get("ticker"): r for r in (prior_rows or [])}
            for r in new_rows:
                p = prior_by_tk.get(r.get("ticker"))
                if p:
                    if p.get("detail") and not r.get("detail"):
                        r["detail"] = p["detail"]
                    if p.get("note") and not r.get("note"):
                        r["note"] = p["note"]
            return new_rows

        movers = fetch_movers(history)
        if movers:
            movers["gainers"] = merge_keep_detail(movers["gainers"], prior_movers.get("gainers"))
            movers["losers"] = merge_keep_detail(movers["losers"], prior_movers.get("losers"))
            log(f"movers: {len(movers['gainers'])} gainers, {len(movers['losers'])} losers")
            brief.setdefault("stocks", {})
            brief["stocks"]["movers"] = movers

        wsb = fetch_wsb()
        if wsb:
            log(f"wsb_top: {len(wsb)} entries (was {len((brief.get('stocks') or {}).get('wsb_top') or [])})")
            brief["stocks"]["wsb_top"] = wsb

        prior_unusual = (brief.get("options") or {}).get("unusual") or []
        unusual = fetch_unusual()
        if unusual:
            unusual = merge_keep_detail(unusual, prior_unusual)
            log(f"options.unusual: {len(unusual)} tickers")
            brief.setdefault("options", {})
            brief["options"]["unusual"] = unusual

    # --- per-tab social sentiment ---
    if not args.skip_scanners:
        per_tab = build_per_tab_social()
        if per_tab:
            for tab in ("macro", "stocks", "options", "crypto"):
                items = per_tab.get(tab, [])
                if items:
                    brief.setdefault(tab, {})
                    brief[tab]["social"] = items
                    log(f"social.{tab}: {len(items)} tickers")

    # --- market_status ---
    status = market_status_now()
    log(f"market_status: {status['tier']} - {status['text']}")
    brief["market_status"] = status

    if args.dry:
        log("dry mode, not writing")
        return 0

    tmp = BRIEFS.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(BRIEFS)
    log(f"wrote {BRIEFS}")

    # --- per-ticker price archive (prices.json) ---
    # Folded in here so every enrich run grows the chart archive. The publish
    # path runs enrich after staging.json merges; the 30-min refresh_site_prices.py
    # cron runs `enrich_briefs.py --skip-scanners`. Either way, prices.json
    # picks up new trading days and seeds new tickers automatically.
    try:
        update_price_archive(brief, reseed=args.reseed_prices, verbose=args.verbose)
    except Exception as e:
        log(f"prices archive update failed (non-fatal): {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
