"""Best stake fraction for a CROSS-ONLY account.

Sizing this arm off a mean EV would be wrong: its return distribution is
violently skewed. On the corpus, 17% of trades (ask < 0.30) carry 74% of the
profit and win only 28.6% of the time, so the mean is a lottery-ticket average.
This resamples the ACTUAL per-trade returns, in whole-day blocks, so both the
skew and the within-day correlation survive into the simulation.

Returns come from the 31-day corpus with the venue's REAL Chainlink strike
(`strike_cl`) and its REAL asks (`*_sell_px`), at the shipped gate.

Two regimes, because live fill data disagrees about the cheap tail:
  all       every qualifying crossing, mean EV +0.314/$1
  fillable  ask >= 0.30 only. Live, 4 of 4 unfilled attempts had decided asks
            of 0.14-0.45, so the cheap tail may not be reachable. Mean EV
            +0.098/$1 -- but a HIGHER day-t (1.91 vs 0.59), so it is the more
            reliable half of the edge, not just the smaller one.

Modelled faithfully: compounding after every trade, the 20% account-wide daily
stop, the $1 venue floor, and the $55 median resting at the touch (a stake
above it cannot fill at the quoted price).
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings as S     # noqa: E402


def corpus_returns():
    ml = pd.read_parquet('data/btc5m_ml.parquet',
                         columns=['window_id', 'day', 'tau_s', 'cl_delta_bps', 'y_up']).dropna()
    px = pd.read_parquet('data/btc5m_prices.parquet',
                         columns=['window_id', 'tau_s', 'up_sell_px', 'dn_sell_px'])
    m = ml.merge(px, on=['window_id', 'tau_s'], how='inner').sort_values(
        ['window_id', 'tau_s'], ascending=[True, False])
    rows = []
    for _wid, g in m.groupby('window_id', sort=False):
        d = g.cl_delta_bps.to_numpy(); tau = g.tau_s.to_numpy()
        ua = g.up_sell_px.to_numpy(); da = g.dn_sell_px.to_numpy()
        y = int(g.y_up.iloc[0]); day = g.day.iloc[0]; n = 0
        for i in range(1, len(d)):
            if not (S.cross_min_tau_s <= tau[i] <= S.cross_max_tau_s):
                continue
            if np.sign(d[i]) == np.sign(d[i - 1]) or d[i] == 0:
                continue
            if abs(d[i]) > S.cross_max_depth_bps:
                continue
            up = d[i] > 0
            ask = ua[i] if up else da[i]
            if not (0.0 < ask < 1.0):
                continue
            w = int(up == bool(y))
            fee = 200.0 * min(1.0, (1 - ask) / ask) / 1e4
            rows.append((day, ask, w, (1 - ask) / ask * (1 - fee) if w else -1.0))
            n += 1
            if n >= S.cross_max_per_round:
                break
    return pd.DataFrame(rows, columns=['day', 'ask', 'won', 'ret'])


def simulate(days_ret, frac, days, paths, start, rng, depth_cap=55.0,
             stop=0.20, floor=1.0):
    """days_ret: list of per-day arrays of trade returns. Resampled by day."""
    k = len(days_ret)
    bank = np.full(paths, float(start))
    stopped = np.zeros(paths)
    for _ in range(days):
        pick = rng.integers(0, k, paths)
        day_start = bank.copy()
        halted = np.zeros(paths, dtype=bool)
        maxlen = max(len(d) for d in days_ret)
        for j in range(maxlen):
            r = np.full(paths, np.nan)
            for p_i in range(k):                       # vectorise per source day
                sel = (pick == p_i)
                if not sel.any():
                    continue
                arr = days_ret[p_i]
                if j < len(arr):
                    r[sel] = arr[j]
            live = (~halted) & np.isfinite(r)
            if not live.any():
                continue
            stake = np.maximum(bank * frac, floor)
            if depth_cap:
                stake = np.minimum(stake, depth_cap)
            stake = np.minimum(stake, bank)
            pnl = np.where(live, stake * np.nan_to_num(r), 0.0)
            bank = np.maximum(bank + pnl, 0.0)
            newly = (~halted) & (bank < day_start * (1 - stop))
            stopped += newly
            halted |= newly
    return bank, stopped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=50.0)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--paths", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--depth-cap", type=float, default=55.0)
    a = ap.parse_args()

    t = corpus_returns()
    print(f"corpus: {len(t)} crossings over {t.day.nunique()} days, gate "
          f"{S.cross_max_depth_bps}bps tau {S.cross_min_tau_s:.0f}-{S.cross_max_tau_s:.0f}")

    for label, sub in (("all asks", t), ("fillable (ask>=0.30)", t[t.ask >= 0.30])):
        days_ret = [g.ret.to_numpy() for _, g in sub.groupby('day')]
        ev = sub.ret.mean()
        # full-Kelly reference on the empirical distribution
        grid = np.linspace(0.001, 0.99, 2000)
        gmean = [np.mean(np.log1p(np.clip(f * sub.ret.to_numpy(), -0.999, None))) for f in grid]
        kelly = grid[int(np.argmax(gmean))]
        print(f"\n=== {label}:  n={len(sub)}  mean EV {ev:+.4f}/$1  "
              f"trades/day {len(sub)/sub.day.nunique():.1f}  full-Kelly {kelly:.1%} ===")
        print(f"{'frac':>7} {'x mult':>7} {'median':>11} {'p10':>10} {'p90':>12} "
              f"{'P(<start)':>10} {'P(ruin)':>8} {'stops/30d':>10}")
        for frac in (0.0224, 0.0336, 0.05, 0.075, 0.10, 0.15, 0.20, 0.30):
            rng = np.random.default_rng(a.seed)
            bank, stops = simulate(days_ret, frac, a.days, a.paths, a.start, rng,
                                   a.depth_cap)
            print(f"{frac:7.2%} {frac/0.0224:7.1f} ${np.median(bank):10.2f} "
                  f"${np.percentile(bank,10):9.2f} ${np.percentile(bank,90):11.2f} "
                  f"{np.mean(bank<a.start):10.1%} {np.mean(bank<5):8.1%} "
                  f"{np.mean(stops):10.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
