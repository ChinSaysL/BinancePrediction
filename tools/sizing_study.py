"""Sizing study: dollar band vs percentage of bankroll.

The signal harness (tools/walk_forward.py) cannot answer sizing questions: the
replay emits PnL per $1 staked, so the stake cancels out and every rule looks
identical. Sizing only shows up once PnL is COMPOUNDED through a bankroll,
which is what this tool does.

It answers two things the flat band answers badly:
  1. how much of the bankroll is at risk per trade, as the bankroll changes
  2. what a losing streak does -- fixed dollars can be wiped out by one streak,
     a fixed fraction cannot (0.95^12 still leaves 54%)

Usage:
    python -m tools.sizing_study
    python -m tools.sizing_study --bankroll 68 --trades 200 --paths 3000
"""
from __future__ import annotations

import argparse
import datetime as dt
import random
import statistics as st

from btcpredict.config import settings
from btcpredict.edge.signal import fee_rate_for_price
from btcpredict.models import build_model
from btcpredict.store.db import Store
from tools.backtest_candle_open import (ReplayParams, build_1m_history, replay,
                                        rolling_sigma)


def load_inputs(market: str = "btc-updown-5m"):
    """Quotes/settlements/sigma/history for one market, ready for `replay`.

    Split out of load_trades so tools that need the raw replay inputs (rather
    than the trades it produced) do not re-query the DB and drift apart.
    """
    store = Store(path=settings.db_path)
    like = market + "%"
    settled = store.query(
        "SELECT slug, strike, end_price, winner, start_ms FROM rounds "
        "WHERE winner IS NOT NULL AND strike IS NOT NULL AND end_price IS NOT NULL "
        "AND slug LIKE ? ORDER BY start_ms", (like,))
    quotes = store.query("""
        SELECT q.* FROM quotes q JOIN rounds r ON r.slug = q.slug
        WHERE r.winner IS NOT NULL AND q.slug LIKE ? ORDER BY q.ts_ms""", (like,))
    sbs = {r["slug"]: r for r in settled}
    sigs = {r["slug"]: rolling_sigma(settled, i) for i, r in enumerate(settled)}
    hist = build_1m_history(quotes, store=store, symbol="BTCUSDT")
    return quotes, sbs, sigs, hist


def load_trades(market: str = "btc-updown-5m"):
    quotes, sbs, sigs, hist = load_inputs(market)
    digital, drift = build_model("digital"), build_model(settings.up_model)
    res = replay(quotes, sbs, sigs, hist, digital, None, False,
                 params=ReplayParams.from_settings(), model_up=drift)
    trades = sorted(res["trades"], key=lambda t: t["ts_ms"])
    days = [dt.datetime.fromtimestamp(t["ts_ms"] / 1000, dt.timezone.utc).date()
            for t in trades]
    return trades, days


def flat_band(trades):
    """Today's live stakes, per trade."""
    lo, hi = settings.stake_ev_lo, settings.stake_ev_hi
    mn, mx = settings.min_stake_usdt, settings.max_stake_usdt
    out = []
    for t in trades:
        q = t["q"]
        ev = t["edge"] / q - fee_rate_for_price(q)
        f = min(1.0, max(0.0, (ev - lo) / (hi - lo)))
        out.append(mn + (mx - mn) * f)
    return out


