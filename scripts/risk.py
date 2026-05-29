#!/usr/bin/env python3
"""Pre-trade gate against CONSTITUTION v1.2.

Enforces:
  - 2% per-trade $-risk cap
  - 25% concentration cap (stocks/crypto)
  - 4 open-positions cap
  - R:R >= 2:1
  - Cooldown
  - 10% per-contract option premium cap
  - FOMO (entry vs MA20 + 2*ATR) — uses --underlying-ticker for inverse vehicles
  - Cumulative $-at-risk cap: 6% correlated / 8% uncorrelated (v1.1)
  - Earnings blackout in horizon unless defined-risk vehicle (v1.2)

Usage:
  risk.py --ticker XXX --entry 50 --stop 47 --target 56 --size 6
  risk.py --ticker XXX --entry 1.20 --stop 0.60 --target 2.40 --size 1 \
          --kind option --vehicle long_call --premium 1.20 --horizon-days 14
  risk.py --ticker SQQQ --entry 54 --stop 49 --target 65 --size 4 --side LONG \
          --underlying QQQ --correlation short_index
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta

from _common import load_portfolio

MAX_RISK_PCT = 2.0
MAX_CONCENTRATION_PCT = 25.0
MAX_OPEN_POSITIONS = 4
MIN_RR = 2.0
MAX_OPTION_PREMIUM_PCT = 10.0
CUM_RISK_CAP_CORRELATED = 6.0
CUM_RISK_CAP_UNCORRELATED = 8.0

# v2.2 FOMO three-tier rule
FOMO_DEMOTE_RISK_PCT = 1.0       # half of MAX_RISK_PCT
FOMO_DEMOTE_MIN_RR = 3.0         # vs MIN_RR baseline of 2.0
FOMO_TREATMENT_BY_CLASS = {
    "long_index":      "hard_block",
    "long_tech":       "hard_block",
    "long_growth":     "hard_block",
    "long_momentum":   "hard_block",
    "long_defensive":  "size_demote",
    "long_value":      "size_demote",
    "long_commodity":  "size_demote",
    "long_uncorrelated": "size_demote",
    "energy_long":     "size_demote",
    # short_* classes are not subject — FOMO rule is LONG-only.
    "unknown":         "size_demote",   # per user 2026-04-30: don't block, demote + disclaim
}


def _open_position_dollar_risk(pos: dict) -> float:
    """$-at-risk on an existing open position (entry to stop)."""
    is_option = pos.get("kind") == "option"
    qty = float(pos.get("qty", 0))
    entry = float(pos.get("entry", 0))
    stop = float(pos.get("stop", 0))
    if is_option:
        # Long options: max risk = premium * 100 * qty
        return qty * entry * 100
    if stop == 0 or entry == 0:
        return 0.0
    return qty * abs(entry - stop)


def _correlation_class(pos: dict) -> str:
    """Best-effort correlation class for an existing position.

    Reads explicit `correlation_class` field, else heuristics on ticker.
    """
    if pos.get("correlation_class"):
        return pos["correlation_class"]
    t = pos.get("ticker", "").upper()
    SHORT_INDEX = {"SQQQ", "SPXS", "SPXU", "SDOW", "SH", "PSQ"}
    LONG_INDEX = {"TQQQ", "SPXL", "UPRO", "QQQ", "SPY", "DIA", "IWM"}
    if t in SHORT_INDEX:
        return "short_index"
    if t in LONG_INDEX:
        return "long_index"
    return "unknown"


def _earnings_within_horizon(ticker: str, horizon_days: int) -> tuple[bool, str]:
    """Return (has_earnings, message). Soft fails — never blocks if data unreachable."""
    try:
        import yfinance as yf
        from _cache import cache_get, cache_put
        cache_key = f"earnings_{ticker.upper()}"
        cached = cache_get(cache_key, ttl_seconds=12 * 3600)
        if cached is not None:
            next_er = cached.get("next_er")
        else:
            tk = yf.Ticker(ticker)
            dates = tk.get_earnings_dates(limit=4)
            today = date.today()
            if dates is None or dates.empty:
                cache_put(cache_key, {"next_er": None})
                return False, f"no earnings data for {ticker}"
            future = dates[dates.index.date > today] if hasattr(dates.index, "date") else dates
            if future.empty:
                cache_put(cache_key, {"next_er": None})
                return False, f"no upcoming earnings for {ticker}"
            next_dt = future.index[0].date() if hasattr(future.index[0], "date") else None
            next_er = next_dt.isoformat() if next_dt else None
            cache_put(cache_key, {"next_er": next_er})

        if not next_er:
            return False, f"no upcoming earnings for {ticker}"
        next_dt = date.fromisoformat(next_er)
        days_out = (next_dt - date.today()).days
        if 0 <= days_out <= horizon_days:
            return True, f"{ticker} earnings on {next_er} ({days_out}d out, horizon {horizon_days}d)"
        return False, f"{ticker} earnings on {next_er} ({days_out}d out — outside horizon)"
    except Exception as e:
        return False, f"earnings check failed: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--kind", choices=["stock", "option", "crypto", "etf"], default="stock")
    ap.add_argument("--vehicle", choices=["stock", "etf", "crypto", "long_call",
                                          "long_put", "debit_spread", "calendar",
                                          "covered_call", "csp"],
                    help="Specific vehicle (used for earnings-defined-risk gate).")
    ap.add_argument("--side", choices=["LONG", "SHORT"], default="LONG")
    ap.add_argument("--entry", type=float, required=True)
    ap.add_argument("--stop", type=float, required=True)
    ap.add_argument("--target", type=float, required=True)
    ap.add_argument("--size", type=float, required=True,
                    help="shares (stock/crypto) or contracts (option)")
    ap.add_argument("--premium", type=float, default=None,
                    help="option premium per contract (required if --kind option)")
    ap.add_argument("--ma20", type=float, default=None, help="20-day MA (FOMO gate)")
    ap.add_argument("--atr", type=float, default=None, help="ATR14 (FOMO gate)")
    ap.add_argument("--underlying", default=None,
                    help="Underlying ticker for inverse ETFs / options "
                         "(FOMO + earnings checks run on underlying)")
    ap.add_argument("--correlation",
                    default="unknown",
                    help="Correlation class label (short_index, long_index, "
                         "energy_long, tech_long, ...). Compared against open positions.")
    ap.add_argument("--horizon-days", type=int, default=10,
                    help="Time-stop in trading days. Used for earnings-blackout gate.")
    ap.add_argument("--skip-earnings-check", action="store_true",
                    help="Skip earnings check (e.g. when offline).")
    ap.add_argument("--fomo-treatment",
                    choices=["hard_block", "size_demote", "allow"],
                    default=None,
                    help="v2.2 FOMO treatment override. If omitted, derived "
                         "from --correlation via FOMO_TREATMENT_BY_CLASS.")
    ap.add_argument("--mean-revert", action="store_true",
                    help="Trade is mean-revert (RSI<30 or documented support bounce). "
                         "Skips FOMO check entirely. Requires thesis to mention "
                         "'RSI' or 'support'.")
    ap.add_argument("--thesis", default="",
                    help="Trade thesis text. Used for --mean-revert keyword check.")
    args = ap.parse_args()

    p = load_portfolio()
    equity = float(p["equity"])
    positions = p.get("positions", [])
    open_positions = len(positions)
    cooldown = int(p.get("cooldown_days_remaining", 0))

    reasons_pass: list[str] = []
    reasons_fail: list[str] = []

    # --- Cooldown ---
    if cooldown > 0:
        reasons_fail.append(f"COOLDOWN active ({cooldown} days remaining). No new trades.")
    else:
        reasons_pass.append("Cooldown: not active.")

    # --- Open positions cap ---
    if open_positions >= MAX_OPEN_POSITIONS:
        reasons_fail.append(f"Open positions at cap ({open_positions}/{MAX_OPEN_POSITIONS}).")
    else:
        reasons_pass.append(f"Open positions: {open_positions}/{MAX_OPEN_POSITIONS}.")

    # --- Risk per trade ---
    per_unit_risk = abs(args.entry - args.stop)
    dollar_risk = 0.0
    if per_unit_risk == 0:
        reasons_fail.append("entry == stop; invalid.")
    else:
        if args.kind == "option":
            if args.premium is None:
                reasons_fail.append("option trade requires --premium")
            else:
                dollar_risk = args.size * args.premium * 100
        else:
            dollar_risk = args.size * per_unit_risk

        risk_pct = dollar_risk / equity * 100 if equity else float("inf")
        line = f"Risk: ${dollar_risk:,.2f} = {risk_pct:.2f}% of equity (cap {MAX_RISK_PCT}%)"
        (reasons_pass if risk_pct <= MAX_RISK_PCT else reasons_fail).append(line)

    # --- R:R ---
    per_unit_reward = abs(args.target - args.entry)
    if per_unit_risk > 0:
        rr = per_unit_reward / per_unit_risk
        line = f"R:R = {rr:.2f} : 1 (cap >= {MIN_RR}:1)"
        (reasons_pass if rr >= MIN_RR else reasons_fail).append(line)

    # --- Concentration ---
    if args.kind != "option":
        capital = args.size * args.entry
        conc = capital / equity * 100 if equity else float("inf")
        line = f"Concentration: ${capital:,.2f} = {conc:.1f}% (cap {MAX_CONCENTRATION_PCT}%)"
        (reasons_pass if conc <= MAX_CONCENTRATION_PCT else reasons_fail).append(line)
    elif args.premium is not None:
        per_contract_premium = args.premium * 100
        prem_pct = per_contract_premium / equity * 100 if equity else float("inf")
        line = (f"Option premium/contract: ${per_contract_premium:.2f} "
                f"= {prem_pct:.1f}% (cap {MAX_OPTION_PREMIUM_PCT}%)")
        (reasons_pass if prem_pct <= MAX_OPTION_PREMIUM_PCT else reasons_fail).append(line)

    # --- FOMO gate (v2.2 three-tier: hard_block / size_demote / allow) ---
    # Rule applies to LONG entries only. For inverse vehicles, the caller passes
    # the underlying's ma20/atr/entry — the test runs on the underlying.
    reasons_warn: list[str] = []
    if args.ma20 is not None and args.atr is not None and args.side == "LONG":
        ceiling = args.ma20 + 2 * args.atr
        ref_price = args.entry
        is_extended = ref_price > ceiling

        # Resolve treatment in priority: --mean-revert > --fomo-treatment > class map
        proposed_class_lower = (args.correlation or "unknown").lower()
        if args.mean_revert:
            thesis_lower = (args.thesis or "").lower()
            if "rsi" in thesis_lower or "support" in thesis_lower:
                treatment = "allow"
                treatment_source = "--mean-revert (thesis cites RSI/support)"
            else:
                # mean-revert claimed without justification — refuse the override
                treatment = FOMO_TREATMENT_BY_CLASS.get(proposed_class_lower, "size_demote")
                treatment_source = ("--mean-revert ignored: thesis must contain 'RSI' "
                                    "or 'support'; falling back to class default")
                reasons_warn.append(treatment_source)
        elif args.fomo_treatment:
            treatment = args.fomo_treatment
            treatment_source = f"--fomo-treatment {args.fomo_treatment}"
        else:
            treatment = FOMO_TREATMENT_BY_CLASS.get(proposed_class_lower, "size_demote")
            treatment_source = f"derived from correlation={proposed_class_lower}"

        line_prefix = (f"FOMO[{treatment}]: ref ${ref_price:.2f} vs ceiling "
                       f"${ceiling:.2f} (extended={is_extended}); src={treatment_source}")

        if not is_extended:
            reasons_pass.append(line_prefix + " — under ceiling, no FOMO concern")
        elif treatment == "hard_block":
            reasons_fail.append(line_prefix + " — HARD BLOCK")
        elif treatment == "allow":
            reasons_pass.append(line_prefix + " — ALLOWED (mean-revert)")
        elif treatment == "size_demote":
            # Allow only if dollar_risk <= FOMO_DEMOTE_RISK_PCT% AND R:R >= FOMO_DEMOTE_MIN_RR
            risk_pct = (dollar_risk / equity * 100) if equity else float("inf")
            rr_now = per_unit_reward / per_unit_risk if per_unit_risk > 0 else 0
            if risk_pct > FOMO_DEMOTE_RISK_PCT:
                reasons_fail.append(
                    line_prefix +
                    f" — SIZE_DEMOTE requires risk <= {FOMO_DEMOTE_RISK_PCT}% "
                    f"(currently {risk_pct:.2f}%). Reduce size or skip.")
            elif rr_now < FOMO_DEMOTE_MIN_RR:
                reasons_fail.append(
                    line_prefix +
                    f" — SIZE_DEMOTE requires R:R >= {FOMO_DEMOTE_MIN_RR}:1 "
                    f"(currently {rr_now:.2f}:1). Tighten stop or extend target.")
            else:
                reasons_pass.append(
                    line_prefix +
                    f" — ALLOWED at demoted risk={risk_pct:.2f}% R:R={rr_now:.2f}")

            # Disclaimer for unknown correlation
            if proposed_class_lower == "unknown" and args.fomo_treatment is None:
                reasons_warn.append(
                    f"FOMO unknown-correlation: treating as size_demote. "
                    f"Confirm correlation_class manually or analyse {args.ticker} "
                    f"5d/20d move vs SPY before entering. (v2.2 user-clarified default.)")

    # --- Cumulative $-at-risk cap (v1.1) ---
    proposed_class = (args.correlation or "unknown").lower()
    open_risk_total = 0.0
    correlated_risk = 0.0
    for pos in positions:
        r = _open_position_dollar_risk(pos)
        open_risk_total += r
        c = _correlation_class(pos).lower()
        if c == proposed_class and proposed_class != "unknown":
            correlated_risk += r

    cum_total = open_risk_total + dollar_risk
    cum_total_pct = cum_total / equity * 100 if equity else float("inf")
    cum_corr_pct = (correlated_risk + dollar_risk) / equity * 100 if equity else float("inf")

    # If proposed class matches at least one open position → use correlated cap
    same_class_count = sum(
        1 for pos in positions
        if _correlation_class(pos).lower() == proposed_class and proposed_class != "unknown"
    )
    cap = CUM_RISK_CAP_CORRELATED if same_class_count > 0 else CUM_RISK_CAP_UNCORRELATED
    cap_label = "correlated" if same_class_count > 0 else "uncorrelated"
    line = (f"Cumulative risk: open ${open_risk_total:,.2f} + proposed ${dollar_risk:,.2f} "
            f"= ${cum_total:,.2f} = {cum_total_pct:.2f}% (cap {cap}% {cap_label})")
    (reasons_pass if cum_total_pct <= cap else reasons_fail).append(line)

    if proposed_class != "unknown":
        sub_line = f"  same-class open risk: ${correlated_risk:,.2f}; with proposed: {cum_corr_pct:.2f}%"
        reasons_pass.append(sub_line) if cum_corr_pct <= CUM_RISK_CAP_CORRELATED else reasons_fail.append(sub_line)

    # --- Earnings blackout (v1.2) ---
    defined_risk_vehicles = {"debit_spread", "calendar"}
    is_defined_risk = (args.vehicle in defined_risk_vehicles)
    earnings_check_ticker = args.underlying.upper() if args.underlying else args.ticker.upper()

    # Index/tech-heavy vehicles are exposed to mega-cap earnings.
    # Other sector ETFs are not — skip mega-cap check on them.
    MEGA_CAP_SENSITIVE = {"SPY", "QQQ", "IWM", "DIA", "SQQQ", "SPXS", "TQQQ", "SPXL",
                          "XLK", "XLC", "XLY"}  # tech, comm-services, consumer-disc carry mega-caps
    OTHER_SECTOR_ETFS = {"XLE", "XLF", "XLV", "XLI", "XLU", "XLP", "XLB", "XLRE"}

    if args.skip_earnings_check:
        reasons_pass.append("Earnings check: skipped (--skip-earnings-check).")
    elif earnings_check_ticker in MEGA_CAP_SENSITIVE:
        # Index check: any mega-cap earnings inside horizon?
        MEGA_CAPS = ["NVDA", "AAPL", "MSFT", "META", "AMZN", "GOOGL", "TSLA", "AMD", "AVGO", "NFLX"]
        triggered = []
        for mc in MEGA_CAPS:
            has, _ = _earnings_within_horizon(mc, args.horizon_days)
            if has:
                triggered.append(mc)
        if triggered and not is_defined_risk:
            reasons_fail.append(
                f"EARNINGS BLACKOUT (index): mega-cap earnings within {args.horizon_days}d "
                f"({', '.join(triggered)}). Switch to defined-risk vehicle (debit_spread/calendar)."
            )
        elif triggered:
            reasons_pass.append(
                f"Earnings: mega-cap prints in horizon ({', '.join(triggered)}) — "
                f"defined-risk vehicle satisfies rule."
            )
        else:
            reasons_pass.append(f"Earnings: no mega-cap prints within {args.horizon_days}d.")
    elif earnings_check_ticker in OTHER_SECTOR_ETFS:
        reasons_pass.append(f"Earnings: {earnings_check_ticker} is a non-tech sector ETF — "
                            f"mega-cap earnings not directly relevant.")
    else:
        has, msg = _earnings_within_horizon(earnings_check_ticker, args.horizon_days)
        if has and not is_defined_risk:
            reasons_fail.append(
                f"EARNINGS BLACKOUT: {msg}. Switch to defined-risk vehicle (debit_spread/calendar)."
            )
        elif has:
            reasons_pass.append(f"Earnings: {msg} — defined-risk vehicle satisfies rule.")
        else:
            reasons_pass.append(f"Earnings: {msg}.")

    # --- Report ---
    ok = not reasons_fail
    print(f"=== Risk check: {args.ticker} {args.kind} {args.side} "
          f"vehicle={args.vehicle or '?'} class={proposed_class} ===")
    print(f"Equity: ${equity:,.2f}  |  Open positions: {open_positions}/{MAX_OPEN_POSITIONS}  |  Cooldown: {cooldown}d")
    print()
    if reasons_pass:
        print("PASS checks:")
        for r in reasons_pass:
            print(f"  + {r}")
    if reasons_warn:
        print("WARNINGS (do independent analysis):")
        for r in reasons_warn:
            print(f"  ! {r}")
    if reasons_fail:
        print("FAIL checks:")
        for r in reasons_fail:
            print(f"  - {r}")
    print()
    print(f"Verdict: {'APPROVED' if ok else 'REJECTED'}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
