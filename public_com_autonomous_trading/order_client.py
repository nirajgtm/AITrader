#!/usr/bin/env python3
"""Order execution for the autonomous trader -- THE ONLY mutating code in the system.

Places / cancels REAL orders on the live public.com cash account (account_id from config.json). There
is no sandbox. Every placement is:
  - blocked unless the system is ARMED (config.enabled = true). Disarmed = preflight
    + log only, never places. place_single() itself refuses when disarmed.
  - validated by public.com `preflight` first (non-state-changing dry-run).
  - assigned a client orderId (UUID) for idempotency.
  - appended to the trade log.

Auth/HTTP/throttle plumbing is reused from the read-only client (publicdotcom_api),
but the mutating endpoint calls live here, isolated in this directory.

Endpoints:
  POST   {GW}/trading/{acct}/preflight/single-leg   -> validate (no placement)
  POST   {GW}/trading/{acct}/order                   -> place
  GET    {GW}/trading/{acct}/order/{orderId}          -> status
  DELETE {GW}/trading/{acct}/order/{orderId}          -> cancel
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

DIR = Path(__file__).resolve().parent
ROOT = DIR.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(DIR))

import publicdotcom_api as pub   # noqa: E402  (auth/HTTP/throttle plumbing, read-only client)
import guards                    # noqa: E402
import positions                 # noqa: E402  (hypotheses store)

STATE_DIR = DIR / "state"
TRADE_LOG = STATE_DIR / "trade_log.jsonl"


class NotArmedError(RuntimeError):
    """Raised if a placement is attempted while the system is disarmed."""


def log_event(event: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    event = {"ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"), **event}
    with open(TRADE_LOG, "a") as f:
        f.write(json.dumps(event) + "\n")


def log_decision(symbol: str, action: str, *, for_case: str = "", against_case: str = "",
                 decision: str = "", why: str = "") -> None:
    """Record a trade DECISION with BOTH sides + the objective call, so the activity
    popup shows WHY an action (or non-action) was taken, not just the order. Call this
    for every BUY / SELL / SKIP / HOLD-with-reason. `action` is the verb; `decision`
    is the concrete outcome (e.g. 'BUY 1sh @218, stop 196'); `why` is the one-line
    objective synthesis. for_case / against_case are the simplified both-sides view."""
    plan = f"{action} {symbol.upper()}" + (f": {why}" if why else "")
    log_event({"action": "decision", "symbol": symbol.upper(), "verb": action,
               "plan": plan, "detail": {"FOR": for_case, "AGAINST": against_case,
                                        "DECISION": decision or action, "WHY": why}})


def _order_body(symbol, side, order_type, quantity, *, limit_price, stop_price,
                open_close, tif, session, order_id=None, instrument_type="EQUITY"):
    body = {
        "instrument": {"symbol": symbol.upper(), "type": instrument_type},
        "orderSide": side,
        "orderType": order_type,
        "expiration": {"timeInForce": tif},
        "quantity": str(quantity),
        "openCloseIndicator": open_close,
    }
    # equityMarketSession is an equity-only concept; CRYPTO orders must omit it.
    if instrument_type == "EQUITY":
        body["equityMarketSession"] = session
    if order_id is not None:
        body["orderId"] = order_id
    if limit_price is not None:
        body["limitPrice"] = str(limit_price)
    if stop_price is not None:
        body["stopPrice"] = str(stop_price)
    return body


def preflight_single(symbol, side, order_type, quantity, *, limit_price=None,
                     stop_price=None, open_close="OPEN", tif="DAY", session="CORE",
                     instrument_type="EQUITY") -> dict:
    """Validate a single-leg order WITHOUT placing it. Safe to call anytime."""
    body = _order_body(symbol, side, order_type, quantity, limit_price=limit_price,
                       stop_price=stop_price, open_close=open_close, tif=tif,
                       session=session, instrument_type=instrument_type)
    url = f"{pub.GATEWAY_BASE}/trading/{pub._account_id()}/preflight/single-leg"
    return pub._http("POST", url, headers=pub._auth_headers(), body=body)


def place_single(symbol, side, order_type, quantity, *, limit_price=None,
                 stop_price=None, open_close="OPEN", tif="DAY", session="CORE",
                 order_id=None, instrument_type="EQUITY") -> tuple[str, dict]:
    """PLACE a real order. Refuses unless ARMED. Returns (order_id, response)."""
    if not guards.is_armed():
        raise NotArmedError("system is DISARMED (config.enabled=false); refusing to place")
    order_id = order_id or str(uuid.uuid4())
    body = _order_body(symbol, side, order_type, quantity, limit_price=limit_price,
                       stop_price=stop_price, open_close=open_close, tif=tif,
                       session=session, order_id=order_id, instrument_type=instrument_type)
    url = f"{pub.GATEWAY_BASE}/trading/{pub._account_id()}/order"
    resp = pub._http("POST", url, headers=pub._auth_headers(), body=body)
    status = (resp.get("status") if isinstance(resp, dict) else "") or "SUBMITTED"
    summary = (f"{side} {quantity} {symbol.upper()} {order_type}"
               + (f" @{limit_price}" if limit_price is not None else "")
               + (f" stop {stop_price}" if stop_price is not None else "")
               + f" -- {status}")
    log_event({"action": "order", "symbol": symbol.upper(), "status": status,
               "plan": summary, "order_id": order_id, "request": body, "response": resp})
    return order_id, resp


def get_order(order_id: str) -> dict:
    url = f"{pub.GATEWAY_BASE}/trading/{pub._account_id()}/order/{order_id}"
    return pub._http("GET", url, headers=pub._auth_headers())


def cancel_order(order_id: str) -> dict:
    if not guards.is_armed():
        raise NotArmedError("system is DISARMED; refusing to cancel")
    url = f"{pub.GATEWAY_BASE}/trading/{pub._account_id()}/order/{order_id}"
    resp = pub._http("DELETE", url, headers=pub._auth_headers())
    log_event({"action": "cancel", "order_id": order_id, "response": resp})
    return resp


def decide_mechanics(price: float, dollar_size: float, cfg: dict | None = None):
    """Hybrid sizing. ('whole', shares) when price <= max_position and at least one
    whole share fits the dollar size (-> can carry a resting broker stop). Else
    ('fractional', dollars) -> market-only, software-enforced stop."""
    cfg = cfg or guards.load_config()
    max_pos = float(cfg["risk"]["max_position_usd"])
    if price <= max_pos:
        shares = int(dollar_size // price)
        if shares >= 1:
            return "whole", shares
    return "fractional", round(float(dollar_size), 2)


def execute_buy(symbol: str, dollar_size: float, ref_price: float, stop_price: float,
                target_price: float, hypothesis: dict, cfg: dict | None = None) -> dict:
    """Guard-gated hybrid buy. Whole-share -> LIMIT buy + resting STOP_LIMIT.
    Fractional -> MARKET buy (software stop). Always preflights; places only when
    armed; records settlement + per-ticker history. Returns a result dict."""
    import settlement
    import history
    cfg = cfg or guards.load_config()
    armed = guards.is_armed(cfg)

    ok, why = guards.stop_ok(ref_price, stop_price, "LONG", cfg)
    if not ok:
        return {"symbol": symbol, "intent": "BUY", "placed": False, "blocked": why}
    ok, why = guards.position_size_ok(dollar_size, cfg)
    if not ok:
        return {"symbol": symbol, "intent": "BUY", "placed": False, "blocked": why}

    mech, amt = decide_mechanics(ref_price, dollar_size, cfg)
    res = {"symbol": symbol, "intent": "BUY", "mechanics": mech, "armed": armed,
           "ref_price": ref_price, "stop": stop_price, "target": target_price}

    if mech == "whole":
        shares = amt
        limit = round(ref_price * (1 + cfg["execution"]["limit_slippage_pct"] / 100), 2)
        res["plan"] = f"BUY {shares}sh LIMIT {limit} (~${shares*ref_price:.0f}); resting STOP_LIMIT {stop_price}"
        res["preflight"] = preflight_single(symbol, "BUY", "LIMIT", shares, limit_price=limit)
        if not armed:
            log_event({"action": "would_buy", "symbol": symbol, "plan": res["plan"]})
            return {**res, "placed": False, "reason": "disarmed (dry-run)"}
        oid, resp = place_single(symbol, "BUY", "LIMIT", shares, limit_price=limit)
        settlement.record_buy(shares * ref_price)
        history.record(symbol, "BUY", shares, ref_price, order_id=oid,
                       note="whole-share entry", hypothesis=hypothesis)
        stop_lim = round(stop_price * (1 - cfg["risk"]["stop_limit_offset_pct"] / 100), 2)
        soid, sresp = place_single(symbol, "SELL", "STOP_LIMIT", shares,
                                   stop_price=stop_price, limit_price=stop_lim, open_close="CLOSE")
        positions.save_hypothesis(symbol, entry=ref_price, stop=stop_price, target=target_price,
                                  mechanics="whole", stop_kind="resting_broker", qty=shares,
                                  stop_order_id=soid, thesis=hypothesis)
        return {**res, "placed": True, "order_id": oid, "shares": shares,
                "stop_order_id": soid, "stop_kind": "resting_broker"}
    else:
        frac_qty = round(amt / ref_price, 4)
        res["plan"] = f"BUY ${amt:.2f} MARKET (~{frac_qty} sh); SOFTWARE stop {stop_price} (checked each run)"
        res["preflight"] = preflight_single(symbol, "BUY", "MARKET", frac_qty)
        if not armed:
            log_event({"action": "would_buy", "symbol": symbol, "plan": res["plan"]})
            return {**res, "placed": False, "reason": "disarmed (dry-run)"}
        oid, resp = place_single(symbol, "BUY", "MARKET", frac_qty)
        settlement.record_buy(amt)
        history.record(symbol, "BUY", frac_qty, ref_price, order_id=oid,
                       note="fractional entry", hypothesis=hypothesis)
        positions.save_hypothesis(symbol, entry=ref_price, stop=stop_price, target=target_price,
                                  mechanics="fractional", stop_kind="software", qty=frac_qty,
                                  stop_order_id=None, thesis=hypothesis)
        return {**res, "placed": True, "order_id": oid, "qty": frac_qty,
                "stop_kind": "software"}


def execute_crypto_buy(symbol: str, dollar_size: float, ref_price: float, stop_price: float,
                       target_price: float, hypothesis: dict, cfg: dict | None = None) -> dict:
    """Guard-gated CRYPTO buy. Crypto trades 24/7, is bought fractionally via a
    notional MARKET order (TIF DAY; GTC unsupported), and carries a SOFTWARE stop
    only -- there is no resting broker stop for crypto, so the stop is enforced by
    each run's position review. Gaps between runs are a known, accepted risk; size
    is kept small (crypto.max_position_usd) to bound it. Preflights; places only when
    armed; persists the hypothesis with stop_kind=software."""
    import settlement
    import history
    cfg = cfg or guards.load_config()
    armed = guards.is_armed(cfg)

    ok, why = guards.stop_ok(ref_price, stop_price, "LONG", cfg)
    if not ok:
        return {"symbol": symbol, "intent": "BUY", "asset": "crypto", "placed": False, "blocked": why}
    ok, why = guards.crypto_position_size_ok(dollar_size, cfg)
    if not ok:
        return {"symbol": symbol, "intent": "BUY", "asset": "crypto", "placed": False, "blocked": why}

    qty = round(float(dollar_size) / float(ref_price), 6)
    res = {"symbol": symbol, "intent": "BUY", "asset": "crypto", "mechanics": "crypto",
           "armed": armed, "ref_price": ref_price, "stop": stop_price, "target": target_price,
           "plan": f"BUY ${dollar_size:.2f} {symbol.upper()} MARKET (~{qty}); SOFTWARE stop {stop_price} (checked each run, 24/7)"}
    res["preflight"] = preflight_single(symbol, "BUY", "MARKET", qty, tif="DAY",
                                        instrument_type="CRYPTO")
    if not armed:
        log_event({"action": "would_buy", "symbol": symbol, "plan": res["plan"]})
        return {**res, "placed": False, "reason": "disarmed (dry-run)"}
    oid, resp = place_single(symbol, "BUY", "MARKET", qty, tif="DAY", instrument_type="CRYPTO")
    settlement.record_buy(dollar_size)
    history.record(symbol, "BUY", qty, ref_price, order_id=oid, note="crypto entry", hypothesis=hypothesis)
    positions.save_hypothesis(symbol, entry=ref_price, stop=stop_price, target=target_price,
                              mechanics="crypto", stop_kind="software", qty=qty,
                              stop_order_id=None, thesis=hypothesis)
    return {**res, "placed": True, "order_id": oid, "qty": qty, "stop_kind": "software"}


def execute_sell(symbol: str, qty: float, ref_price: float, *, mechanics: str = "fractional",
                 stop_order_id: str | None = None, reason: str = "", realized_r=None,
                 instrument_type: str = "EQUITY", cfg: dict | None = None) -> dict:
    """Guard-gated exit. Cancels any resting stop first, then MARKET-sells the
    position (fractional/crypto must be market; whole-share market exit ensures the
    fill). Records settled proceeds (T+1) + history. Places only when armed."""
    import settlement
    import history
    cfg = cfg or guards.load_config()
    armed = guards.is_armed(cfg)
    res = {"symbol": symbol, "intent": "SELL", "qty": qty, "reason": reason, "armed": armed,
           "asset": "crypto" if instrument_type == "CRYPTO" else "equity",
           "plan": f"SELL {qty} {symbol.upper()} MARKET ({reason})"}
    res["preflight"] = preflight_single(symbol, "SELL", "MARKET", qty, open_close="CLOSE",
                                        instrument_type=instrument_type)
    if not armed:
        log_event({"action": "would_sell", "symbol": symbol, "plan": res["plan"]})
        return {**res, "placed": False, "reason_detail": "disarmed (dry-run)"}
    if stop_order_id:
        try:
            cancel_order(stop_order_id)
        except Exception as e:
            log_event({"action": "stop_cancel_failed", "symbol": symbol, "error": str(e)})
    oid, resp = place_single(symbol, "SELL", "MARKET", qty, open_close="CLOSE",
                             instrument_type=instrument_type)
    settlement.record_sell(qty * ref_price)
    history.record(symbol, "SELL", qty, ref_price, order_id=oid, realized_r=realized_r, note=reason)
    positions.remove(symbol)
    return {**res, "placed": True, "order_id": oid}


def _preflight_test() -> int:
    """Probe order mechanics live (preflight only, nothing placed):
       1) fractional qty + LIMIT buy, 2) STOP_LIMIT sell (fractional), 3) notional sense."""
    import publicdotcom_api as p
    sym = "AAPL"
    q = p.get_quote(sym) or {}
    last = float(q.get("last") or 0) or 300.0
    frac_qty = round(50.0 / last, 4)            # ~$50 position -> fractional shares
    buy_limit = round(last * 1.003, 2)
    stop_px = round(last * 0.92, 2)
    stop_limit = round(last * 0.915, 2)
    print(f"{sym} last={last} frac_qty={frac_qty} buy_limit={buy_limit} stop={stop_px}/{stop_limit}\n")

    print("[1] fractional qty + LIMIT buy:")
    try:
        print("   ", json.dumps(preflight_single(sym, "BUY", "LIMIT", frac_qty,
              limit_price=buy_limit))[:400])
    except Exception as e:
        print("    ERROR:", e)

    print("[2] fractional qty + STOP_LIMIT sell (the resting stop):")
    try:
        print("   ", json.dumps(preflight_single(sym, "SELL", "STOP_LIMIT", frac_qty,
              stop_price=stop_px, limit_price=stop_limit, open_close="CLOSE"))[:400])
    except Exception as e:
        print("    ERROR:", e)

    print("[3] fractional qty + MARKET buy (fallback shape):")
    try:
        print("   ", json.dumps(preflight_single(sym, "BUY", "MARKET", frac_qty))[:400])
    except Exception as e:
        print("    ERROR:", e)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Autonomous order execution (mutating).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight-test", help="Probe order mechanics live (preflight only).")
    args = ap.parse_args()
    if args.cmd == "preflight-test":
        return _preflight_test()
    return 1


if __name__ == "__main__":
    sys.exit(main())
