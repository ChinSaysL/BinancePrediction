"""Structural arbitrage on this venue: measured, and why it cannot exist.

Two strategies were proposed:

  1. SPLIT-AND-SELL   deposit $1.00, mint 1 UP + 1 DOWN, rest maker sells on
                      both sides, capture the spread with no directional risk.
  2. DISCOUNT MERGE   when the combined ask dips under $1.00, take both sides
                      and MERGE the pair back into a dollar.

Neither primitive exists here -- the venue exposes eleven endpoints and none is
split, merge or mint. That alone is not the answer, because merge is only a way
to collect the dollar EARLY: holding 1 UP + 1 DOWN to settlement pays exactly
$1.00 for free. So the real question is whether the combined ask ever falls
below a dollar.

It cannot, and the reason is structural. UP and DOWN are not two books. They
are ONE book, mirrored:

    down_ask == 1 - up_bid   to within 1e-4, in 98.9% of 366,148 snapshots

so the combined ask is `1 + spread` by construction, and the combined bid is
`1 - spread`. The corpus min over every row where the mirror holds is exactly
1.0000. The 403 sub-dollar snapshots that do appear are 100.0% mirror BREAKS --
one leg stale in the snapshot -- and 96% of them survive a single poll tick,
with a maximum life of 1.0 second across seven days.

The same identity dissolves strategy 1. Buying DOWN at its bid IS selling UP at
its ask (`down_bid == 1 - up_ask`), so "buy the pair at the bid" is not a
hedged pair at all -- it is a flat book and a captured spread, i.e. plain
market making, already measured at -4.7pts of adverse selection. The +0.0072
mean this script reports for the pair-at-bid is the 1-cent spread minus the
fee, not a new edge.

Run it to reproduce all three: the mirror, the combined-ask floor, and the
sub-dollar snapshots' provenance.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FEE = 200 / 1e4


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    a = ap.parse_args()
    q = pd.read_csv(f'{a.base}/quotes_arm.csv.gz')
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    win = {t.slug: t.winner for t in r.itertuples()}

    # --- the structural fact everything else follows from -----------------
    brk = (q.down_ask - (1 - q.up_bid)).abs() > 1e-4
    print("is this one mirrored book, or two independent books?")
    for lab, x, y in (("down_ask vs 1 - up_bid", q.down_ask, 1 - q.up_bid),
                      ("down_bid vs 1 - up_ask", q.down_bid, 1 - q.up_ask)):
        d = (x - y).to_numpy()
        print(f"  {lab:<26} |diff| < 1e-4 in "
              f"{100 * (np.abs(d) < 1e-4).mean():.1f}% of rows")
    held = q[~brk]
    print(f"  where the mirror holds ({len(held):,} rows) the combined ask is "
          f"1 + spread by construction:")
    print(f"    min {(held.up_ask + held.down_ask).min():.4f}   "
          f"median {(held.up_ask + held.down_ask).median():.4f}\n")

    q['pref'] = q.slug.str.rsplit('-', n=1).str[0]
    both = q[q.up_ask.notna() & q.down_ask.notna()].copy()
    both['pair'] = both.up_ask + both.down_ask
    print(f"quote rows {len(q):,}   with BOTH asks quoted {len(both):,} "
          f"({100*len(both)/len(q):.1f}%)\n")

    print("combined ask (up_ask + down_ask), per family")
    print(f"  {'family':<18}{'n':>8}{'p1':>8}{'p5':>8}{'median':>9}{'p95':>8}"
          f"{'min':>8}{'<1.00':>8}{'<0.99':>8}")
    for p, g in both.groupby('pref'):
        v = g.pair.to_numpy()
        print(f"  {p:<18}{len(v):>8,}{np.percentile(v,1):>8.4f}"
              f"{np.percentile(v,5):>8.4f}{np.median(v):>9.4f}"
              f"{np.percentile(v,95):>8.4f}{v.min():>8.4f}"
              f"{(v<1.0).sum():>8,}{(v<0.99).sum():>8,}")
    v = both.pair.to_numpy()
    print(f"  {'ALL':<18}{len(v):>8,}{np.percentile(v,1):>8.4f}"
          f"{np.percentile(v,5):>8.4f}{np.median(v):>9.4f}"
          f"{np.percentile(v,95):>8.4f}{v.min():>8.4f}"
          f"{(v<1.0).sum():>8,}{(v<0.99).sum():>8,}")

    # --- price every sub-dollar pair that exists, after the fee -------------
    sub = both[both.pair < 1.0].copy()
    print(f"\n=== the {len(sub):,} snapshots quoted under $1.00, priced ===")
    if len(sub) == 0:
        print("  none. there is no discount pair to buy on this venue.")
        return 0
    sub['winner'] = sub.slug.map(win)
    sub = sub[sub.winner.isin(['Up', 'Down'])]
    w_ask = np.where(sub.winner.to_numpy() == 'Up',
                     sub.up_ask.to_numpy(), sub.down_ask.to_numpy())
    net = 1.0 - sub.pair.to_numpy() - FEE * (1.0 - w_ask)
    print(f"  of these, {100 * brk.reindex(sub.index).mean():.1f}% are rows where "
          f"the MIRROR IS BROKEN -- one leg stale, not a real discount")
    print(f"  gross edge (1 - pair):  median {np.median(1-sub.pair):+.4f}   "
          f"best {np.max(1-sub.pair):+.4f}")
    print(f"  net of the 200bp fee:   median {np.median(net):+.4f}   "
          f"best {np.max(net):+.4f}")
    print(f"  snapshots still positive after the fee: "
          f"{(net > 0).sum():,} of {len(net):,}")
    print(f"  distinct rounds offering one: {sub[net>0].slug.nunique() if (net>0).any() else 0}"
          f" of {both.slug.nunique():,}")
    if (net > 0).any():
        s = sub[net > 0]
        print(f"\n  {'family':<18}{'rounds':>8}{'snaps':>8}{'median net':>12}"
              f"{'median tau':>12}")
        for p, g in s.groupby('pref'):
            m = net[np.isin(sub.index, g.index)]
            print(f"  {p:<18}{g.slug.nunique():>8}{len(g):>8}"
                  f"{np.median(m):>+12.4f}{g.tau_s.median():>12.0f}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
