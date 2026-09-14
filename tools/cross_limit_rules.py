"""Can a smarter limit price buy fills that are WORTH having?

Nine live attempts have now been cancelled unfilled and every one was cheap
(decided ask 0.14-0.57, median 0.36); no attempt at a decided ask of 0.60+ has
ever missed. The shipped rule is

    buf = max(0.01, min(cross_limit_buffer 0.10, cross_limit_buffer_pct 0.20 * a))

which makes the buffer SHRINK as the ask falls -- 0.10 at a=0.50+, 0.064 at
0.32, 0.028 at 0.14. That is exactly backwards with respect to the mechanism:
a cheap ask means the venue has not yet repriced a fresh cross, so it is the
case where the book is about to move FURTHEST. The rule bids least where it
needs to bid most.

So this sweeps limit rules, not a single buffer:

    flat        a + c
    shipped     a + min(c, p*a)
    inverse     a + c*(1-a)          bigger buffer when cheap
    tofair      max(a + cmin, target)  bid toward a fixed price
    side        different c for Up and Down

WHY THIS MIGHT STILL FAIL, and the thing to watch: on the 7 live misses the
trades were worth +7.17/$1 at the price we bid and -0.73/$1 at the price
actually on offer. Buying a miss is only good if the fill price stays inside
the edge. A rule is therefore scored on GROWTH, never on fill rate -- a rule
that fills everything and pays for it is worse than the miss.

Fill model: the order rests `LAT` seconds after the decision (measured 2.7-5.2s
live) and fills if the ask trades at or below the limit before the wait expires.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings as S            # noqa: E402
from tools.cross_buffer_sweep import triggers, score   # noqa: E402
import tools.cross_buffer_sweep as CB                  # noqa: E402


def limit_for(rule, ask, side):
    kind = rule['kind']
    if kind == 'flat':
        buf = rule['c']
    elif kind == 'shipped':
        buf = max(0.01, min(rule['c'], rule['p'] * ask))
    elif kind == 'inverse':
        buf = max(0.01, rule['c'] * (1.0 - ask))
    elif kind == 'tofair':
        return min(max(round(ask + rule['cmin'], 2), rule['target']), 0.99)
    elif kind == 'side':
        buf = rule['cu'] if side == 'Up' else rule['cd']
    else:
        raise ValueError(kind)
    return min(round(ask + buf, 2), 0.99)


def run(trig, rule):
    day, won, px, decided = [], [], [], []
    miss_cheap = miss_rich = 0
    for d, _slug, side, ask, w, path in trig:
        lim = limit_for(rule, ask, side)
        hit = path[path <= lim]
        if len(hit) == 0:
            if ask < 0.45:
                miss_cheap += 1
            else:
                miss_rich += 1
            continue
        day.append(d); won.append(w); decided.append(ask)
        px.append(min(lim, float(hit[0])))
    t = pd.DataFrame({'day': day, 'won': won, 'ask': px, 'decided': decided})
    return t, miss_cheap, miss_rich


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    ap.add_argument('--latency', type=float, default=3.0)
    a = ap.parse_args()
    CB.LAT = a.latency
    q = pd.read_csv(f'{a.base}/quotes_arm.csv.gz')
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    q = q[q.slug.str.startswith('btc-updown-5m')]
    rounds = {t.slug: t for t in r.itertuples() if isinstance(t.winner, str)}
    trig = triggers(q, rounds)
    cheap = sum(1 for t in trig if t[3] < 0.45)
    print(f"triggers {len(trig)} ({cheap} cheap, ask<0.45)  "
          f"days {len({t[0] for t in trig})}  latency {a.latency}s\n")

    RULES = [
        ('SHIPPED  min(.10,.20a)', {'kind': 'shipped', 'c': 0.10, 'p': 0.20}),
        ('flat 0.06',             {'kind': 'flat', 'c': 0.06}),
        ('flat 0.10',             {'kind': 'flat', 'c': 0.10}),
        ('flat 0.15',             {'kind': 'flat', 'c': 0.15}),
        ('flat 0.20',             {'kind': 'flat', 'c': 0.20}),
        ('inverse .15(1-a)',      {'kind': 'inverse', 'c': 0.15}),
        ('inverse .25(1-a)',      {'kind': 'inverse', 'c': 0.25}),
        ('inverse .35(1-a)',      {'kind': 'inverse', 'c': 0.35}),
        ('inverse .50(1-a)',      {'kind': 'inverse', 'c': 0.50}),
        ('tofair >=0.45',         {'kind': 'tofair', 'cmin': 0.04, 'target': 0.45}),
        ('tofair >=0.55',         {'kind': 'tofair', 'cmin': 0.04, 'target': 0.55}),
        ('side Up.10/Dn.10',      {'kind': 'side', 'cu': 0.10, 'cd': 0.10}),
        ('side Up.15/Dn.06',      {'kind': 'side', 'cu': 0.15, 'cd': 0.06}),
        ('side Up.06/Dn.15',      {'kind': 'side', 'cu': 0.06, 'cd': 0.15}),
    ]
    print(f"  {'rule':<24}{'fills':>6}{'fill%':>7}{'missC':>7}{'win':>7}"
          f"{'mean px':>9}{'paid':>7}   growth / day-t / drop-best")
    base = None
    for lab, rule in RULES:
        t, mc, mr = run(trig, rule)
        s = score(t)
        if s is None:
            print(f"  {lab:<24}{len(t):>6}  (too few)")
            continue
        if base is None:
            base = s[0]
        paid = (t.ask - t.decided).mean()
        print(f"  {lab:<24}{len(t):>6}{100*len(t)/len(trig):>7.1f}{mc:>7}"
              f"{t.won.mean():>7.3f}{t.ask.mean():>9.3f}{paid:>+7.3f}   "
              f"{s[0]:+.3f} / {s[1]:+.2f} / {s[2]:+.3f}"
              + ("   <- SHIPPED" if lab.startswith('SHIPPED') else
                 f"   ({s[0]-base:+.3f} vs shipped)"))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
