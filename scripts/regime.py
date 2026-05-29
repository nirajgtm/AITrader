#!/usr/bin/env python3
"""Market regime one-pager.

Pulls SPY, QQQ, IWM positioning vs. 20/50/200 MAs, plus VIX, 10Y yield (^TNX),
DXY, oil, gold, BTC.

Modes:
  regime.py          # human dashboard
  regime.py --json   # compressed JSON (for runbook ingest)
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from _market import fetch_bulk, with_indicators, ret_pct, rsi as rsi_series, atr as atr_series, vs_ma
from _terse import alert, emit, step_result

TICKERS = {
    "SPY": "SPY", "QQQ": "QQQ", "IWM": "IWM",
    "VIX": "^VIX", "10Y": "^TNX", "DXY": "DX-Y.NYB",
    "OIL": "CL=F", "GOLD": "GC=F", "BTC": "BTC-USD",
}


def _last_or_nan(series: pd.Series) -> float:
    return float(series.iloc[-1]) if not series.empty else float("nan")


def _build_data() -> dict:
    """One bulk fetch for all 9 tickers."""
    bulk = fetch_bulk(list(TICKERS.values()), period="1y")
    out: dict = {"tickers": {}}
    for label, sym in TICKERS.items():
        df = bulk.get(sym)
        if df is None or df.empty:
            out["tickers"][label] = None
            continue
        df = with_indicators(df)
        last = df.iloc[-1]
        out["tickers"][label] = {
            "close": round(float(last["Close"]), 2),
            "chg5d_pct": round(ret_pct(df, 5), 2),
            "vs_ma20": vs_ma(last["Close"], last["MA20"]),
            "vs_ma50": vs_ma(last["Close"], last["MA50"]),
            "vs_ma200": vs_ma(last["Close"], last["MA200"]),
        }
        if label in ("SPY", "QQQ", "IWM"):
            out["tickers"][label]["rsi14"] = round(_last_or_nan(df["RSI14"]), 1)
            out["tickers"][label]["atr14"] = round(_last_or_nan(df["ATR14"]), 2)

    # VIX bucket
    vix = (out["tickers"].get("VIX") or {}).get("close")
    if vix is not None:
        if vix < 15:
            out["vix_bucket"] = "complacency"
        elif vix < 20:
            out["vix_bucket"] = "normal"
        elif vix < 25:
            out["vix_bucket"] = "elevated"
        elif vix < 30:
            out["vix_bucket"] = "stress"
        else:
            out["vix_bucket"] = "panic"

    # SPY regime
    spy = out["tickers"].get("SPY") or {}
    spy_close = spy.get("close")
    if spy_close is not None and spy.get("vs_ma200") and spy.get("vs_ma50") and spy.get("vs_ma20"):
        if (spy["vs_ma200"] == "above" and spy["vs_ma50"] == "above"
                and spy["vs_ma20"] == "above" and spy["chg5d_pct"] > 0):
            out["spy_regime"] = "BULL"
        elif (spy["vs_ma200"] == "below" and spy["vs_ma50"] == "below"
                and spy["chg5d_pct"] < 0):
            out["spy_regime"] = "BEAR"
        else:
            out["spy_regime"] = "CHOP_TRANSITION"

    return out


def _flags_and_actions(data: dict) -> tuple[list[str], list[dict]]:
    flags: list[str] = []
    actions: list[dict] = []
    spy = data["tickers"].get("SPY") or {}
    qqq = data["tickers"].get("QQQ") or {}
    iwm = data["tickers"].get("IWM") or {}
    vix = data["tickers"].get("VIX") or {}

    if spy.get("rsi14") and spy["rsi14"] >= 75:
        flags.append("rsi_extreme_spy")
    if qqq.get("rsi14") and qqq["rsi14"] >= 80:
        flags.append("rsi_extreme_qqq")
    if iwm.get("rsi14") and iwm["rsi14"] >= 75:
        flags.append("rsi_extreme_iwm")

    # FOMO: SPY close > 20MA + 2*ATR (uses bulk-fetched data)
    spy_close = spy.get("close")
    spy_atr = spy.get("atr14")
    if spy_close and spy_atr:
        bulk = fetch_bulk(["SPY"], period="1y")
        spy_df = bulk.get("SPY")
        if spy_df is not None and not spy_df.empty:
            spy_df = with_indicators(spy_df)
            ma20 = float(spy_df["MA20"].iloc[-1])
            ceiling = ma20 + 2 * spy_atr
            if spy_close > ceiling:
                flags.append("fomo_above_2atr_spy")

    if vix.get("close") and vix.get("chg5d_pct") and vix["chg5d_pct"] > 5:
        flags.append("vix_5d_rising")

    if data.get("spy_regime") == "BULL" and "fomo_above_2atr_spy" in flags:
        actions.append(alert("BULL but extended above 20MA+2ATR; entries banned by FOMO rule"))

    return flags, actions


def cmd_dashboard(data: dict) -> None:
    print("=== Regime snapshot ===\n")
    print(f"{'':<5} {'Close':>10} {'5d%':>8}  {'vs MAs':<25} {'extras':<25}")
    for label in TICKERS:
        d = data["tickers"].get(label)
        if d is None:
            print(f"{label:<5} n/a")
            continue
        ma_str = f">{'/'.join(['20MA' if d['vs_ma20']=='above' else '<20MA','50MA' if d['vs_ma50']=='above' else '<50MA','200MA' if d['vs_ma200']=='above' else '<200MA'])}"
        ma_str = f"{d['vs_ma20']}20 {d['vs_ma50']}50 {d['vs_ma200']}200"
        extras = ""
        if "rsi14" in d:
            extras = f"RSI={d['rsi14']:.1f} ATR={d['atr14']:.2f}"
        print(f"{label:<5} {d['close']:>10,.2f} {d['chg5d_pct']:>7,.2f}%  {ma_str:<25} {extras}")

    print()
    if "vix_bucket" in data:
        print(f"VIX regime: {data['vix_bucket']}")
    if "spy_regime" in data:
        print(f"SPY regime: {data['spy_regime']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="emit compressed JSON")
    args = ap.parse_args()

    data = _build_data()
    flags, actions = _flags_and_actions(data)

    spy_reg = data.get("spy_regime", "?")
    vix_b = data.get("vix_bucket", "?")
    qqq_rsi = (data["tickers"].get("QQQ") or {}).get("rsi14")
    headline = f"{spy_reg}; VIX {vix_b}"
    if qqq_rsi:
        headline += f"; QQQ RSI {qqq_rsi}"
    if flags:
        headline += f"; flags={','.join(flags)}"

    result = step_result("regime", ok=True, headline=headline,
                         data=data, flags=flags, actions=actions)

    if args.json:
        emit(result)
    else:
        cmd_dashboard(data)
        if flags:
            print(f"\nFlags: {flags}")
        if actions:
            for a in actions:
                print(f"[{a.get('kind')}] {a.get('msg','')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
