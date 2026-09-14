"""Early-round momentum, priced against the venue's OWN ask.

The corpus test of this idea was wrong in a way worth stating plainly: its
earliest snapshot is tau 270 -- thirty seconds into the round -- so "momentum
over the last 30s" IS "where spot sits relative to a strike set 30s ago".
AUC(delta_bps)=0.6368 and AUC(mom_30_bps)=0.6365 are the same number for the
same reason, and a 59-feature model scored 0.631, beating neither. Candle shape
(wick_up 0.486), reaction zones (dist_round100 0.496) and flow (cvd 0.504) were
all noise.

Worse, it was scored against an assumed 0.524 break-even. That is only the
venue's price in the FIRST SECOND. By tau 270 the venue has repriced the same
drift the signal is reading, so the real bar is whatever it is asking -- which
the corpus does not contain.

This fixes both. Recorded book, real asks, every family. For each round it takes
the momentum over the last W seconds at tau T, buys the side it points to, and
compares the hit rate to the price the venue is actually charging for that side
at that instant.

The bar is `ask + slippage`, per entry, with nothing assumed.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SLIP = 0.0037          # the model arm's MEASURED patient slippage
FEE = 200 / 1e4
FRAC = 0.0672


def entries(q, rounds, at_tau, win_s, min_mv):
    out = []
    for slug, g in q.groupby('slug', sort=False):
        rr = rounds.get(slug)
        if rr is None or not isinstance(rr.winner, str) or not np.isfinite(rr.strike):
            continue
        g = g.sort_values('ts_ms')
        ts = g.ts_ms.to_numpy(float); sp = g.spot.to_numpy(float)
        ua = g.up_ask.to_numpy(float); da = g.down_ask.to_numpy(float)
        end = float(rr.end_ms)
        tau = (end - ts) / 1000.0
        i = int(np.argmin(np.abs(tau - at_tau)))
        if abs(tau[i] - at_tau) > 4:
            continue
        j = int(np.searchsorted(ts, ts[i] - win_s * 1000.0, 'left'))
        if j >= i or sp[j] <= 0:
            continue
        mv = 1e4 * (sp[i] - sp[j]) / sp[j]
        if abs(mv) < min_mv:
            continue
        side = 'Up' if mv > 0 else 'Down'
        ask = ua[i] if side == 'Up' else da[i]
        if not np.isfinite(ask) or not (0.05 < ask < 0.95):
            continue
        out.append((pd.Timestamp(ts[i], unit='ms', tz='UTC').strftime('%Y-%m-%d'),
                    slug, slug.rsplit('-', 1)[0], side, float(ask), abs(mv),
                    int(side == rr.winner)))
    return pd.DataFrame(out, columns=['day', 'slug', 'pref', 'side', 'ask',
                                      'mv', 'won'])


def stats(t):
    fill = np.clip(t.ask.to_numpy() + SLIP, 1e-4, .9999)
    r = np.where(t.won.to_numpy() == 1, (1 - fill) / fill * (1 - FEE), -1.0)
    lg = np.log1p(np.clip(FRAC * r, -0.999999, None))
    per = pd.Series(lg, index=t.day.to_numpy()).groupby(level=0).sum()
    sd = per.std(ddof=1)
    tt = per.mean() / (sd / np.sqrt(len(per))) if sd and len(per) > 2 else np.nan
    return r.mean(), lg.sum(), tt


def boot(t, n=1500, seed=7):
    g = [x for _, x in t.groupby('slug', sort=False)]
    rng = np.random.default_rng(seed)
    k = len(g)
    o = np.empty(n)
    for i in range(n):
        o[i] = stats(pd.concat([g[j] for j in rng.integers(0, k, k)]))[0]
    return float(np.percentile(o, 2.5)), float(np.percentile(o, 97.5))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    ap.add_argument('--all-markets', action='store_true')
    a = ap.parse_args()
    q = pd.read_csv(f'{a.base}/quotes_arm.csv.gz')
    if not a.all_markets:
        q = q[q.slug.str.startswith('btc-updown-5m')]
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    rounds = {t.slug: t for t in r.itertuples()}

    print("entry tau | momentum window | min move -> hit rate vs the venue's ask\n")
    print(f"  {'tau':<5}{'win':<5}{'min':<6}{'n':>5}{'hit':>7}{'ask':>7}"
          f"{'break':>7}{'margin':>9}{'EV/$1':>9}{'growth/t':>16}")
    best = None
    for at_tau in (280, 270, 240, 210, 150):
        for win_s in (10, 30, 60):
            for mv in (0.0, 2.0, 4.0):
                t = entries(q, rounds, at_tau, win_s, mv)
                if len(t) < 40 or t.day.nunique() < 3:
                    continue
                ev, g, tt = stats(t)
                be = t.ask.mean() + SLIP
                margin = t.won.mean() - be
                flag = "  <<" if margin > 0 and ev > 0 else ""
                print(f"  {at_tau:<5}{win_s:<5}{mv:<6.0f}{len(t):>5}"
                      f"{t.won.mean():>7.3f}{t.ask.mean():>7.3f}{be:>7.3f}"
                      f"{margin:>+9.4f}{ev:>+9.4f}"
                      f"{f'{g:+.3f} / t{tt:+.2f}':>16}{flag}")
                if ev > 0 and (best is None or g > best[0]):
                    best = (g, at_tau, win_s, mv, t)

    if best is None:
        print("\n  nothing clears the venue's ask at any entry point.")
        return 0
    g, at_tau, win_s, mv, t = best
    lo, hi = boot(t)
    ev, g, tt = stats(t)
    print(f"\n=== best cell: tau {at_tau}, window {win_s}s, min move {mv:.0f}bps ===")
    print(f"  n={len(t)}  hit {t.won.mean():.4f}  ask {t.ask.mean():.4f}  "
          f"EV/$1 {ev:+.4f}  95% CI [{lo:+.4f}, {hi:+.4f}]")
    print(f"  growth {g:+.3f}   day-t {tt:+.2f}   over {t.day.nunique()} days")
    print(f"\n  {'family':<18}{'n':>6}{'hit':>8}{'ask':>8}{'margin':>9}")
    for p, x in t.groupby('pref'):
        if len(x) < 15:
            continue
        print(f"  {p:<18}{len(x):>6}{x.won.mean():>8.3f}{x.ask.mean():>8.3f}"
              f"{x.won.mean()-x.ask.mean()-SLIP:>+9.4f}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
