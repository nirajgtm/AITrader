#!/usr/bin/env python3
"""Generate and broadcast a daily market watch message at 6 AM PT.

Usage:
    morning_broadcast.py             # generate, send to all active recipients
    morning_broadcast.py --dry-run   # generate, print to stdout, do not send
    morning_broadcast.py --save      # generate, save draft, do not send

Pipeline:
    1. Skip on weekends + US holidays.
    2. Run brief.py morning to get the digest JSON.
    3. Pick: 1-2 buy ideas, 1 options strategy, 0-1 crypto, 1-3 watch-outs.
    4. Format per fixed template.
    5. Save draft to state/broadcasts/YYYY-MM-DD.txt.
    6. If not --dry-run / --save, send via broadcast.py.
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
STATE = ROOT / "state"
BROADCASTS_DIR = STATE / "broadcasts"
BRIEF = SCRIPTS / "brief.py"
BROADCAST = SCRIPTS / "broadcast.py"
VENV_PY = SCRIPTS / ".venv" / "bin" / "python3"
PY = str(VENV_PY) if VENV_PY.exists() else "python3"

# Names treated as "popular retail holdings" — used as a fallback when the
# user's actual portfolio is empty. The user's real holdings (loaded from
# portfolio.json) take priority.
DEFAULT_RETAIL_HOLDINGS = {"AAPL", "TSLA", "NVDA", "MSFT", "GOOGL", "AMZN",
                           "META", "AMD", "INTC", "COIN", "MSTR", "PLTR",
                           "GME", "AMC", "SOFI"}


def load_user_holdings():
    """Read the user's open positions from portfolio.json.

    Returns a set of tickers. Falls back to DEFAULT_RETAIL_HOLDINGS if the
    portfolio is empty or unreadable.
    """
    try:
        portfolio = json.loads((STATE / "portfolio.json").read_text())
        held = {p["ticker"] for p in portfolio.get("positions", [])}
        if held:
            return held
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return DEFAULT_RETAIL_HOLDINGS


def is_market_day(now_pt):
    if now_pt.weekday() >= 5:
        return False
    # Hard-coded US market holidays for 2026. Update annually.
    holidays_2026 = {
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
        "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
        "2026-11-26", "2026-12-25",
    }
    return now_pt.strftime("%Y-%m-%d") not in holidays_2026


def run_brief():
    """Run brief.py morning, return parsed JSON dict or None on error.

    Cold runs can take 5-15 minutes due to scanner.py and pead scanner.
    Warm cache runs (within 4h) finish in under a minute.
    """
    try:
        result = subprocess.run(
            [PY, str(BRIEF), "morning"],
            capture_output=True, text=True, timeout=1800, check=True,
        )
        return json.loads(result.stdout.strip())
    except (subprocess.SubprocessError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        print(f"brief.py failed: {e}", file=sys.stderr)
        return None


def build_hypothesis(c):
    """Translate scanner sources into a one-line human hypothesis."""
    sources = c.get("sources", [])
    rsi = c.get("_rsi")
    ma20 = c.get("_ma20")
    spot = c.get("_spot")

    parts = []
    if "sector_leader_pullback" in sources:
        parts.append("sector RS leader pulled back to moving average")
    if "breakout" in sources:
        parts.append("breakout on rising volume")
    if "pead" in sources and "breakout" not in sources:
        parts.append("post-earnings drift continuation")
    if "mover_active" in sources and not parts:
        parts.append("high relative volume")
    if "unusual_flow" in sources:
        parts.append("unusual options flow")
    if rsi is not None and ma20 and spot and rsi < 45 and abs(spot - ma20) / ma20 < 0.02:
        parts.append(f"RSI {rsi:.0f} oversold near 20MA")

    return ", ".join(parts) if parts else "; ".join(c.get("details", [])) or "scanner signal"


def fetch_ticker_data(ticker):
    """Run price.py --json for one ticker, return data dict or None."""
    try:
        result = subprocess.run(
            [PY, str(SCRIPTS / "price.py"), ticker, "--json"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return json.loads(result.stdout.strip()).get("data", {})
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return None


def pick_buy_ideas(digest):
    """Pick up to 2 buy ideas from digest candidates.

    Filters:
    - Skip names in any earnings list (mega-cap or scanner pre-earnings).
    - Skip names where gap detail > 7% (chase).
    - Skip names where current price is above their FOMO ceiling.
    - Sort by signal density.

    Falls back to a sector ETF leader if no candidate passes.
    """
    candidates = digest.get("candidates", [])
    earnings_within = set(digest.get("earnings_within_7d", []))
    flags = set(digest.get("flags", []))

    # Build a broader earnings exclusion set from candidate sources.
    earnings_excl = set(earnings_within)
    for c in candidates:
        if "pre_earnings_runup" in c.get("sources", []):
            earnings_excl.add(c["ticker"])

    picks = []
    for c in sorted(candidates, key=lambda x: -len(x.get("sources", []))):
        if len(picks) >= 2:
            break
        ticker = c["ticker"]
        sources = c.get("sources", [])
        details = c.get("details", [])

        if ticker in earnings_excl:
            continue

        # Chase check: parse "gap=XX%" from details, skip if > 7%.
        chase = False
        for d in details:
            if "gap=" in d:
                try:
                    gap_str = d.split("gap=")[1].split("%")[0]
                    if abs(float(gap_str)) > 7.0:
                        chase = True
                        break
                except (ValueError, IndexError):
                    pass
        if chase:
            continue

        # Per-ticker FOMO check via price.py.
        td = fetch_ticker_data(ticker)
        if td:
            close = td.get("close", 0)
            fomo_ceiling = td.get("fomo_ceiling", 0)
            if close and fomo_ceiling and close > fomo_ceiling:
                continue
            c["_spot"] = close
            c["_rsi"] = td.get("rsi14")
            c["_ma20"] = td.get("ma20")
        picks.append(c)

    # Fallback: if no candidates passed, use the top sector ETF if it passes FOMO.
    if not picks:
        sectors_top = digest.get("sectors_top", [])
        if sectors_top:
            etf = sectors_top[0]["etf"]
            td = fetch_ticker_data(etf)
            if td and td.get("close", 0) <= td.get("fomo_ceiling", 0):
                picks.append({
                    "ticker": etf,
                    "sources": ["sector_leader_pullback"],
                    "details": [f"5d rotation +{sectors_top[0].get('rotation', 0):.1f}",
                                f"RSI {td.get('rsi14', 0):.0f}"],
                    "_spot": td.get("close"),
                    "_rsi": td.get("rsi14"),
                    "_ma20": td.get("ma20"),
                })

    return picks


def pick_options_strategy(digest):
    """Pick one options idea: long call, secured put, or LEAP.

    Logic:
    - If VIX low + a watchlist name triggered: long call near-ATM 30-45 DTE.
    - If a quality leader is at support and IV elevated: cash-secured put at -5% strike.
    - If a sector leader is on multi-month uptrend: LEAP call 12+ months.
    Default to a long call on the top buy idea if no specific match.
    """
    regime = digest.get("regime_summary", {})
    vix_bucket = regime.get("vix_bucket", "")
    watchlist = digest.get("watchlist", [])
    sectors_top = digest.get("sectors_top", [])
    flags = set(digest.get("flags", []))

    triggered = [w for w in watchlist if w.get("status") == "TRIGGERED"]

    if triggered:
        w = triggered[0]
        return {
            "kind": "long_call",
            "ticker": w["ticker"],
            "rationale": f"watchlist trigger hit at ${w.get('entry_trigger')}",
            "dte_target": "30-45 DTE",
            "strike_logic": "ATM or +1 strike OTM",
        }

    if vix_bucket == "low" and sectors_top:
        leader = sectors_top[0]
        return {
            "kind": "leap_call",
            "ticker": leader["etf"],
            "rationale": f"sector RS leader (rotation +{leader.get('rotation', 0):.1f})",
            "dte_target": "Jan 2027 or further",
            "strike_logic": "ATM",
        }

    # Default: cash-secured put on a quality name if we have a buy candidate.
    candidates = digest.get("candidates", [])
    earnings_within = set(digest.get("earnings_within_7d", []))
    quality = [c for c in candidates if c["ticker"] not in earnings_within
               and "pead" in c.get("sources", [])]
    if quality:
        c = quality[0]
        return {
            "kind": "cash_secured_put",
            "ticker": c["ticker"],
            "rationale": "name you would own anyway, collecting premium",
            "dte_target": "30-45 DTE",
            "strike_logic": "5 to 8 percent below current price",
        }

    return None


def pick_crypto(digest):
    """Pick a crypto idea or None.

    Look at digest crypto step output for trending or oversold signals.
    """
    steps = {s["name"]: s for s in digest.get("steps_log", [])}
    crypto_step = steps.get("crypto", {})
    headline = crypto_step.get("headline", "")
    if "trending(RH)" not in headline:
        return None

    # Parse trending coins from headline. Crude but works.
    if "trending(RH)=" in headline:
        trending_part = headline.split("trending(RH)=")[1].split(";")[0]
        coins = [c.strip() for c in trending_part.split(",")]
        if "BTC" in coins:
            # BTC is the conservative pick.
            return {
                "ticker": "BTC",
                "rationale": "trending on RH, broad market sentiment proxy",
            }
        if coins:
            return {
                "ticker": coins[0],
                "rationale": "trending on RH, momentum signal",
            }
    return None


def pick_watchouts(digest):
    """Build the watch-out list. Includes:
    - Open position actions on user holdings (highest priority).
    - Bearish signals on user's actual holdings (or default retail list if empty).
    - Macro events within the next session.
    - Earnings blackouts approaching.
    """
    watchouts = []
    holdings = load_user_holdings()

    # Open position actions (from position_review on the personal portfolio).
    for pos in digest.get("open_positions_review", []):
        action = pos.get("primary_action", "")
        if action and action not in ("HOLD", "OK"):
            watchouts.append(
                f"{pos['ticker']} position: {action.replace('_', ' ').lower()}"
            )

    # Bearish signals on names the user holds.
    flags = digest.get("flags", [])
    for f in flags:
        if f.startswith("rsi_extreme_") or f.startswith("breakdown_") or f.startswith("failed_breakout_"):
            ticker = f.split("_")[-1].upper()
            if ticker in holdings:
                watchouts.append(f"{ticker} showing bearish signal, consider trimming or exiting")

    # Per-ticker FOMO check on user holdings: warn if any holding is now in chase territory
    # AND has been bid up further intraday.
    for ticker in holdings:
        td = fetch_ticker_data(ticker)
        if not td:
            continue
        close = td.get("close", 0)
        ceiling = td.get("fomo_ceiling", 0)
        rsi = td.get("rsi14", 0)
        if close and ceiling and close > ceiling and rsi > 80:
            # Avoid duplicate if already flagged.
            already = any(ticker in w for w in watchouts)
            if not already:
                watchouts.append(f"{ticker} extended (RSI {rsi:.0f}, above FOMO ceiling), consider taking profits")

    # Macro within 2 days.
    macro = digest.get("macro_upcoming_14d", [])
    next_2d = [m for m in macro if m.get("days_out", 99) <= 2 and "Fed" in m.get("type", "")]
    if next_2d:
        m = next_2d[0]
        watchouts.append(f"{m['type']} in {m.get('days_out', 0)} day(s)")

    # Earnings cluster within 3 days.
    earnings = digest.get("earnings_within_7d", [])
    if len(earnings) >= 3:
        watchouts.append(f"earnings cluster: {', '.join(earnings[:5])} reporting within 7 days")

    return watchouts[:5]


def regime_oneliner(digest):
    """Compact 1-2 line market read."""
    regime = digest.get("regime_summary", {})
    spy = regime.get("spy_regime", "?")
    vix_b = regime.get("vix_bucket", "?")
    sectors_top = digest.get("sectors_top", [])
    sectors_bot = digest.get("sectors_bottom", [])
    top = sectors_top[0]["etf"] if sectors_top else "?"
    bot = sectors_bot[-1]["etf"] if sectors_bot else "?"

    flags = set(digest.get("flags", []))
    rsi_extreme = any(f.startswith("rsi_extreme_") for f in flags)
    fomo = "fomo_above_2atr_spy" in flags

    line1 = f"Market: {spy.lower()} regime, VIX {vix_b}. Leader {top}, laggard {bot}."
    extras = []
    if rsi_extreme:
        extras.append("RSI extended on indices")
    if fomo:
        extras.append("FOMO gate active")
    line2 = " ".join(extras) + "." if extras else ""

    return line1 + ("\n" + line2 if line2 else "")


def format_message(digest, today_str):
    buy_ideas = pick_buy_ideas(digest)
    option = pick_options_strategy(digest)
    crypto = pick_crypto(digest)
    watchouts = pick_watchouts(digest)

    lines = [f"[MARKET WATCH] {today_str}", regime_oneliner(digest), ""]

    if buy_ideas:
        lines.append("Trade suggestions:")
        for c in buy_ideas:
            ticker = c["ticker"]
            spot = c.get("_spot")
            entry_str = f"${spot:.2f}" if spot else "current price"
            sources = c.get("sources", [])
            hypothesis = build_hypothesis(c)
            lines.append(f"Buy {ticker} at or below {entry_str}")
            lines.append(f"Hypothesis: {hypothesis}")
            lines.append("")

    if option:
        lines.append(f"Options strategy: {option['kind'].replace('_', ' ').upper()} on {option['ticker']}")
        lines.append(f"Setup: {option['rationale']}")
        lines.append(f"Target: {option['dte_target']}, {option['strike_logic']}")
        kind = option["kind"]
        ticker = option["ticker"]
        if kind == "long_call":
            steps = (f"Robinhood: {ticker} > Trade > Trade Options > pick {option['dte_target']} > "
                     "Call > strike near ATM > Buy to Open > Limit at mid > Day")
        elif kind == "leap_call":
            steps = (f"Robinhood: {ticker} > Trade > Trade Options > pick Jan 2027+ > "
                     "Call > strike ATM > Buy to Open > Limit at mid > GTC")
        elif kind == "cash_secured_put":
            steps = (f"Robinhood: {ticker} > Trade > Trade Options > pick {option['dte_target']} > "
                     "Put > strike 5-8% below spot > Sell to Open > Limit at mid > Day. "
                     "Need cash equal to strike x 100 reserved.")
        else:
            steps = ""
        lines.append(steps)
        lines.append("")

    if crypto:
        lines.append("Crypto:")
        lines.append(f"Buy ${crypto['ticker']} at or below current price")
        lines.append(f"Hypothesis: {crypto['rationale']}")
        lines.append("")

    if watchouts:
        lines.append("Watch out for:")
        for w in watchouts:
            lines.append(f"- {w}")

    return "\n".join(lines).rstrip() + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="generate and print, do not send")
    parser.add_argument("--save", action="store_true",
                        help="generate and save draft, do not send")
    args = parser.parse_args()

    BROADCASTS_DIR.mkdir(parents=True, exist_ok=True)

    pt = ZoneInfo("America/Los_Angeles")
    now_pt = datetime.now(pt)
    today_str = now_pt.strftime("%Y-%m-%d")

    if not is_market_day(now_pt):
        print(f"{today_str} is not a US market day. Skipping.", file=sys.stderr)
        sys.exit(0)

    digest = run_brief()
    if not digest:
        print("brief.py failed or returned no data. Skipping send.", file=sys.stderr)
        sys.exit(1)

    message = format_message(digest, today_str)
    draft_path = BROADCASTS_DIR / f"{today_str}.txt"
    draft_path.write_text(message)

    if args.dry_run:
        print(message)
        print(f"\n(saved to {draft_path}, not sent)")
        return

    if args.save:
        print(f"Draft saved to {draft_path}. Not sent.")
        return

    # Live send.
    result = subprocess.run(
        [PY, str(BROADCAST), message],
        capture_output=True, text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
