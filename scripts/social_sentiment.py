#!/usr/bin/env python3
"""Social / retail sentiment scanner.

Sources (validated 2026-05-02 / extended 2026-05-03):
  - apewisdom.io  — Reddit ticker mentions across WSB / all-stocks / crypto.
                    Returns mention count, 24h-ago mention count, rank, rank-24h-ago.
                    No bull/bear polarity in the API.
  - reddit.com    — direct JSON for niche subs apewisdom doesn't track:
                      r/TheRaceTo10Million, r/TheRaceTo1Million, r/unusual_whales,
                      r/Vitards, r/thetagang, r/Daytrading, r/options.
                    We tally cap-letter ticker mentions across hot post titles+bodies,
                    filter noise words, and validate against the tradable universe
                    cache when present.
  - StockTwits    — public endpoints (no auth):
                      /api/2/trending/symbols.json
                      /api/2/streams/symbol/<TKR>.json (per-message Bullish/Bearish tags)
  - Twitter / X   — not pulled. Nitter is dead, free API is dead. Documented gap.

What we surface:
  - Top mention leaders per Reddit cohort + 24h delta
  - "Social breakouts": names rising into top-10 from outside top-30, OR with
    mentions_24h_ago null/0 (brand-new attention)
  - "WSB squeeze candidates": top-10 in WSB AND mention delta > +100%
  - StockTwits trending: tickers + AI-generated narrative summary
  - Per-watchlist bull/bear ratio from StockTwits message stream (tagged-only)
  - "Bull/bear flips" on watchlist names: ratio crossed 0.5 vs prior snapshot

Cache: 30 min for each network call. Prior watchlist sentiment snapshot kept
25h to detect flips across the trading day.

Usage:
  social_sentiment.py            # human-readable dashboard
  social_sentiment.py --json     # one-line step_result for brief.py
  social_sentiment.py --no-watchlist  # skip per-ticker StockTwits (faster)
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from _cache import cache_get, cache_put, cached
from _terse import emit, step_result
import watchlist_store

ROOT = Path(__file__).resolve().parent.parent

CACHE_TTL = 1800        # 30 min for fresh API pulls
PREV_TTL = 90_000       # ~25h — used only to compare today's bull/bear vs yesterday's
USER_AGENT = "trader-research/0.1 (research only)"
HTTP_TIMEOUT = 10

APEWISDOM_SUBS = ["wallstreetbets", "stocks", "all-stocks", "cryptocurrency"]

# Subreddits we scrape directly via Reddit JSON (apewisdom doesn't track these)
REDDIT_DIRECT_SUBS = {
    "race10m":   "TheRaceTo10Million",
    "race1m":    "TheRaceTo1Million",
    "vitards":   "Vitards",
    "thetagang": "thetagang",
    "daytrading": "Daytrading",
    "options":   "options",
    "uw":        "unusual_whales",
}

# Strings that ARE valid tradable tickers but in retail/options chatter
# almost always mean something else. Filtered from EVERY social source
# (both Reddit-direct and apewisdom output) so they never surface as a
# "trending name". Add cautiously; a real ticker getting filtered is worse
# than a noise word slipping through.
SOCIAL_TICKER_BLACKLIST = {
    "DTE",   # "Days To Expire" in options chatter, not DTE Energy
}

# Common all-caps strings that look like tickers but aren't. Combined with a
# universe-membership check below, this catches both pure noise and abbrev
# words that happen to be valid symbols (e.g. "AI" is a ticker but in
# "AI产业" it's discussion not a position).
TICKER_NOISE = {
    "THE", "AND", "FOR", "YOU", "WHY", "WTF", "SOS", "OMG", "OP", "TIL", "EOY",
    "ATH", "EPS", "CEO", "CFO", "IPO", "SEC", "FED", "CPI", "GDP", "USD", "EUR",
    "API", "URL", "UI", "UX", "NYC", "LA", "PR", "FYI", "LOL", "IMO", "TIA",
    "DD", "YOLO", "FOMO", "HODL", "MOON", "PUMP", "DUMP", "BUY", "SELL", "HOLD",
    "OUT", "IN", "UP", "DOWN", "PT", "GG", "WL", "GTC", "OTM", "ITM", "ATM",
    "RSI", "VIX", "DXY", "ETF", "ETN", "MA", "SMA", "EMA", "PE", "PEG", "ROE",
    "ROI", "ROA", "EBITDA", "FCF", "EPS", "DCF", "TAM", "SAM", "SOM", "NPV",
    "IRR", "PMI", "ISM", "JOLTS", "FOMC", "PPI", "CES", "BLS", "BEA", "USA",
    "US", "UK", "EU", "AI", "ML", "DL", "AR", "VR", "SaaS", "B2B", "B2C",
    "EOD", "AH", "PM", "AM", "RH", "MM", "IB", "ER", "IV", "OI", "PCR",
    "TFSA", "RRSP", "ESPP", "LLC", "LP", "PLC", "INC", "CORP", "CO",
    "NEW", "OLD", "BIG", "SMALL", "ALL", "ANY", "EACH", "BOTH",
    "MAGA", "MAGS", "WSB",
    # Government / agency acronyms (frequently appear in news posts on r/unusual_whales,
    # r/options, etc. and get falsely parsed as tickers). Verified NOT active US tickers.
    # Skip ICE intentionally (Intercontinental Exchange is a real ticker).
    "USDA", "CDC", "FBI", "IRS", "NIH", "DOJ", "NSA", "FTC", "FCC",
    "EPA", "DEA", "ATF", "USPS", "NTSB", "NHTSA", "OSHA", "DHS", "DOD",
    # News outlets / media (commonly appear in headlines parsed as posts)
    "WSJ", "NYT", "NYP", "CNN", "BBC", "MSNBC", "FOX", "WAPO", "AP",
    "REUTERS", "BLOOMBERG", "CNBC", "YF",
    # International orgs / common geopolitical acronyms
    "WHO", "NATO", "OPEC", "UN", "IMF", "WTO", "G7", "G20", "EU",
    # Common parsing artifacts from compound words / typos seen in the wild
    "RTUNE", "AKING", "KETV",
}


def _load_universe_set() -> set[str]:
    """Tickers we treat as 'real' when extracting from free text."""
    p = ROOT / "scripts" / ".." / "state" / "cache" / "universe_full.json"
    p = p.resolve()
    if not p.exists():
        return set()
    try:
        payload = json.loads(p.read_text())
        data = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(data, list):
            return {str(t).upper() for t in data}
        if isinstance(data, dict):
            # support {"tickers": [...]} or {"AAPL": {...}, ...}
            if "tickers" in data and isinstance(data["tickers"], list):
                return {str(t).upper() for t in data["tickers"]}
            return {str(t).upper() for t in data.keys()}
    except (json.JSONDecodeError, OSError):
        return set()
    return set()


_UNIVERSE_CACHE: set[str] = set()
def universe_set() -> set[str]:
    global _UNIVERSE_CACHE
    if not _UNIVERSE_CACHE:
        _UNIVERSE_CACHE = _load_universe_set()
    return _UNIVERSE_CACHE


def _http_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return None


def watchlist_tickers() -> list[str]:
    return watchlist_store.active_tickers()


@cached("social_apewisdom_wsb", ttl_seconds=CACHE_TTL)
def fetch_apewisdom_wsb() -> dict | None:
    return _http_json("https://apewisdom.io/api/v1.0/filter/wallstreetbets/page/1")


@cached("social_apewisdom_stocks", ttl_seconds=CACHE_TTL)
def fetch_apewisdom_stocks() -> dict | None:
    return _http_json("https://apewisdom.io/api/v1.0/filter/stocks/page/1")


@cached("social_apewisdom_all", ttl_seconds=CACHE_TTL)
def fetch_apewisdom_all() -> dict | None:
    return _http_json("https://apewisdom.io/api/v1.0/filter/all-stocks/page/1")


@cached("social_apewisdom_crypto", ttl_seconds=CACHE_TTL)
def fetch_apewisdom_crypto() -> dict | None:
    return _http_json("https://apewisdom.io/api/v1.0/filter/cryptocurrency/page/1")


# --- Reddit JSON direct (for subs apewisdom doesn't track) ------------------

def _fetch_reddit_sub_uncached(sub: str, limit: int = 50) -> dict | None:
    """Hot posts from r/<sub>. Returns the parsed JSON dict from Reddit."""
    url = f"https://www.reddit.com/r/{sub}/hot.json?limit={limit}"
    return _http_json(url)


@cached("social_reddit_race10m", ttl_seconds=CACHE_TTL)
def fetch_reddit_race10m() -> dict | None:
    return _fetch_reddit_sub_uncached("TheRaceTo10Million", limit=50)


@cached("social_reddit_race1m", ttl_seconds=CACHE_TTL)
def fetch_reddit_race1m() -> dict | None:
    return _fetch_reddit_sub_uncached("TheRaceTo1Million", limit=50)


@cached("social_reddit_vitards", ttl_seconds=CACHE_TTL)
def fetch_reddit_vitards() -> dict | None:
    return _fetch_reddit_sub_uncached("Vitards", limit=50)


@cached("social_reddit_thetagang", ttl_seconds=CACHE_TTL)
def fetch_reddit_thetagang() -> dict | None:
    return _fetch_reddit_sub_uncached("thetagang", limit=50)


@cached("social_reddit_daytrading", ttl_seconds=CACHE_TTL)
def fetch_reddit_daytrading() -> dict | None:
    return _fetch_reddit_sub_uncached("Daytrading", limit=50)


@cached("social_reddit_options", ttl_seconds=CACHE_TTL)
def fetch_reddit_options() -> dict | None:
    return _fetch_reddit_sub_uncached("options", limit=50)


@cached("social_reddit_uw", ttl_seconds=CACHE_TTL)
def fetch_reddit_uw() -> dict | None:
    # r/unusual_whales — options flow chatter + news/links from members.
    # Pull hot+new to catch breaking news items in addition to trending mentions.
    return _fetch_reddit_sub_uncached("unusual_whales", limit=50)


def _extract_tickers_from_text(text: str, valid_universe: set[str]) -> list[str]:
    """Extract cap-letter tickers from a chunk of free text. Filters noise.
    If valid_universe is non-empty, also requires membership."""
    if not text:
        return []
    raw = re.findall(r"\$?([A-Z]{1,5})\b", text)
    out = []
    for t in raw:
        if t in TICKER_NOISE or t in SOCIAL_TICKER_BLACKLIST:
            continue
        if len(t) == 1:
            continue  # too noisy without context
        if valid_universe and t not in valid_universe:
            continue
        out.append(t)
    return out


def _condense_reddit_direct(payload: dict | None, top_n: int = 15) -> list[dict]:
    """Walk the hot-posts payload, count ticker mentions weighted by score+1,
    return top tickers with mention count, post-count, and top relevant posts."""
    if not payload:
        return []
    posts = (payload.get("data") or {}).get("children") or []
    if not posts:
        return []
    universe = universe_set()
    counts: dict[str, dict] = {}
    # Per-ticker, keep the top 3 posts (by score) that mention the ticker
    per_ticker_posts: dict[str, list[dict]] = {}
    for p in posts:
        pd = p.get("data") or {}
        title = pd.get("title") or ""
        body = pd.get("selftext") or ""
        score = max(int(pd.get("score") or 0), 0)
        flair = pd.get("link_flair_text") or ""
        sub = pd.get("subreddit") or ""
        tickers = set(_extract_tickers_from_text(f"{title} {body}", universe))
        for t in tickers:
            row = counts.setdefault(t, {"ticker": t, "mentions": 0, "posts": 0, "score_sum": 0})
            row["mentions"] += 1
            row["posts"] += 1
            row["score_sum"] += score
            per_ticker_posts.setdefault(t, []).append({
                "sub": sub, "title": title.strip(),
                "score": score, "flair": flair,
                "body_excerpt": (body.strip()[:240] + "...") if len(body.strip()) > 240 else body.strip(),
            })
    # Sort each ticker's posts by score desc, keep top 3
    for t, lst in per_ticker_posts.items():
        lst.sort(key=lambda p: p["score"], reverse=True)
        per_ticker_posts[t] = lst[:3]
    # Rank tickers by mentions then upvote weight
    rows = sorted(counts.values(),
                  key=lambda r: (r["mentions"], r["score_sum"]),
                  reverse=True)[:top_n]
    out = []
    for i, r in enumerate(rows, 1):
        tk = r["ticker"]
        out.append({
            "rank": i,
            "ticker": tk,
            "name": "",
            "mentions": r["mentions"],
            "rank_24h_ago": None,
            "delta_pct": None,
            "posts": r["posts"],
            "score_sum": r["score_sum"],
            "top_posts": per_ticker_posts.get(tk, []),
        })
    return out


# Crude bullish/bearish keyword classifier for post titles. Used to derive a
# directional read when StockTwits AI summary is absent.
_BULL_WORDS = {
    "buy", "calls", "call", "long", "bullish", "moon", "rally", "breakout",
    "all-time high", "ath", "bid", "yolo", "lfg", "uptrend", "squeeze",
    "support", "reclaim", "beat", "beats", "upside", "accumulate", "gain",
    "rip", "ripping", "to the moon", "bull",
}
_BEAR_WORDS = {
    "sell", "puts", "put", "short", "bearish", "crash", "dump", "rejection",
    "guidance cut", "miss", "misses", "downgrade", "downgraded", "downside",
    "rolling over", "breakdown", "topping", "rug", "tank", "tanking",
    "bear", "warning", "weak", "fade",
}


def _post_sentiment_keywords(title: str) -> str:
    """BULLISH | BEARISH | NEUTRAL based on title keyword scan."""
    t = (title or "").lower()
    bull = sum(1 for w in _BULL_WORDS if w in t)
    bear = sum(1 for w in _BEAR_WORDS if w in t)
    if bull > bear and bull >= 1: return "BULLISH"
    if bear > bull and bear >= 1: return "BEARISH"
    return "NEUTRAL"


def derive_discussion(top_posts: list[dict]) -> dict:
    """From a ticker's top posts, derive a sentiment vote and a quote of the
    top post. Returns {'sentiment': BULL/BEAR/MIXED/NEUTRAL, 'top_quote': str, 'subs': [...]}."""
    if not top_posts:
        return {"sentiment": "NEUTRAL", "top_quote": "", "subs": []}
    bull = bear = neutral = 0
    for p in top_posts:
        s = _post_sentiment_keywords(p.get("title", ""))
        if s == "BULLISH": bull += 1
        elif s == "BEARISH": bear += 1
        else: neutral += 1
    if bull > 0 and bear == 0: sentiment = "BULLISH"
    elif bear > 0 and bull == 0: sentiment = "BEARISH"
    elif bull > 0 and bear > 0: sentiment = "MIXED"
    else: sentiment = "NEUTRAL"
    subs = sorted({p.get("sub", "") for p in top_posts if p.get("sub")})
    top = max(top_posts, key=lambda p: p.get("score", 0))
    return {
        "sentiment": sentiment,
        "top_quote": top.get("title", ""),
        "top_score": int(top.get("score", 0)),
        "top_sub": top.get("sub", ""),
        "subs": subs,
    }


@cached("social_stocktwits_trending", ttl_seconds=CACHE_TTL)
def fetch_stocktwits_trending() -> dict | None:
    return _http_json("https://api.stocktwits.com/api/2/trending/symbols.json")


def fetch_stocktwits_stream(ticker: str) -> dict | None:
    key = f"social_stocktwits_stream_{ticker.upper()}"
    cached_v = cache_get(key, ttl_seconds=CACHE_TTL)
    if cached_v is not None:
        return cached_v
    data = _http_json(f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json")
    if data is not None:
        cache_put(key, data)
    return data


def _condense_apewisdom(payload: dict | None, top_n: int = 15) -> list[dict]:
    if not payload:
        return []
    rows = []
    for r in (payload.get("results") or []):
        tk = r.get("ticker")
        if tk in SOCIAL_TICKER_BLACKLIST:
            continue
        m = r.get("mentions") or 0
        m_prev = r.get("mentions_24h_ago")
        if m_prev in (None, 0):
            delta_pct = None
        else:
            delta_pct = round((m - m_prev) / m_prev * 100, 1)
        rows.append({
            "rank": r.get("rank"),
            "ticker": tk,
            "name": html.unescape(r.get("name") or ""),
            "mentions": m,
            "rank_24h_ago": r.get("rank_24h_ago"),
            "delta_pct": delta_pct,
        })
        if len(rows) >= top_n:
            break
    # re-rank locally since we may have dropped entries
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def _detect_breakouts(rows: list[dict]) -> list[dict]:
    """A 'social breakout' = currently in top 10 AND (was outside top 30 yesterday
    OR has no mentions_24h_ago at all). New attention, not chronic loud names.
    """
    out = []
    for r in rows:
        if (r.get("rank") or 99) > 10:
            continue
        prev = r.get("rank_24h_ago")
        if prev in (None, 0) or prev > 30:
            out.append({
                "ticker": r["ticker"],
                "name": r.get("name"),
                "mentions": r["mentions"],
                "rank": r["rank"],
                "rank_24h_ago": prev,
                "delta_pct": r.get("delta_pct"),
            })
    return out


def _detect_squeeze_candidates(rows: list[dict]) -> list[dict]:
    """In WSB top 10 AND mentions delta > +100%. Captures parabolic mention surges
    on names that were already on the radar — classic squeeze precursor.
    """
    out = []
    for r in rows:
        if (r.get("rank") or 99) > 10:
            continue
        d = r.get("delta_pct")
        if d is not None and d >= 100:
            out.append({
                "ticker": r["ticker"],
                "mentions": r["mentions"],
                "delta_pct": d,
            })
    return out


def _summarize_stream(stream: dict | None) -> dict:
    if not stream or not stream.get("messages"):
        return {"bull": 0, "bear": 0, "untagged": 0, "total": 0, "ratio": None}
    bull = bear = untagged = 0
    for m in stream["messages"]:
        sent = (m.get("entities") or {}).get("sentiment")
        if not sent:
            untagged += 1
            continue
        b = sent.get("basic")
        if b == "Bullish":
            bull += 1
        elif b == "Bearish":
            bear += 1
        else:
            untagged += 1
    total = bull + bear + untagged
    tagged = bull + bear
    ratio = round(bull / tagged, 3) if tagged else None
    return {"bull": bull, "bear": bear, "untagged": untagged, "total": total, "ratio": ratio}


def _condense_st_trending(payload: dict | None, top_n: int = 10) -> list[dict]:
    if not payload:
        return []
    rows = []
    for s in (payload.get("symbols") or [])[:top_n]:
        trends = s.get("trends") or {}
        summary = (trends.get("summary") or "").strip()
        if len(summary) > 160:
            summary = summary[:157] + "..."
        rows.append({
            "rank": s.get("rank"),
            "symbol": s.get("symbol"),
            "instrument_class": s.get("instrument_class"),
            "trending_score": round(s.get("trending_score") or 0, 2),
            "summary": summary,
        })
    return rows


def build_snapshot(include_watchlist: bool = True) -> dict:
    snap: dict = {"reddit": {}, "stocktwits": {}, "errors": []}

    fetchers = {
        "wallstreetbets": fetch_apewisdom_wsb,
        "stocks": fetch_apewisdom_stocks,
        "all_stocks": fetch_apewisdom_all,
        "cryptocurrency": fetch_apewisdom_crypto,
    }
    for label, fn in fetchers.items():
        rows = _condense_apewisdom(fn())
        if not rows:
            snap["errors"].append(f"apewisdom_{label}: no data")
        snap["reddit"][label] = rows

    # Reddit-direct subs (apewisdom doesn't track these)
    direct_fetchers = {
        "race10m":    fetch_reddit_race10m,
        "race1m":     fetch_reddit_race1m,
        "vitards":    fetch_reddit_vitards,
        "thetagang":  fetch_reddit_thetagang,
        "daytrading": fetch_reddit_daytrading,
        "options":    fetch_reddit_options,
        "uw":         fetch_reddit_uw,
    }
    for label, fn in direct_fetchers.items():
        rows = _condense_reddit_direct(fn())
        if not rows:
            snap["errors"].append(f"reddit_direct_{label}: no data")
        snap["reddit"][label] = rows

    snap["stocktwits"]["trending"] = _condense_st_trending(fetch_stocktwits_trending())
    if not snap["stocktwits"]["trending"]:
        snap["errors"].append("stocktwits_trending: no data")

    snap["stocktwits"]["watchlist"] = {}
    if include_watchlist:
        for tk in watchlist_tickers():
            stream = fetch_stocktwits_stream(tk)
            snap["stocktwits"]["watchlist"][tk] = _summarize_stream(stream)

    snap["x_twitter"] = {
        "status": "unavailable",
        "note": "Free Twitter API and Nitter instances are dead as of 2026-05-02; no auto-pull. "
                "Use ad-hoc WebSearch site:x.com queries when a specific name needs FinTwit color.",
    }

    seen_breakouts: dict[str, dict] = {}
    for label in ("wallstreetbets", "all_stocks"):
        for b in _detect_breakouts(snap["reddit"].get(label) or []):
            tk = b["ticker"]
            if tk in seen_breakouts:
                seen_breakouts[tk]["subs"].append(label)
            else:
                seen_breakouts[tk] = {**b, "subs": [label]}
    # Race subs: any top-5 ticker is a breakout candidate (no historical compare yet)
    for label in ("race10m", "race1m", "vitards", "thetagang", "daytrading", "options", "uw"):
        rows = snap["reddit"].get(label) or []
        for r in rows[:5]:
            tk = r["ticker"]
            if tk in seen_breakouts:
                if label not in seen_breakouts[tk]["subs"]:
                    seen_breakouts[tk]["subs"].append(label)
            else:
                seen_breakouts[tk] = {
                    "ticker": tk,
                    "rank": r["rank"],
                    "rank_24h_ago": None,
                    "mentions": r["mentions"],
                    "subs": [label],
                }
    snap["breakouts"] = list(seen_breakouts.values())
    snap["squeeze_candidates"] = _detect_squeeze_candidates(
        snap["reddit"].get("wallstreetbets") or []
    )

    prev = cache_get("social_watchlist_prev", ttl_seconds=PREV_TTL) or {}
    flips = []
    new_prev = {}
    for tk, summ in snap["stocktwits"]["watchlist"].items():
        cur = summ.get("ratio")
        new_prev[tk] = cur
        old = prev.get(tk)
        if cur is not None:
            if old is not None:
                if (old < 0.5 <= cur) or (old >= 0.5 > cur):
                    flips.append({
                        "ticker": tk,
                        "prev_ratio": old,
                        "ratio": cur,
                        "direction": "bull" if cur >= 0.5 else "bear",
                    })
    cache_put("social_watchlist_prev", new_prev)
    snap["bull_bear_flips"] = flips

    return snap


def _build_flags(snap: dict) -> list[str]:
    flags: list[str] = []
    for b in snap.get("breakouts", []):
        flags.append(f"social_breakout:{b['ticker']}")
    for s in snap.get("squeeze_candidates", []):
        flags.append(f"wsb_squeeze_candidate:{s['ticker']}")
    for f in snap.get("bull_bear_flips", []):
        flags.append(f"bull_bear_flip:{f['ticker']}_{f['direction']}")
    return flags


def _build_headline(snap: dict) -> str:
    wsb = snap["reddit"].get("wallstreetbets") or []
    top = ", ".join(f"{r['ticker']}({r['mentions']})" for r in wsb[:5])
    parts = []
    if top:
        parts.append(f"WSB top5: {top}")
    if snap.get("breakouts"):
        bos = ",".join(b["ticker"] for b in snap["breakouts"][:3])
        parts.append(f"breakouts: {bos}")
    if snap.get("squeeze_candidates"):
        sq = ",".join(s["ticker"] for s in snap["squeeze_candidates"][:3])
        parts.append(f"squeeze: {sq}")
    if snap.get("bull_bear_flips"):
        fl = ",".join(f"{f['ticker']}->{f['direction']}" for f in snap["bull_bear_flips"][:3])
        parts.append(f"flips: {fl}")
    return "; ".join(parts) if parts else "no notable social signals"


def cmd_dashboard(snap: dict) -> None:
    print("=== Social Sentiment ===\n")
    for label in ("wallstreetbets", "stocks", "all_stocks", "cryptocurrency",
                  "race10m", "race1m", "vitards", "thetagang", "daytrading", "options"):
        rows = snap["reddit"].get(label) or []
        if not rows:
            continue
        print(f"-- Reddit / {label} (top 10) --")
        for r in rows[:10]:
            d = r.get("delta_pct")
            d_s = f"{d:+.0f}%" if d is not None else "new"
            prev = r.get("rank_24h_ago")
            prev_s = f"#{prev}" if prev else "—"
            print(f"  #{r['rank']:>2}  {r['ticker']:<10}  m={r['mentions']:<5}  "
                  f"24h={d_s:<7}  prev={prev_s:<5}  {r.get('name','')}")
        print()

    if snap.get("breakouts"):
        print("-- Social breakouts (new top-10) --")
        for b in snap["breakouts"]:
            subs = ",".join(b.get("subs") or [])
            print(f"  {b['ticker']:<8}  rank #{b['rank']} (was #{b.get('rank_24h_ago') or 'new'})"
                  f"  mentions={b['mentions']}  subs={subs}")
        print()

    if snap.get("squeeze_candidates"):
        print("-- WSB squeeze candidates (top10 + delta>=100%) --")
        for s in snap["squeeze_candidates"]:
            print(f"  {s['ticker']:<8}  mentions={s['mentions']}  delta={s['delta_pct']:+.0f}%")
        print()

    st_trend = snap["stocktwits"].get("trending") or []
    if st_trend:
        print("-- StockTwits trending (top 10) --")
        for s in st_trend[:10]:
            klass = s.get("instrument_class") or ""
            summ = (s.get("summary") or "")[:90]
            print(f"  #{s['rank']:>2}  {s['symbol']:<10}  ({klass})  score={s['trending_score']}  {summ}")
        print()

    wl = snap["stocktwits"].get("watchlist") or {}
    if wl:
        print("-- StockTwits per-watchlist (recent stream sample) --")
        for tk, summ in wl.items():
            r = summ.get("ratio")
            r_s = f"{r:.2f}" if r is not None else "n/a"
            print(f"  {tk:<8}  bull={summ['bull']:<3}  bear={summ['bear']:<3}  "
                  f"untagged={summ['untagged']:<3}  bull_ratio={r_s}")
        print()

    if snap.get("bull_bear_flips"):
        print("-- Bull/bear flips (vs prior snapshot) --")
        for f in snap["bull_bear_flips"]:
            print(f"  {f['ticker']}  {f['prev_ratio']:.2f} -> {f['ratio']:.2f}  ({f['direction']})")
        print()

    print(f"X/Twitter: {snap['x_twitter']['status']}  ({snap['x_twitter']['note']})")
    if snap.get("errors"):
        print(f"\nErrors: {snap['errors']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-watchlist", action="store_true",
                    help="skip per-ticker StockTwits stream pulls")
    args = ap.parse_args()

    snap = build_snapshot(include_watchlist=not args.no_watchlist)
    flags = _build_flags(snap)
    headline = _build_headline(snap)

    if args.json:
        result = step_result("social_sentiment", ok=True, headline=headline,
                             data=snap, flags=flags, errors=snap.get("errors") or [])
        emit(result)
    else:
        cmd_dashboard(snap)
        if flags:
            print(f"\nFlags: {flags}")
        print(f"\nHeadline: {headline}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
