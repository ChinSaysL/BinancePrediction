"""Buy the FAVOURITE when the round has already drifted. Does it pay?

Not the same as the momentum probe. That one fired on a short burst (2-30s
trailing return) and asked whether the venue lagged it -- it does, by +0.046,
for under a second. This asks a different question, the one a trending regime
suggests: when spot has already drifted well away from the strike with a couple
of minutes left, the round is close to decided. Buy the side that is winning and
collect the remainder.

    19:40  drift +11.90 bps  fav ask 0.940  -> won
    19:55  drift  -9.43 bps  fav ask 0.940  -> won
    20:15  drift +17.12 bps  fav ask 0.980  -> won

The catch is the price. At an ask of 0.94 a win pays (1-0.94)/0.94 = +0.064 and
a loss costs 1.000, so break-even is a 94% win rate -- ONE loss undoes fifteen
wins. Everything depends on whether the venue prices that last stretch too
cheaply or about right, and on whether a 0.4-cent slippage still fits inside a
6-cent margin.

Scored at the model arm's MEASURED patient slippage (+0.0037), because nothing
about this trigger is racing -- the drift is minutes old by construction. The
urgent models are shown too, for contrast.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SLIP = {'patient': lambda q: q + 0.0037,
        'book': lambda q: q,
        'additive': lambda q: q + 0.088}
FEE = 200 / 1e4
FRAC = 0.0672


def entries(q, rounds, at_tau, min_drift, max_ask):
    out = []
    for slug, g in q.groupby('slug', sort=False):
        rr = rounds.get(slug)
        if rr is None or not isinstance(rr.winner, str) or not np.isfinite(rr.strike):
            continue
        g = g.sort_values('ts_ms')
        ts = g.ts_ms.to_numpy(float); sp = g.spot.to_numpy(float)
        ua = g.up_ask.to_numpy(float); da = g.down_ask.to_numpy(float)
        K = float(rr.strike); end = float(rr.end_ms)
        tau = (end - ts) / 1000.0
        i = int(np.argmin(np.abs(tau - at_tau)))
        if abs(tau[i] - at_tau) > 6:
            continue
        drift = 1e4 * (sp[i] - K) / K
        if abs(drift) < min_drift:
            continue
        side = 'Up' if drift > 0 else 'Down'
        ask = ua[i] if side == 'Up' else da[i]
        if not np.isfinite(ask) or not (0.02 < ask <= max_ask):
            continue
        out.append((pd.Timestamp(ts[i], unit='ms', tz='UTC').strftime('%Y-%m-%d'),
                    slug, side, float(ask), abs(drift), int(side == rr.winner)))
    return pd.DataFrame(out, columns=['day', 'slug', 'side', 'ask', 'drift', 'won'])


def score(t, model):
    fill = np.clip(SLIP[model](t.ask.to_numpy()), 1e-4, .9999)
    r = np.where(t.won.to_numpy() == 1, (1 - fill) / fill * (1 - FEE), -1.0)
    lg = np.log1p(np.clip(FRAC * r, -0.999999, None))
    per = pd.Series(lg, index=t.day.to_numpy()).groupby(level=0).sum()
    sd = per.std(ddof=1)
    tt = per.mean() / (sd / np.sqrt(len(per))) if sd and len(per) > 2 else np.nan
    return r.mean(), lg.sum(), tt


def boot_ev(t, model, n=2000, seed=3):
    g = [x for _, x in t.groupby('slug', sort=False)]
    rng = np.random.default_rng(seed)
    k = len(g)
    o = np.empty(n)
    for i in range(n):
        o[i] = score(pd.concat([g[j] for j in rng.integers(0, k, k)]), model)[0]
    return float(np.percentile(o, 2.5)), float(np.percentile(o, 97.5))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    ap.add_argument('--market', default='btc-updown-5m')
    a = ap.parse_args()
    q = pd.read_csv(f'{a.base}/quotes_arm.csv.gz')
    q = q[q.slug.str.startswith(a.market)]
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    rounds = {t.slug: t for t in r.itertuples()}

    print(f"{a.market}: buy the favourite at tau T when |drift| exceeds a floor\n")
    print(f"  {'tau':<5}{'drift>':<8}{'n':>5}{'win':>7}{'ask':>7}{'break':>7}"
          f"{'margin':>9}{'EV/$1 patient':>15}{'growth/t':>16}")
    for at_tau in (150, 90, 60):
        for md in (0.0, 2.0, 4.0, 8.0):
            t = entries(q, rounds, at_tau, md, 0.98)
            if len(t) < 30 or t.day.nunique() < 3:
                continue
            ev, g, tt = score(t, 'patient')
            be = t.ask.mean() + 0.0037
            print(f"  {at_tau:<5}{md:<8.0f}{len(t):>5}{t.won.mean():>7.3f}"
                  f"{t.ask.mean():>7.3f}{be:>7.3f}{t.won.mean()-be:>+9.4f}"
                  f"{ev:>+15.4f}{f'{g:+.3f} / t{tt:+.2f}':>16}")

    print("\n=== the high-drift cell, in detail ===")
    t = entries(q, rounds, 150, 8.0, 0.98)
    if len(t) >= 30:
        for m in SLIP:
            ev, g, tt = score(t, m)
            lo, hi = boot_ev(t, m)
            print(f"  {m:<10} EV/$1 {ev:+.4f}  95% CI [{lo:+.4f},{hi:+.4f}]"
                  f"   growth {g:+.3f}  day-t {tt:+.2f}")
        print(f"\n  {'ask band':<14}{'n':>6}{'win':>8}{'breakeven':>11}{'margin':>9}")
        for lo_, hi_ in ((0.50, 0.80), (0.80, 0.90), (0.90, 0.95), (0.95, 0.98)):
            b = t[(t.ask >= lo_) & (t.ask < hi_)]
            if len(b) < 8:
                continue
            be = b.ask.mean() + 0.0037
            print(f"  {f'{lo_:.2f}-{hi_:.2f}':<14}{len(b):>6}{b.won.mean():>8.3f}"
                  f"{be:>11.3f}{b.won.mean()-be:>+9.4f}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
