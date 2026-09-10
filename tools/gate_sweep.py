"""Is the up_regime_gate lever still earning its keep?

It was justified on 25 rounds under the OLD up model ("Up WR 0.50 in rising
hours vs 0.82 flat/down") and it is currently ON at 0.005. Both halves of that
justification need re-testing now that Up is priced by the calibrated driftless
model, on the full recording, with a train/test split.

The gate blocks Up when the last hour's log return exceeds the threshold, and a
blocked Up falls back to a Down candidate on the same quote -- so it changes the
trade MIX, not just the count.

Usage:  python -m tools.gate_sweep
"""
from __future__ import annotations

import statistics as st
from dataclasses import replace

from btcpredict.config import settings
from btcpredict.models import build_model
from btcpredict.store.db import Store
from tools.backtest_candle_open import (ReplayParams, build_1m_history, replay,
                                        rolling_sigma)

GATES = (0.0, 0.002, 0.005, 0.01, 0.02, 0.05)


def load(market: str, symbol: str):
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
    hist = build_1m_history(quotes, store=store, symbol=symbol)
    store.close()
    return quotes, sbs, sigs, hist


def side_stats(trades, side):
    s = [t for t in trades if t["side"] == side]
    if not s:
        return None
    return {"n": len(s), "wr": sum(t["won"] for t in s) / len(s),
            "claim": st.mean(t["p"] for t in s),
            "pnl": sum(t["pnl"] for t in s)}


def main() -> None:
    base_params = ReplayParams.from_settings()
    print(f"live settings: up_model={settings.up_model} "
          f"up_regime_gate={settings.up_regime_gate} "
          f"min_edge={settings.min_edge} edge_haircut={settings.edge_haircut}\n")

    for market, symbol in (("btc-updown-5m", "BTCUSDT"),):
        quotes, sbs, sigs, hist = load(market, symbol)
        if not sbs:
            continue
        digital = build_model("digital")
        span = [q["ts_ms"] for q in quotes]
        split = span[0] + (span[-1] - span[0]) // 2

        print(f"=== {market}: {len(sbs)} settled rounds ===")
        print(f"  {'gate':>7}{'trades':>8}{'Up n':>6}{'Up WR':>7}{'Up claim':>10}"
              f"{'Up pnl':>9}{'Down n':>8}{'Down pnl':>10}{'total pnl':>11}"
              f"{'test pnl':>10}")
        for g in GATES:
            p = replace(base_params, up_regime_gate=g)
            res = replay(quotes, sbs, sigs, hist, digital, None, False,
                         params=p, model_up=None)
            t = res["trades"]
            if not t:
                continue
            up, dn = side_stats(t, "Up"), side_stats(t, "Down")
            test = [x for x in t if x["ts_ms"] >= split]
            print(f"  {g:>7.3f}{len(t):>8}"
                  f"{(up['n'] if up else 0):>6}"
                  f"{(up['wr'] if up else 0):>7.2f}"
                  f"{(up['claim'] if up else 0):>10.3f}"
                  f"{(up['pnl'] if up else 0):>+9.1f}"
                  f"{(dn['n'] if dn else 0):>8}"
                  f"{(dn['pnl'] if dn else 0):>+10.1f}"
                  f"{sum(x['pnl'] for x in t):>+11.1f}"
                  f"{sum(x['pnl'] for x in test):>+10.1f}")
        print()


if __name__ == "__main__":
    main()
