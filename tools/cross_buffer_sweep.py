"""What does `cross_limit_buffer` actually cost, and what is the right value?

The arm decides at ask `a` and sends a limit at `a + buf`, where

    buf = max(0.01, min(cross_limit_buffer 0.10, cross_limit_buffer_pct 0.20 * a))

Live fills land AT that limit, not at `a`: over 13 fills, mean(got - limit) is
-0.028 while mean(got - decided) is +0.063. So the buffer is very nearly a
straight markup on every trade -- and at a mean fill of 0.629 against a corpus
win rate of 0.714, the ENTIRE edge is 0.085. A 0.09 buffer is therefore about
the size of the edge it is spending.

The obvious question -- can the buffer be smaller? -- cannot be answered from
the 20 live attempts we have; that is exactly the sample size that produced the
config this repo spent a day dismantling. But it CAN be answered from the
recorded book, which carries the ask second by second for 7 days:

    for each trigger, send a limit at a+buf and walk the ask forward. If the
    ask trades at or below the limit within `limit_wait_s`, it fills, and it
    fills at the better of (limit, ask at that moment). Otherwise it rests and
    is cancelled -- exactly what the 7 live misses did.

That turns "what buffer?" into a question with ~200 triggers and 7 day-blocks
behind it instead of 20 attempts, and it prices the MISSES correctly: a miss is
not a loss, it is a trade we never made, which is the whole reason the naive
"wider buffer = more fills = better" argument fails.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings as S            # noqa: E402
from btcpredict.main import crossed_side               # noqa: E402

FEE = 200 / 1e4
FRAC = 0.0504          # stake_fraction 0.0224 * cross_stake_mult 2.25
LAT = 3.0              # seconds from decision to the order resting on the book;
                       # live `cross exec` elapsed ran 2.67-5.21s on fills


def triggers(q, rounds):
    """Every entry the shipped gate takes, with the forward ask path."""
    out = []
    for slug, g in q.groupby('slug', sort=False):
        rr = rounds.get(slug)
        if rr is None or not np.isfinite(rr.strike):
            continue
        g = g.sort_values('ts_ms')
        ts = g.ts_ms.to_numpy(float); sp = g.spot.to_numpy(float)
        ua = g.up_ask.to_numpy(float); da = g.down_ask.to_numpy(float)
        K = float(rr.strike); end = float(rr.end_ms)
        took, n = None, 0
        for i in range(len(ts)):
            tau = (end - ts[i]) / 1000.0
            if not (S.cross_min_tau_s <= tau <= S.cross_max_tau_s):
                continue
            j0 = int(np.searchsorted(ts, ts[i] - (S.cross_look_s + 2.0) * 1000.0, 'left'))
            if i - j0 < 1:
                continue
            side = crossed_side([(int(a), float(b)) for a, b in
                                 zip(ts[j0:i + 1], sp[j0:i + 1])], K, S.cross_look_s)
            if side is None or n >= S.cross_max_per_round:
                continue
            if not S.cross_allow_reversal and took and took != side:
                continue
            if 1e4 * abs(sp[i] - K) / K > S.cross_max_depth_bps:
                continue
            seen = sp[:i + 1]
            rg = 1e4 * (np.nanmax(seen) - np.nanmin(seen)) / K
            if S.cross_max_round_range_bps > 0 and rg > S.cross_max_round_range_bps:
                continue
            ask = ua[i] if side == 'Up' else da[i]
            if not np.isfinite(ask) or not (S.cross_min_price <= ask <= S.cross_max_price):
                continue
            # forward ask path, capped by the bell as _place_order does
            # LATENCY IS THE WHOLE POINT. The ask at the trigger instant is
            # by construction <= any limit above it, so a path starting at i
            # fills 100% of the time at every buffer -- which is what the first
            # version of this tool reported, and it is meaningless. Live, the
            # order reaches the book 2.7-5.2s later (measured, `cross exec`
            # lines) and the book has moved by then. The path therefore starts
            # at t + latency.
            wait = min(S.limit_wait_s, max(1.0, tau - 1.0))
            k0 = int(np.searchsorted(ts, ts[i] + LAT * 1000.0, 'left'))
            k1 = int(np.searchsorted(ts, ts[i] + wait * 1000.0, 'right'))
            path = (ua if side == 'Up' else da)[k0:k1]
            path = path[np.isfinite(path)]
            out.append((pd.Timestamp(ts[i], unit='ms', tz='UTC').strftime('%Y-%m-%d'),
                        slug, side, ask, int(side == rr.winner), path))
            took, n = side, n + 1
    return out


def simulate(trig, buf_flat, buf_pct):
    """Fill/miss and fill price for one buffer setting."""
    day, won, px = [], [], []
    miss = 0
    for d, _slug, _side, ask, w, path in trig:
        buf = max(0.01, min(buf_flat, buf_pct * ask)) if buf_pct > 0 else buf_flat
        limit = min(round(ask + buf, 2), 0.99)
        hit = path[path <= limit]
        if len(hit) == 0:
            miss += 1
            continue
        # fills at the better of (limit, the ask that crossed it)
        day.append(d); won.append(w); px.append(min(limit, float(hit[0])))
    return pd.DataFrame({'day': day, 'won': won, 'ask': px}), miss


def score(t):
    if len(t) < 10 or t.day.nunique() < 3:
        return None
    fill = np.clip(t.ask.to_numpy(), 1e-4, .9999)
    r = np.where(t.won.to_numpy() == 1, (1 - fill) / fill * (1 - FEE), -1.0)
    lg = np.log1p(np.clip(FRAC * r, -0.999999, None))
    per = pd.Series(lg, index=t.day.to_numpy()).groupby(level=0).sum()
    sd = per.std(ddof=1)
    tt = per.mean() / (sd / np.sqrt(len(per))) if sd else np.nan
    return lg.sum(), tt, per.drop(per.idxmax()).sum()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    ap.add_argument('--latency', type=float, default=3.0)
    a = ap.parse_args()
    global LAT
    LAT = a.latency
    q = pd.read_csv(f'{a.base}/quotes_arm.csv.gz')
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    q = q[q.slug.str.startswith('btc-updown-5m')]
    rounds = {t.slug: t for t in r.itertuples() if isinstance(t.winner, str)}
    trig = triggers(q, rounds)
    print(f"triggers on the recorded book: {len(trig)}  "
          f"days {len({t[0] for t in trig})}")
    print(f"shipped: flat {S.cross_limit_buffer} pct {S.cross_limit_buffer_pct} "
          f"wait {S.limit_wait_s}s\n")
    print(f"  {'buffer':<18}{'fills':>6}{'miss':>6}{'fill%':>7}{'win':>7}"
          f"{'mean px':>9}   growth / day-t / drop-best")
    rowsets = [(0.02, 0.0), (0.04, 0.0), (0.06, 0.0), (0.08, 0.0),
               (0.10, 0.0), (0.15, 0.0),
               (0.10, 0.20), (0.10, 0.10), (0.06, 0.10), (0.04, 0.08)]
    for bf, bp in rowsets:
        t, miss = simulate(trig, bf, bp)
        s = score(t)
        lab = f"flat {bf:.2f}" if bp == 0 else f"min({bf:.2f},{bp:.2f}*a)"
        if s is None:
            print(f"  {lab:<18}{len(t):>6}{miss:>6}  (too few)")
            continue
        mark = "  <- SHIPPED" if (bf, bp) == (S.cross_limit_buffer,
                                              S.cross_limit_buffer_pct) else ""
        print(f"  {lab:<18}{len(t):>6}{miss:>6}{100*len(t)/max(1,len(trig)):>7.1f}"
              f"{t.won.mean():>7.3f}{t.ask.mean():>9.3f}   "
              f"{s[0]:+.3f} / {s[1]:+.2f} / {s[2]:+.3f}{mark}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
