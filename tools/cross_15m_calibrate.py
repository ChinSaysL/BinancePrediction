"""Do the 15m families carry an edge, and are their gates right?

Every 15m knob was inherited from btc-5m when the arm was widened to six
markets. They have never been calibrated on their own tape. The question was
forced by a live trade on 2026-09-15 00:59: bnb-15m was offered the CORRECT Up
and refused it on the 5-tick depth floor (spot was 2 ticks past its strike),
then took the opposite side 4s later on the reverse leg of the same whipsaw and
lost. bnb-5m, whose strike sat 7c lower, passed the floor on the up-leg and won.

Scored exactly as the 5m work: entry AT the limit
`ask + max(0.01, min(cap, inv*(1-ask)))`, the MEASURED 169bp fee, fills modelled
as "the ask did not run past the limit within the rest window", day-clustered t,
drop-best-day, bootstrap over ROUNDS.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import cross_knob, settings as S   # noqa: E402
from btcpredict.main import crossed_side                  # noqa: E402

FEE = 0.0169
FRAC = 0.0672


def triggers(q, rounds, *, floor_ticks, gate, tau_lo, tau_hi, hold):
    out = []
    for slug, g in q.groupby('slug', sort=False):
        rr = rounds.get(slug)
        if rr is None:
            continue
        g = g.sort_values('ts_ms')
        ts = g.ts_ms.to_numpy(float); sp = g.spot.to_numpy(float)
        ua = g.up_ask.to_numpy(float); da = g.down_ask.to_numpy(float)
        K = float(rr.strike); end = float(rr.end_ms)
        took, n, last = None, 0, -9e9
        for i in range(len(ts)):
            tau = (end - ts[i]) / 1000.0
            if not (tau_lo <= tau <= tau_hi):
                continue
            if ts[i] - last < S.cross_reentry_gap_s * 1000.0:
                continue
            j0 = int(np.searchsorted(ts, ts[i] - (S.cross_look_s + 2.0) * 1000.0, 'left'))
            if i - j0 < 1:
                continue
            side = crossed_side([(int(a), float(b)) for a, b in
                                 zip(ts[j0:i + 1], sp[j0:i + 1])], K, S.cross_look_s)
            if side is None or n >= S.cross_max_per_round:
                continue
            if took and took != side:
                continue
            dep = abs(sp[i] - K)
            if dep < floor_ticks * S.cross_tick_size - 1e-12:
                continue
            if 1e4 * dep / K > S.cross_max_depth_bps:
                continue
            rng = 1e4 * (np.nanmax(sp[:i + 1]) - np.nanmin(sp[:i + 1])) / K
            if gate > 0 and rng > gate:
                continue
            a0 = (ua if side == 'Up' else da)[i]
            if not np.isfinite(a0) or not (S.cross_min_price <= a0 <= S.cross_max_price):
                continue
            k = int(np.searchsorted(ts, ts[i] + hold * 1000.0, 'right'))
            fwd = (ua if side == 'Up' else da)[i + 1:max(k, i + 2)]
            fwd = fwd[np.isfinite(fwd)]
            out.append((pd.Timestamp(ts[i], unit='ms', tz='UTC').strftime('%Y-%m-%d'),
                        slug, float(a0),
                        float(np.min(fwd)) if len(fwd) else float(a0),
                        int(side == rr.winner)))
            took, n, last = side, n + 1, ts[i]
    return out


def fills(rows):
    recs = []
    for d, slug, a0, amin, won in rows:
        buf = max(0.01, min(S.cross_limit_buffer, S.cross_limit_buffer_inv * (1 - a0)))
        lim = round(a0 + buf, 2)
        if amin > lim + 1e-12:
            continue
        px = float(np.clip(lim, 1e-4, .9999))
        recs.append((d, slug, ((1 - px) / px - FEE) if won else -1.0, a0, won))
    return pd.DataFrame(recs, columns=['day', 'slug', 'ret', 'ask', 'won'])


def growth(t):
    return float(np.log1p(np.clip(FRAC * t.ret.to_numpy(), -0.999999, None)).sum()) if len(t) else 0.0


def dayt(t):
    lg = np.log1p(np.clip(FRAC * t.ret.to_numpy(), -0.999999, None))
    per = pd.Series(lg, index=t.day.to_numpy()).groupby(level=0).sum()
    sd = per.std(ddof=1)
    return per.mean() / (sd / np.sqrt(len(per))) if sd and len(per) > 2 else np.nan


def dropbest(t):
    per = pd.Series(np.log1p(np.clip(FRAC * t.ret.to_numpy(), -0.999999, None)),
                    index=t.day.to_numpy()).groupby(level=0).sum()
    return float(per.sum() - per.max()) if len(per) > 1 else np.nan


def boot(t, n=2000, seed=17):
    g = [x for _, x in t.groupby('slug', sort=False)]
    if len(g) < 5:
        return np.nan, np.nan
    rng = np.random.default_rng(seed); k = len(g)
    o = np.array([growth(pd.concat([g[j] for j in rng.integers(0, k, k)])) for _ in range(n)])
    return float(np.percentile(o, 2.5)), float(np.percentile(o, 97.5))


def report(lab, t):
    if len(t) < 10:
        print(f"  {lab:<22}{len(t):>5}  too few")
        return
    lo, hi = boot(t)
    print(f"  {lab:<22}{len(t):>5}{t.won.mean():>7.3f}{t.ask.mean():>7.3f}"
          f"{growth(t):>+9.3f}{dayt(t):>+8.2f}{dropbest(t):>+11.3f}"
          f"{f'[{lo:+.3f}, {hi:+.3f}]':>24}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--quotes', required=True)
    ap.add_argument('--rounds', required=True)
    ap.add_argument('--hold', type=float, default=20.0)
    a = ap.parse_args()
    Q = pd.read_csv(a.quotes)
    R = pd.read_csv(a.rounds)
    rounds = {t.slug: t for t in R.itertuples()}
    hdr = (f"  {'cell':<22}{'n':>5}{'win':>7}{'ask':>7}{'growth':>9}"
           f"{'day-t':>8}{'drop-best':>11}{'95% CI':>24}")
    for fam in ('btc-updown-15m', 'eth-updown-15m', 'bnb-updown-15m'):
        q = Q[Q.slug.str.startswith(fam)]
        if not len(q):
            continue
        shipped_gate = cross_knob(fam, 'max_round_range_bps')
        print(f"\n=== {fam}   {q.slug.nunique()} rounds   "
              f"shipped: floor {S.cross_min_depth_ticks:.0f} ticks, "
              f"gate {shipped_gate}, tau {S.cross_min_tau_s:.0f}-{S.cross_max_tau_s:.0f}s ===")
        print(hdr)
        base = dict(gate=shipped_gate, tau_lo=S.cross_min_tau_s,
                    tau_hi=S.cross_max_tau_s, hold=a.hold)
        report("SHIPPED", fills(triggers(q, rounds,
                                         floor_ticks=S.cross_min_depth_ticks, **base)))
        for ft in (0.0, 2.0, 3.0, 8.0, 12.0):
            report(f"floor {ft:.0f} ticks",
                   fills(triggers(q, rounds, floor_ticks=ft, **base)))
        report("range gate OFF",
               fills(triggers(q, rounds, floor_ticks=S.cross_min_depth_ticks,
                              gate=0.0, tau_lo=S.cross_min_tau_s,
                              tau_hi=S.cross_max_tau_s, hold=a.hold)))
        for lo, hi in ((5, 120), (5, 180), (60, 240)):
            report(f"tau {lo}-{hi}s",
                   fills(triggers(q, rounds, floor_ticks=S.cross_min_depth_ticks,
                                  gate=shipped_gate, tau_lo=lo, tau_hi=hi,
                                  hold=a.hold)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
