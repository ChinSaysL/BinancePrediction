"""Does `fair` add information over `quote` ON THE POPULATION THE ARM TRADES?

The shutdown case rested partly on AUC(edge) = 0.4906 over 133k evaluations.
That statistic is wrong twice:

  * it is MARGINAL. `edge = fair - quote` is a difference, and its
    unconditional AUC conflates two levels. The conditional question -- does
    `fair` add information GIVEN `quote` -- is the incremental regression.
  * it is computed on the wrong POPULATION. Those 133k rows are every tau
    snapshot of every round. The arm only trades where `edge > hurdle` and
    `quote` is inside the band. Selection is on (fair, quote) and never on the
    outcome, so E[y | fair, quote] stays unbiased when restricted -- and the
    restricted set is the question the arm actually faces.

Both statistics are bootstrapped over ROUNDS. Rows inside a round share one
price path and one outcome; a per-row CI is ~sqrt(k) too narrow, which is the
error that made earlier per-market tables look decisive.

This is the cheap decisive test: if in-region AUC(edge) is 0.49 with a tight
CI, the arm is dead and no further live data is needed. If it is 0.55 with a
tight CI, the arm has an edge too small to resolve in 7 days rather than no
edge, and the question becomes whether ~36 days at minimum size is worth
buying.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def auc(y, s):
    y = np.asarray(y, float); s = np.asarray(s, float)
    n1, n0 = y.sum(), (1 - y).sum()
    if n1 == 0 or n0 == 0:
        return np.nan
    r = pd.Series(s).rank().to_numpy()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def beta_incr(d):
    """Slope of (outcome - quote) on (fair - quote): does our disagreement pay?"""
    x = (d.fair - d.quote).to_numpy()
    y = (d.won - d.quote).to_numpy()
    if x.std() == 0:
        return np.nan
    return float(np.polyfit(x, y, 1)[0])


def boot(d, fn, n=4000, seed=11):
    g = [x for _, x in d.groupby('slug', sort=False)]
    rng = np.random.default_rng(seed)
    k = len(g)
    out = np.empty(n)
    for i in range(n):
        out[i] = fn(pd.concat([g[j] for j in rng.integers(0, k, k)]))
    return float(np.nanpercentile(out, 2.5)), float(np.nanpercentile(out, 97.5))


def show(lab, d, n_boot):
    if len(d) < 50 or d.slug.nunique() < 10:
        print(f"  {lab:<34}{len(d):>7}  (too few)")
        return
    a_e, a_f, a_q = auc(d.won, d.edge), auc(d.won, d.fair), auc(d.won, d.quote)
    lo_e, hi_e = boot(d, lambda x: auc(x.won, x.edge), n_boot)
    b = beta_incr(d)
    lo_b, hi_b = boot(d, beta_incr, n_boot)
    verdict = ("CLEARS" if lo_e > 0.5 else
               "straddles" if hi_e > 0.5 else "below 0.5")
    print(f"  {lab:<34}{len(d):>7}{d.slug.nunique():>8}"
          f"{a_e:>9.4f}  [{lo_e:.4f},{hi_e:.4f}] {verdict:<10}"
          f"{b:>+9.4f}  [{lo_b:+.3f},{hi_b:+.3f}]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    ap.add_argument('--boot', type=int, default=2000)
    a = ap.parse_args()
    d = pd.read_csv(f'{a.base}/decisions_model.csv.gz')
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    d = d[d.side.isin(['Up', 'Down'])].merge(r[['slug', 'winner']], on='slug')
    d = d[d.winner.notna() & d.fair.notna() & d.quote.notna() & d.edge.notna()]
    d['won'] = (d.side == d.winner).astype(int)
    d = d[(d.quote > 0.01) & (d.quote < 0.99)]

    print(f"evaluations {len(d):,} over {d.slug.nunique():,} rounds\n")
    print(f"  {'population':<34}{'n':>7}{'rounds':>8}{'AUC(edge)':>9}"
          f"{'  95% CI (by round)':<22}{'b_fair|quote':>10}{'  95% CI':<20}")

    show("ALL evaluations (the old test)", d, a.boot)

    # the arm's own gate: edge over hurdle, quote inside the band
    lo, hi = 0.45, 0.80          # DEPLOYED band, not config.py's default
    g = d[(d.edge > d.hurdle) & (d.quote >= lo) & (d.quote <= hi)]
    show(f"GATE REGION (edge>hurdle, {lo}-{hi})", g, a.boot)

    show("  ...and tau 60-210 (deployed)",
         g[(g.tau_s >= 60) & (g.tau_s <= 210)], a.boot)

    print()
    for q0, q1 in ((0.45, 0.60), (0.60, 0.70), (0.70, 0.80)):
        show(f"  gate region, quote {q0:.2f}-{q1:.2f}",
             g[(g.quote >= q0) & (g.quote < q1)], a.boot)

    print("\n  edge quartiles inside the gate region (margin = win - quote):")
    if len(g) > 100:
        g = g.copy()
        g['q'] = pd.qcut(g.edge, 4, labels=False, duplicates='drop')
        print(f"    {'quartile':<10}{'n':>7}{'mean edge':>11}{'win':>8}"
              f"{'mean quote':>12}{'margin':>9}")
        for k, x in g.groupby('q'):
            print(f"    q{int(k)+1:<9}{len(x):>7}{x.edge.mean():>11.4f}"
                  f"{x.won.mean():>8.3f}{x.quote.mean():>12.3f}"
                  f"{x.won.mean()-x.quote.mean():>+9.4f}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
