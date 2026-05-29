# Crypto rules (autonomous trader)

Crypto is enforced in CODE (`crypto_strategy.py` + `guards.py` crypto checks +
`order_client.execute_crypto_buy`). This doc is the readable reference; the code is
what binds. public.com trades crypto 24/7 (verified live 2026-05-22): bare-symbol
symbology (`BTC`, not `BTC-USD`), `instrument_type=CRYPTO`, MARKET orders, TIF DAY
(GTC unsupported), ~0.6% commission per side.

## What's different from equities
- **24/7.** The run evaluates crypto whenever it fires, including off equity hours
  (the gate is `guards.is_crypto_tradeable`, always open while `config.crypto.enabled`).
- **Software stop only.** Crypto carries no resting broker stop. The stop is checked
  each run (every ~15 min). Between runs a coin can gap through it. This is the core
  risk and the reason size is small.
- **Smaller size.** `config.crypto.max_position_usd` (~$150) vs the $250 equity cap,
  because crypto runs 2-4x the volatility and the stop is weaker.
- **Fewer slots.** `config.crypto.max_open_positions` (2) concurrent crypto positions.

## Entry (need >= 50 daily bars; both setups re-validate on a fresh quote before buy)
- **MOMENTUM:** last > MA20 > MA50 (uptrend) AND `min_rsi_entry` <= RSI <= `max_rsi_entry`
  (default 40-72: in trend, not overbought).
- **MEAN_REVERT:** RSI < 35 (oversold) AND last >= MA50 * 0.90 (a dip, not a freefall
  below the long average), AND the dip is **stabilizing** -- price bouncing (last >
  prior daily close) OR RSI turning up (RSI now > RSI as of the prior bar). Oversold
  alone is never a buy.

## Hard blockers (veto regardless of setup)
- Insufficient data (< ~50 bars or missing RSI/MA/ATR).
- RSI >= 80, or RSI > `max_rsi_entry` (no chasing).
- ATR > 12%/day (too wild for a stop checked only each run).
- Downtrend (last < MA50) with no mean-revert signal.
- **Mean-revert, no stabilization** (not bouncing and RSI not turning up).
- **Suspect signal:** RSI craters while price is ~flat (< 0.5%) -- the UTC daily-bar
  boundary artifact; treated as bad data, not a dip.
- **Complex-wide deleveraging:** when most of the universe (>= 60%, min 3 names) flips
  oversold/mean-revert at once, it's one broad risk-off flush (a falling knife), not N
  independent dips -- the whole oversold set is vetoed. Momentum names are unaffected.
- Banned/disabled, kill-switch tripped (then manage/sell only), or crypto cap reached.

## Math
- **Stop** = entry - `stop_atr_mult` * ATR (default 1.5 ATR), floored so risk <=
  `max_stop_loss_pct` (15%). If a coin needs a wider stop than 15% it's too volatile
  to size here, so it gets capped (and is usually blocked by the ATR rule first).
- **Target** = entry + 2R where R = entry - stop. R:R 2:1. A 2R win clears the ~1.2%
  crypto round-trip commission with room to spare (e.g. a 1.5-ATR stop on a 3%-ATR
  coin is ~4.5% risk, so the +9% target dwarfs fees).
- **Size** = `config.crypto.max_position_usd`, bounded by settled cash AND by
  `config.crypto.max_portfolio_pct` of equity (a TOTAL crypto-exposure cap: held + new
  must stay within that % of the book; the surfaced candidate size is pre-capped to the
  remaining budget, and an exhausted budget blocks new crypto buys). Scales with equity,
  binds alongside the per-position and count caps. Enforced in `guards.crypto_portfolio_ok`
  / `guards.crypto_budget_remaining` and `run_autonomous` (the keep-crypto-small directive).

## Management (each run, 24/7)
- last <= stop  -> SELL (software stop fired; hard exit).
- last >= target -> SELL (target reached).
- else HOLD, with a thesis re-check (broken thesis -> exit even before the stop).

## Discipline
- A surfaced `crypto_candidate` is a reason to ANALYZE, never an automatic buy. Pull
  news/macro (why is the coin / the whole complex moving?) before any entry, exactly
  like equities. A broad risk-off crypto dump is a falling knife, not a dip to buy.
- Every order is preflighted and only places when ARMED.
- Tunables live in `config.json -> crypto`. Changing them is a risk-state change and
  should follow the same evolution/approval discipline as equity risk params.
