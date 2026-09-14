"""Does our `fair` carry ANY information the venue's price does not?

This is the fork the model arm's future turns on. The arm is off because it has
no measurable margin (0.64971 won against a 0.64967 ask over 688 fills) and
because no knob rescued it. Two very different diagnoses remain:

  A. the signal is real but MISCALIBRATED -- it ranks outcomes better than the
     venue does, and we simply size/gate it wrongly. Fixable by recalibration.
  B. the signal carries no incremental information over the venue's own price.
     Then there is nothing to recalibrate and the arm needs a different input,
     not a better transform.

Discrimination, not calibration, decides this. AUC is invariant to any monotone
transform, so the fact that `quote` is an ASK (biased high by the spread) does
not flatter or penalise it here -- which is exactly why a raw Brier comparison
against an ask would be misleading.

CLUSTERING. Decision rows are written ~1/s inside a round, so thousands of them
share one price path and one outcome. Every interval here bootstraps over
ROUNDS, never over rows; treating rows as independent would shrink the CIs by
roughly the square root of the rows-per-round and make noise look decisive.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def auc(y, s):
    """Rank AUC, ties averaged."""
    y = np.asarray(y, float)
    s = np.asarray(s, float)
    n1, n0 = y.sum(), (1 - y).sum()
    if n1 == 0 or n0 == 0:
        return np.nan
    r = pd.Series(s).rank().to_numpy()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def boot_auc(df, col, n=400, seed=11):
    """AUC with a round-clustered bootstrap CI."""
    g = [x for _, x in df.groupby("slug", sort=False)]
    rng = np.random.default_rng(seed)
    out = np.empty(n)
    k = len(g)
    for i in range(n):
        s = pd.concat([g[j] for j in rng.integers(0, k, k)])
        out[i] = auc(s.won.to_numpy(), s[col].to_numpy())
    return auc(df.won.to_numpy(), df[col].to_numpy()), \
        float(np.nanpercentile(out, 2.5)), float(np.nanpercentile(out, 97.5))


def brier(y, p):
    return float(np.mean((np.asarray(p, float) - np.asarray(y, float)) ** 2))


def load(base):
    d = pd.read_csv(f"{base}/decisions_model.csv.gz")
    r = pd.read_csv(f"{base}/rounds.csv.gz")
    d = d[d.side.isin(["Up", "Down"])]
    d = d.merge(r[["slug", "winner"]], on="slug", how="inner")
    d = d[d.winner.notna()]
    d["won"] = (d.side == d.winner).astype(int)
    d = d[d.fair.notna() & d.quote.notna()]
    d = d[(d.quote > 0.01) & (d.quote < 0.99)]
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="data/recorded")
    ap.add_argument("--per-round", type=int, default=0,
                    help="thin to N evenly spaced rows per round (0 = all)")
    a = ap.parse_args()
    d = load(a.base)
    print(f"evaluations {len(d):,}  rounds {d.slug.nunique():,}  "
          f"base rate (side won) {d.won.mean():.4f}")

    if a.per_round:
        # cumcount, not groupby.apply: apply folds `slug` into the index and
        # the round-clustered bootstrap below then cannot group on it.
        d = d.sort_values(["slug", "ts_ms"]).copy()
        k = d.groupby("slug").cumcount()
        n = d.groupby("slug").slug.transform("size")
        step = (n // a.per_round).clip(lower=1)
        d = d[(k % step == 0) & (k // step < a.per_round)]
        print(f"thinned to {len(d):,} rows over {d.slug.nunique():,} rounds")

    print("\n=== DISCRIMINATION: can it rank winners? (round-clustered CI) ===")
    print(f"  {'predictor':<28}{'AUC':>8}{'95% CI':>20}")
    for col, lab in (("quote", "venue quote (the ask)"),
                     ("fair", "our fair"),
                     ("edge", "our edge = fair - quote")):
        m, lo, hi = boot_auc(d, col)
        print(f"  {lab:<28}{m:>8.4f}   [{lo:.4f}, {hi:.4f}]")

    print("\n=== by tau band (the model arm trades 60-210) ===")
    print(f"  {'tau':<14}{'n':>9}{'rounds':>8}{'AUC quote':>11}{'AUC fair':>10}"
          f"{'AUC edge':>10}{'  fair beats quote?':>20}")
    for lo, hi in ((0, 60), (60, 120), (120, 210), (210, 1e9)):
        x = d[(d.tau_s >= lo) & (d.tau_s < hi)]
        if len(x) < 200:
            continue
        aq, fq = auc(x.won, x.quote), auc(x.won, x.fair)
        ae = auc(x.won, x.edge)
        print(f"  {f'{lo}-{hi if hi<1e8 else 270}':<14}{len(x):>9,}{x.slug.nunique():>8}"
              f"{aq:>11.4f}{fq:>10.4f}{ae:>10.4f}{('YES' if fq > aq else 'no'):>20}")

    print("\n=== INCREMENTAL: does (fair - quote) predict (outcome - quote)? ===")
    # If the venue's price is already the best estimate, the residual outcome
    # is unpredictable from our disagreement with it and beta is ~0. beta>0
    # means our fair moves the right way; beta<0 means it is anti-informative.
    x = (d.fair - d.quote).to_numpy()
    y = (d.won - d.quote).to_numpy()
    g = [i for _, i in d.groupby("slug", sort=False).indices.items()]
    rng = np.random.default_rng(5)
    bs = np.empty(400)
    for i in range(400):
        idx = np.concatenate([g[j] for j in rng.integers(0, len(g), len(g))])
        xx, yy = x[idx], y[idx]
        bs[i] = np.polyfit(xx, yy, 1)[0] if xx.std() else np.nan
    beta = np.polyfit(x, y, 1)[0]
    lo, hi = np.nanpercentile(bs, 2.5), np.nanpercentile(bs, 97.5)
    print(f"  beta = {beta:+.4f}   95% CI [{lo:+.4f}, {hi:+.4f}]")
    print(f"  interpretation: {'ANTI-informative' if hi < 0 else 'informative' if lo > 0 else 'INDISTINGUISHABLE FROM ZERO'}")

    print("\n=== calibration of our fair, by decile (round-clustered n) ===")
    d = d.copy()
    d["b"] = pd.qcut(d.fair, 10, duplicates="drop")
    t = d.groupby("b", observed=True).agg(n=("won", "size"), rounds=("slug", "nunique"),
                                          fair=("fair", "mean"), quote=("quote", "mean"),
                                          actual=("won", "mean"))
    t["fair_err"] = t.fair - t.actual
    t["quote_err"] = t.quote - t.actual
    print(t.to_string(float_format=lambda v: f"{v:7.4f}"))
    print(f"\n  Brier  our fair {brier(d.won, d.fair):.5f}   "
          f"venue quote {brier(d.won, d.quote):.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
