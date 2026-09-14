"""STAGE 1: does ANY feature beat the digital at predicting the outcome?

The model arm is off because its edge (+0.016 to +0.060) is smaller than its
slippage (+0.088), and because the quantity it trades -- its disagreement with
the venue -- ranks outcomes at AUC 0.4906, i.e. noise. That rules out every
transform of (spot, strike, tau, sigma), since the venue prices those better.

The only remaining path is DIFFERENT INPUTS. btc5m_ml.parquet carries 57
causally-evolving features per (window, tau) snapshot -- order flow, path
shape, momentum at nine horizons -- none of which enter `fair`.

This asks the cheap, well-powered half of the question: on 31 days of real
Chainlink strikes and settlements, can a model over those features rank
outcomes better than the digital does? If not, stage 2 (does it also beat the
VENUE's price, which needs quotes and so is confined to 5-7 days) is moot.

LEAKAGE CONTROL, which decides whether any of this means anything:
  * dropped outright: final_cl, close_end_tape, true_delta_bps, y_tape -- all
    constant within a window and all functions of the settlement.
  * dropped as non-stationary IDENTIFIERS: px, strike_cl, strike_tape,
    open_tape. Absolute BTC levels are unique to a day, so a tree can use them
    to memorise which day a row came from; that inflates in-fold accuracy and
    transfers to nothing. Only bps / ratio / z-scored features survive.
  * split FORWARD BY DAY, expanding window, never random. Rows inside a window
    share a price path, and windows inside a day share a regime.

The baseline is the digital's own argument `z_tau`. AUC is invariant to any
monotone transform, and the digital fair is Phi(z), so AUC(z) IS the digital's
discrimination -- no sigma reconstruction needed.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LEAK = ['final_cl', 'close_end_tape', 'true_delta_bps', 'y_tape', 'y_up',
        'z_base']
IDENT = ['window_id', 'start_ms', 'day', 'px', 'strike_cl', 'strike_tape',
         'open_tape']


def auc(y, s):
    y = np.asarray(y, float)
    s = np.asarray(s, float)
    n1, n0 = y.sum(), (1 - y).sum()
    if n1 == 0 or n0 == 0:
        return np.nan
    r = pd.Series(s).rank().to_numpy()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--tau-lo', type=float, default=60.0)
    ap.add_argument('--tau-hi', type=float, default=210.0)
    ap.add_argument('--folds', type=int, default=5)
    ap.add_argument('--all-tau', action='store_true')
    a = ap.parse_args()

    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    f = pd.read_parquet('data/btc5m_ml.parquet')
    if not a.all_tau:
        f = f[(f.tau_s >= a.tau_lo) & (f.tau_s <= a.tau_hi)]
    f = f[f.y_up.notna() & f.z_tau.notna()].copy()
    # BASELINE CORRECTION. `z_tau` normalises the CHAINLINK delta, and in this
    # corpus (not live) strike_cl sits a median -4.0 bps from the reconstructed
    # tape strike -- a construction artefact, verified absent on the bot's own
    # recorded book (basis median +0.000, mean +0.017, n=1,938 rounds). Scored
    # against z_tau, any model wins simply by rediscovering `delta_bps`, which
    # is what the first run did (permutation importance 0.276 vs <=0.011 for
    # everything else). The honest baseline already contains that, so the
    # search has to find something BEYOND position-over-vol.
    sg = (f.sigma_ref_bps * np.sqrt(f.tau_s / 60.0)).replace(0, np.nan)
    f['z_base'] = (f.delta_bps / sg).fillna(0.0)
    feats = [c for c in f.columns if c not in LEAK + IDENT]
    days = np.sort(f.day.unique())
    print(f"rows {len(f):,}  windows {f.window_id.nunique():,}  days {len(days)}"
          f"  tau {'ALL' if a.all_tau else f'{a.tau_lo:.0f}-{a.tau_hi:.0f}'}")
    print(f"features {len(feats)} | dropped {len(LEAK)-1} leakers + "
          f"{len(IDENT)} identifiers/levels")
    print(f"base rate P(up) = {f.y_up.mean():.4f}\n")

    # Expanding forward folds over DAYS.
    cuts = np.array_split(days, a.folds + 1)
    rows = []
    print(f"  {'fold':<6}{'train d':>9}{'test d':>8}{'test n':>9}"
          f"{'baseline':>10}{'logistic':>10}{'GBM':>9}{'GBM-base':>13}")
    for k in range(a.folds):
        tr_days = np.concatenate(cuts[:k + 1])
        te_days = cuts[k + 1]
        tr, te = f[f.day.isin(tr_days)], f[f.day.isin(te_days)]
        if len(te) < 200 or tr.y_up.nunique() < 2:
            continue
        Xtr = tr[feats].to_numpy('float32')
        Xte = te[feats].to_numpy('float32')
        ytr, yte = tr.y_up.to_numpy(int), te.y_up.to_numpy(int)

        g = HistGradientBoostingClassifier(
            max_iter=300, learning_rate=0.05, max_depth=4,
            l2_regularization=1.0, early_stopping=True,
            validation_fraction=0.15, random_state=0)
        g.fit(Xtr, ytr)
        pg = g.predict_proba(Xte)[:, 1]

        sc = StandardScaler()
        Ztr = np.nan_to_num(sc.fit_transform(np.nan_to_num(Xtr)))
        Zte = np.nan_to_num(sc.transform(np.nan_to_num(Xte)))
        lr = LogisticRegression(max_iter=2000, C=0.1)
        lr.fit(Ztr, ytr)
        pl = lr.predict_proba(Zte)[:, 1]

        ad, al, ag = auc(yte, te.z_base), auc(yte, pl), auc(yte, pg)
        rows.append((te.day.values, yte, te.z_base.to_numpy(), pl, pg))
        print(f"  {k:<6}{len(tr_days):>9}{len(te_days):>8}{len(te):>9,}"
              f"{ad:>10.4f}{al:>10.4f}{ag:>9.4f}{ag-ad:>+13.4f}")

    if not rows:
        print("no usable folds")
        return 1

    day = np.concatenate([r[0] for r in rows])
    y = np.concatenate([r[1] for r in rows])
    zd = np.concatenate([r[2] for r in rows])
    pl = np.concatenate([r[3] for r in rows])
    pg = np.concatenate([r[4] for r in rows])
    print(f"\n=== pooled out-of-sample ({len(y):,} rows, "
          f"{len(np.unique(day))} test days) ===")
    print(f"  baseline z(tape)  AUC {auc(y, zd):.4f}")
    print(f"  logistic          AUC {auc(y, pl):.4f}")
    print(f"  GBM               AUC {auc(y, pg):.4f}")

    # Paired per-day comparison: the honest test. One AUC per test day, then a
    # t on the differences -- days are the independent unit, not rows.
    d = pd.DataFrame({'day': day, 'y': y, 'zd': zd, 'pg': pg, 'pl': pl})
    per = []
    for dd, g_ in d.groupby('day'):
        if g_.y.nunique() < 2 or len(g_) < 50:
            continue
        per.append((auc(g_.y, g_.zd), auc(g_.y, g_.pl), auc(g_.y, g_.pg)))
    per = np.array(per)
    print(f"\n=== paired by DAY (n={len(per)} test days) ===")
    for i, lab in ((1, 'logistic'), (2, 'GBM')):
        diff = per[:, i] - per[:, 0]
        sd = diff.std(ddof=1)
        t = diff.mean() / (sd / np.sqrt(len(diff))) if sd else np.nan
        print(f"  {lab:<10} mean AUC {per[:, i].mean():.4f} vs digital "
              f"{per[:, 0].mean():.4f}   diff {diff.mean():+.4f}  t={t:+.2f}"
              f"  beat on {int((diff > 0).sum())}/{len(diff)} days")

    g = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05,
                                       max_depth=4, random_state=0)
    g.fit(f[feats].to_numpy('float32'), f.y_up.to_numpy(int))
    try:
        from sklearn.inspection import permutation_importance
        sub = f.sample(min(8000, len(f)), random_state=1)
        r = permutation_importance(
            g, sub[feats].to_numpy('float32'), sub.y_up.to_numpy(int),
            n_repeats=3, random_state=0, scoring='roc_auc')
        imp = pd.Series(r.importances_mean, index=feats).sort_values(ascending=False)
        print("\n=== permutation importance, top 12 (IN-SAMPLE -- direction only) ===")
        for k, v in imp.head(12).items():
            print(f"  {k:<22}{v:+.5f}")
    except Exception as e:                      # pragma: no cover
        print(f"(importance unavailable: {e})")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