def simulate(returns, stake_fn, bankroll, cap_pct=None, days=None):
    """One path. `stake_fn(i, bankroll)` returns the stake in dollars."""
    b, peak = bankroll, bankroll
    dd_pct, low = 0.0, bankroll
    taken = skipped = 0
    day, day_start = object(), bankroll
    for i, r in enumerate(returns):
        if b < settings.min_order_usdt:
            break
        if cap_pct:
            d = days[i] if days else 0
            if d != day:
                day, day_start = d, b
            if (b - day_start) <= -cap_pct * day_start:
                skipped += 1
                continue
        stake = stake_fn(i, b)
        if stake is None or stake < settings.min_order_usdt:
            skipped += 1
            continue
        stake = min(stake, b)
        b += r * stake
        taken += 1
        peak = max(peak, b)
        low = min(low, b)
        if peak > 0:
            dd_pct = min(dd_pct, (b - peak) / peak * 100.0)
    return {"final": b, "x": b / bankroll, "dd_pct": dd_pct, "low": low,
            "taken": taken, "skipped": skipped}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="btc-updown-5m")
    ap.add_argument("--bankroll", type=float, default=68.0)
    ap.add_argument("--trades", type=int, default=200)
    ap.add_argument("--paths", type=int, default=3000)
    args = ap.parse_args()

    trades, days = load_trades(args.market)
    ret = [t["pnl"] for t in trades]
    b0 = args.bankroll
    flat = flat_band(trades)
    m, sd = st.mean(ret), st.pstdev(ret)
    print(f"{len(ret)} replayed trades   per-$1 mean {m:+.4f}  sd {sd:.4f}")
    print(f"bankroll ${b0:.2f}   venue minimum ${settings.min_order_usdt:.2f}   "
          f"Kelly f* = mean/var = {m / sd ** 2 * 100:.1f}%")
    print(f"current band stakes ${min(flat):.2f}-${max(flat):.2f} = "
          f"{min(flat) / b0 * 100:.1f}%-{max(flat) / b0 * 100:.1f}% of bankroll\n")

    rules = {
        "flat $5-10 band (live)": lambda i, b: flat[i % len(flat)],
        "3% of bankroll": lambda i, b: b * 0.03,
        "5% of bankroll": lambda i, b: b * 0.05,
        "10% of bankroll": lambda i, b: b * 0.10,
    }

    print("=== the real sequence, in order ===")
    print(f"  {'rule':<26}{'final':>10}{'x':>7}{'maxDD%':>9}{'lowest$':>10}")
    for name, fn in rules.items():
        o = simulate(ret, fn, b0, days=days)
        print(f"  {name:<26}${o['final']:>9.2f}{o['x']:>7.2f}"
              f"{o['dd_pct']:>9.1f}${o['low']:>9.2f}")

    print("\n=== stress: 12 consecutive losses first ===")
    print("  (fixed dollars can be wiped out by one streak; a fraction cannot)")
    stress = [-1.0] * 12 + ret
    print(f"  {'rule':<26}{'after streak':>14}{'% of start':>12}{'final':>10}")
    for name, fn in rules.items():
        b = b0
        for i in range(12):
            if b < settings.min_order_usdt:
                break
            s = fn(i, b)
            if s is None or s < settings.min_order_usdt:
                break
            b += -1.0 * min(s, b)
        o = simulate(stress, fn, b0)
        print(f"  {name:<26}${b:>13.2f}{(b / b0 - 1) * 100:>11.0f}%"
              f"${o['final']:>9.2f}")

    print(f"\n=== bootstrap ({args.paths} paths x {args.trades} trades) ===")
    rng = random.Random(20260910)
    print(f"  {'rule':<26}{'median':>12}{'p05':>11}{'P(<$10)':>9}{'medDD%':>9}")
    for name, fn in rules.items():
        finals, dds, ruin = [], [], 0
        for _ in range(args.paths):
            seq = [rng.choice(ret) for _ in range(args.trades)]
            o = simulate(seq, fn, b0)
            finals.append(o["final"])
            dds.append(o["dd_pct"])
            ruin += o["final"] < 10
        finals.sort()
        print(f"  {name:<26}${st.median(finals):>11.2f}"
              f"${finals[int(0.05 * len(finals))]:>10.2f}"
              f"{ruin / args.paths:>9.1%}{st.median(dds):>9.1f}")

    print("\nNOTE: the bootstrap assumes the measured edge persists. It is an")
    print("estimate from ~70 trades, so treat the growth columns as an upper")
    print("bound and choose the fraction for survivability, not for growth.")


if __name__ == "__main__":
    main()
