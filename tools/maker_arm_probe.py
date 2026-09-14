"""Can the arm EARN the spread at tau 60-210 instead of paying it?

The model arm is dead as a taker: at tau 60-210 its edge (+0.003 to +0.024) is
the same order as the half-spread it must cross (+0.0057), while the cross arm's
edge at tau 5-60 is 21x its spread. That is why every knob, gate and feature
swept flat -- none of them change an edge that is fighting the bid-ask.

The one form not yet tested is MAKING. A resting buy at the bid does not pay
the spread, it collects it. On the same 0.6-cent scale that is the difference
between a rounding error and a real number.

The catch is adverse selection, and it is structural rather than incidental:

    a resting buy on Up at price B fills when the ASK falls to B or below.
    The ask only falls that far when the market has moved AGAINST Up.

So you are filled precisely when you are wrong. The question is entirely
quantitative -- whether the price you get compensates for the news that getting
it delivers.

Measured three ways:
  * FILLED  win rate and edge of orders that actually filled
  * ALL     win rate of every round at the same posting price -- the
            counterfactual if fills were random rather than selected
  * the gap between them IS the adverse selection, in win-rate points

No model, no signal, no gate: this asks whether the mechanism can work at all
before asking what to post on.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FEE = 200 / 1e4


def simulate(q, rounds, post_tau, until_tau, improve_ticks, tick=0.01):
    """Post at (bid + improve) at `post_tau`; fill if the ask reaches it."""
    out = []
    for slug, g in q.groupby('slug', sort=False):
        rr = rounds.get(slug)
        if rr is None or not isinstance(rr.winner, str):
            continue
        g = g.sort_values('ts_ms')
        tau = g.tau_s.to_numpy(float)
        i = int(np.argmin(np.abs(tau - post_tau)))
        if abs(tau[i] - post_tau) > 5:
            continue
        for side in ('Up', 'Down'):
            bid = g.up_bid.to_numpy(float)[i] if side == 'Up' else g.down_bid.to_numpy(float)[i]
            if not np.isfinite(bid) or not (0.02 < bid < 0.98):
                continue
            post = round(bid + improve_ticks * tick, 2)
            if post >= 0.99:
                continue
            fwd = g[(g.tau_s < tau[i]) & (g.tau_s >= until_tau)]
            asks = (fwd.up_ask if side == 'Up' else fwd.down_ask).to_numpy(float)
            asks = asks[np.isfinite(asks)]
            filled = bool(len(asks)) and bool((asks <= post + 1e-9).any())
            out.append((pd.Timestamp(g.ts_ms.iloc[i], unit='ms', tz='UTC')
                        .strftime('%Y-%m-%d'), slug, side, post, int(filled),
                        int(side == rr.winner)))
    return pd.DataFrame(out, columns=['day', 'slug', 'side', 'post',
                                      'filled', 'won'])


def boot(t, fn, n=2000, seed=5):
    g = [x for _, x in t.groupby('slug', sort=False)]
    rng = np.random.default_rng(seed)
    k = len(g)
    o = np.empty(n)
    for i in range(n):
        o[i] = fn(pd.concat([g[j] for j in rng.integers(0, k, k)]))
    return float(np.nanpercentile(o, 2.5)), float(np.nanpercentile(o, 97.5))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    ap.add_argument('--market', default='btc-updown-5m')
    a = ap.parse_args()
    q = pd.read_csv(f'{a.base}/quotes_arm.csv.gz')
    q = q[q.slug.str.startswith(a.market)]
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    rounds = {t.slug: t for t in r.itertuples()}

    print(f"{a.market}: posting at the bid at tau 210, resting until tau 60\n")
    print(f"  {'post':<16}{'orders':>8}{'filled':>8}{'fill%':>7}"
          f"{'win|filled':>12}{'win|all':>9}{'adverse':>9}{'edge|filled':>13}")
    for improve in (0, 1, 2):
        t = simulate(q, rounds, 210, 60, improve)
        if t.empty:
            continue
        f = t[t.filled == 1]
        if len(f) < 20:
            print(f"  {f'bid+{improve}':<16}{len(t):>8}{len(f):>8}  (too few fills)")
            continue
        adverse = f.won.mean() - t.won.mean()
        edge = (f.won - f.post).mean()
        print(f"  {f'bid+{improve} tick':<16}{len(t):>8}{len(f):>8}"
              f"{100*len(f)/len(t):>6.0f}%{f.won.mean():>12.3f}{t.won.mean():>9.3f}"
              f"{adverse:>+9.3f}{edge:>+13.4f}")

    t = simulate(q, rounds, 210, 60, 0)
    f = t[t.filled == 1]
    if len(f) >= 20:
        lo, hi = boot(f, lambda x: (x.won - x.post).mean())
        print(f"\n  edge on filled maker orders (bid+0): "
              f"{(f.won - f.post).mean():+.4f}  95% CI [{lo:+.4f}, {hi:+.4f}]")
        ret = np.where(f.won == 1, (1 - f.post) / f.post * (1 - FEE), -1.0)
        print(f"  EV per $1 staked: {ret.mean():+.4f}")
        print(f"\n  by posting price:")
        print(f"    {'price':<12}{'n':>6}{'win':>8}{'edge':>9}")
        for lo_, hi_ in ((0.02, 0.30), (0.30, 0.45), (0.45, 0.55),
                         (0.55, 0.70), (0.70, 0.98)):
            b = f[(f.post >= lo_) & (f.post < hi_)]
            if len(b) < 10:
                continue
            print(f"    {f'{lo_:.2f}-{hi_:.2f}':<12}{len(b):>6}{b.won.mean():>8.3f}"
                  f"{(b.won - b.post).mean():>+9.4f}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
