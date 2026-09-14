"""Predict the ROUND at its open, from momentum, candle shape and reaction zones.

Every other arm tested today enters mid-round, where the venue's quote already
encodes where spot sits relative to the strike -- and that quote beats our fair
at every tau. At the OPEN the situation is structurally different:

    strike == spot, so the digital says exactly 0.500 and has NO information.
    The venue quotes both sides near 0.50 because it does not know either.

So at the open, momentum is not competing against a better model. It is the
only thing on the table. That makes it the one place a weak signal could still
be tradeable, and it is the highest-frequency entry that exists -- one per
round, 288 rounds a day per 5m family, against the cross arm's ~24.

Features, all causal at the decision instant and all already in the corpus:
  * MOMENTUM      mom_{1,2,5,10,20,30,60,120,300}_bps, mom_z_{10,30,60,120},
                  accel_5_20, last_ret_bps, ret_streak
  * CANDLE SHAPE  wick_up_bps, wick_dn_bps, range_bps, range_pos, efficiency,
                  path_over_net, max_exc_bps, min_exc_bps, ssl
  * REACTION ZONE dist_round100_bps (distance to a round-number level),
                  vwap_dev_bps, dist_to_max_bps, dist_to_min_bps
  * FLOW          cvd, cvd_ratio, taker_buy_ratio, vol_surge, trades_per_s

Leakage control is the same as the tau 60-210 search: settlement-derived columns
dropped, absolute price levels dropped (a tree uses them to memorise the day),
expanding forward folds BY DAY, never random.

The bar is not AUC > 0.5. It is edge > the ask at the open plus slippage --
reported directly, because a signal that ranks well and cannot clear ~0.52 is
not a trade.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LEAK = ['final_cl', 'close_end_tape', 'true_delta_bps', 'y_tape', 'y_up']
IDENT = ['window_id', 'start_ms', 'day', 'px', 'strike_cl', 'strike_tape',
         'open_tape']


def auc(y, s):
    y = np.asarray(y, float); s = np.asarray(s, float)
    n1, n0 = y.sum(), (1 - y).sum()
    if n1 == 0 or n0 == 0:
        return np.nan
    r = pd.Series(s).rank().to_numpy()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--folds', type=int, default=5)
    a = ap.parse_args()
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    f = pd.read_parquet('data/btc5m_ml.parquet')
    tmax = f.tau_s.max()
    f = f[f.tau_s == tmax].copy()          # the earliest snapshot of each round
    f = f[f.y_up.notna()]
    feats = [c for c in f.columns if c not in LEAK + IDENT]
    days = np.sort(f.day.unique())
    print(f"rounds {len(f):,}  days {len(days)}  snapshot at tau {tmax:.0f}s "
          f"({300 - tmax:.0f}s into the round)")
    print(f"features {len(feats)}   base rate P(up) = {f.y_up.mean():.4f}")
    print(f"positional baseline here: delta_bps median "
          f"{f.delta_bps.median():+.3f} bps -- the digital has no view\n")

    cuts = np.array_split(days, a.folds + 1)
    Y, PG, PL, D = [], [], [], []
    print(f"  {'fold':<6}{'train d':>9}{'test n':>8}{'AUC logistic':>14}{'AUC GBM':>10}")
    for k in range(a.folds):
        tr = f[f.day.isin(np.concatenate(cuts[:k + 1]))]
        te = f[f.day.isin(cuts[k + 1])]
        if len(te) < 50 or tr.y_up.nunique() < 2:
            continue
        Xtr, Xte = tr[feats].to_numpy('float32'), te[feats].to_numpy('float32')
        ytr, yte = tr.y_up.to_numpy(int), te.y_up.to_numpy(int)
        g = HistGradientBoostingClassifier(max_iter=250, learning_rate=0.05,
                                           max_depth=3, l2_regularization=1.0,
                                           early_stopping=True, random_state=0)
        g.fit(Xtr, ytr); pg = g.predict_proba(Xte)[:, 1]
        sc = StandardScaler()
        lr = LogisticRegression(max_iter=2000, C=0.05)
        lr.fit(np.nan_to_num(sc.fit_transform(np.nan_to_num(Xtr))), ytr)
        pl = lr.predict_proba(np.nan_to_num(sc.transform(np.nan_to_num(Xte))))[:, 1]
        Y.append(yte); PG.append(pg); PL.append(pl); D.append(te.day.values)
        print(f"  {k:<6}{len(np.concatenate(cuts[:k+1])):>9}{len(te):>8,}"
              f"{auc(yte, pl):>14.4f}{auc(yte, pg):>10.4f}")
    if not Y:
        print("no usable folds")
        return 1
    y = np.concatenate(Y); pg = np.concatenate(PG)
    pl = np.concatenate(PL); d = np.concatenate(D)
    print(f"\n=== pooled out-of-sample ({len(y):,} rounds, "
          f"{len(np.unique(d))} test days) ===")
    print(f"  logistic AUC {auc(y, pl):.4f}    GBM AUC {auc(y, pg):.4f}")

    print(f"\n=== is it TRADEABLE? accuracy in the tails of the prediction ===")
    print(f"  {'model':<10}{'selection':<22}{'n':>7}{'hit rate':>10}"
          f"{'  vs 0.52 ask + 0.004 slip':<28}")
    for lab, p in (('logistic', pl), ('GBM', pg)):
        for q0 in (0.5, 0.6, 0.7):
            side = np.where(p >= 0.5, 1, 0)
            conf = np.where(p >= 0.5, p, 1 - p)
            m = conf >= q0
            if m.sum() < 50:
                continue
            hit = (side[m] == y[m]).mean()
            print(f"  {lab:<10}{f'confidence >= {q0:.2f}':<22}{int(m.sum()):>7}"
                  f"{hit:>10.4f}{('  CLEARS' if hit > 0.524 else '  below'):<28}")
    print("\n  (break-even is the ask at the open plus slippage; the venue quotes")
    print("   ~0.50-0.52 there, so ~0.524 is the bar a hit rate must beat)")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
