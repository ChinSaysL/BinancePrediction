"""Re-sweep the cross knobs that the depth change invalidated.

Everything except the depth gate was tuned at depth 0.5bps. The gate is now
5.0, a 10x widening, so any knob that interacts with it is tuned against a
strategy that no longer exists.

POWER, stated first because it decides what is trustworthy here:
  win rate   31 days of real Chainlink strikes + real settlements (1,413
             crossings). Well powered; day-clustered t is meaningful.
  EV         5 days that carry usable quotes (btc5m_prices.parquet samples
             every 4th day and its last three degrade to a 0.02 median ask on
             5-91 rows). ANY EV number here is 5 day-blocks. Treat a sweep over
             it as a hypothesis, not a tuning.

So: sweep tau and look on the 31-day win rate, and price bands on the 5 priced
days, and do not pretend the second is as solid as the first.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings as S     # noqa: E402

CLEAN = [20572, 20576, 20580, 20584, 20588]


def build(lo, hi, look_steps, dep, cap, priced):
    """Replay crossings. `look_steps` = samples back for the reference
    (the corpus grid is ~13s apart, so 1 step is the finest available)."""
    ml = pd.read_parquet('data/btc5m_ml.parquet',
                         columns=['window_id', 'day', 'tau_s', 'cl_delta_bps', 'y_up']).dropna()
    if priced:
        px = pd.read_parquet('data/btc5m_prices.parquet',
                             columns=['window_id', 'tau_s', 'up_sell_px', 'dn_sell_px'])
        ml = ml.merge(px, on=['window_id', 'tau_s'], how='inner')
        ml = ml[ml.day.isin(CLEAN)]
    ml = ml.sort_values(['window_id', 'tau_s'], ascending=[True, False])
    rows = []
    for _w, g in ml.groupby('window_id', sort=False):
        d = g.cl_delta_bps.to_numpy(); tau = g.tau_s.to_numpy()
        y = int(g.y_up.iloc[0]); day = g.day.iloc[0]; n = 0
        ua = g.up_sell_px.to_numpy() if priced else None
        da = g.dn_sell_px.to_numpy() if priced else None
        for i in range(look_steps, len(d)):
            if not (lo <= tau[i] <= hi):
                continue
            j = i - look_steps
            if np.sign(d[i]) == np.sign(d[j]) or d[i] == 0:
                continue
            if abs(d[i]) > dep:
                continue
            up = d[i] > 0
            ask = None
            if priced:
                ask = ua[i] if up else da[i]
                if not (0.0 < ask < 1.0):
                    continue
            rows.append((day, abs(d[i]), tau[i], int(up == bool(y)), ask))
            n += 1
            if n >= cap:
                break
    t = pd.DataFrame(rows, columns=['day', 'm', 'tau', 'won', 'ask'])
    if priced and len(t):
        fee = 200.0 * np.minimum(1, (1 - t.ask) / t.ask) / 1e4
        t['ret'] = np.where(t.won == 1, (1 - t.ask) / t.ask * (1 - fee), -1.0)
    return t


def dayt(t, col, base=0.0):
    g = t.groupby('day')[col].mean()
    if len(g) < 2:
        return float('nan')
    return (g.mean() - base) / (g.std(ddof=1) / np.sqrt(len(g)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="all")
    a = ap.parse_args()
    dep, cap = S.cross_max_depth_bps, S.cross_max_per_round

    if a.only in ("all", "tau"):
        print("=== TAU WINDOW -- 31 days, win rate (WELL POWERED) ===")
        print(f"{'tau':>12} {'n':>6} {'/day':>6} {'win':>7} {'day-t vs .5':>11}")
        for lo, hi in ((10, 30), (10, 60), (10, 90), (10, 120), (20, 60),
                       (30, 90), (10, 180), (3, 60)):
            t = build(lo, hi, 1, dep, cap, priced=False)
            if len(t) < 30:
                continue
            print(f"{f'{lo}-{hi}':>12} {len(t):6d} {len(t)/t.day.nunique():6.1f} "
                  f"{t.won.mean():7.3f} {dayt(t,'won',0.5):11.2f}")

    if a.only in ("all", "price"):
        print("\n=== PRICE BAND -- 5 priced days, EV (WEAK: 5 day-blocks) ===")
        base = build(S.cross_min_tau_s, S.cross_max_tau_s, 1, dep, cap, priced=True)
        print(f"{'band':>12} {'n':>5} {'share':>7} {'ask':>6} {'win':>7} "
              f"{'EV/$1':>8} {'day-t':>7}")
        for mn, mx in ((0.10, 0.90), (0.10, 0.75), (0.10, 0.60), (0.02, 0.90),
                       (0.30, 0.90), (0.30, 0.75), (0.10, 0.50)):
            x = base[(base.ask >= mn) & (base.ask <= mx)]
            if len(x) < 20:
                continue
            print(f"{f'{mn:.2f}-{mx:.2f}':>12} {len(x):5d} "
                  f"{100*len(x)/len(base):6.1f}% {x.ask.mean():6.3f} "
                  f"{x.won.mean():7.3f} {x.ret.mean():+8.3f} {dayt(x,'ret'):7.2f}")

    if a.only in ("all", "round"):
        print("\n=== ENTRIES PER ROUND -- 5 priced days, EV (WEAK) ===")
        print(f"{'cap':>5} {'n':>5} {'EV/$1':>8} {'total':>8} {'day-t':>7}")
        for c in (1, 2, 3, 5, 99):
            t = build(S.cross_min_tau_s, S.cross_max_tau_s, 1, dep, c, priced=True)
            print(f"{c:5d} {len(t):5d} {t.ret.mean():+8.3f} {t.ret.sum():+8.1f} "
                  f"{dayt(t,'ret'):7.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
