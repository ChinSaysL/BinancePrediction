"""NUMBER ONE: fit the model's BIAS out-of-sample, then ask the only question
that matters -- after calibration, is the model more accurate than the venue?

WHY THIS IS THE DECISIVE TEST. `pm_forecast_edge_check.py` measured the PM
terminal distribution at Brier 0.246 against the venue's 0.112, and showed the
model is over-confident in every |displacement| bucket (+0.04 .. +0.25). A bias
is FIXABLE without any new information: fit a monotone map from p_model to the
empirical frequency, and the levels become honest. So the calibration map is
worth building -- but it can only create money if the model, once honest, is
still MORE ACCURATE than the price it is betting against. If the calibrated model
merely reproduces the venue's own probability, then `p_cal - ask - fee` is
negative everywhere by construction (the ask sits above the mid and the taker
pays a fee), and the whole idea is closed properly rather than by assertion.

WHAT IS MEASURED (all on HELD-OUT rounds, grouped by round so no round is in both
the fit and the test):
  1. Brier of the calibrated model vs the venue's mid.
  2. Two calibration forms, both fitted out-of-sample, both reported (picking the
     better one on the same data would be the usual fitting error):
       * ISOTONIC: a monotone binned map fitted by pool-adjacent-violators.
       * SHRINKAGE: p = fair_value(S, K, tau, sigma*k, J) with a single k found
         by minimising Brier on the training folds -- the model's own algebra
         with an honest sigma, so it keeps the shape the theory implies.
  3. The money test, one entry per round: is there ANY cohort where the
     calibrated model beats the ASK plus fee? Scored against the venue's own
     probability as the control (which by construction never signals).

Usage: PYTHONPATH=/root/BinancePrediction /root/analysis-venv/bin/python \
           tools/pm_calibration_map.py
"""
from __future__ import annotations

import math
import sys

import numpy as np
import pandas as pd

from tools.pm_forecast_edge_check import build, load
from btcpredict.venues.polymarket.pricing import (
    TAIL, edge_per_share, fair_value, fee_per_share)

DB = sys.argv[1] if len(sys.argv) > 1 else "/root/BinancePrediction/data/btcpredict.db"
FOLDS = 5
N_BINS = 16


# --------------------------------------------------------------------------- #
# calibration forms
# --------------------------------------------------------------------------- #
def _pava(y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Pool adjacent violators: the least-squares monotone fit."""
    y = y.astype(float).copy()
    w = w.astype(float).copy()
    i = 0
    while i < len(y) - 1:
        if y[i] <= y[i + 1]:
            i += 1
            continue
        tot = y[i] * w[i] + y[i + 1] * w[i + 1]
        wt = w[i] + w[i + 1]
        y[i] = tot / wt
        w[i] = wt
        y = np.delete(y, i + 1)
        w = np.delete(w, i + 1)
        i = max(0, i - 1)
    return y


def fit_isotonic(p: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(knots, values) of a monotone map p -> P(win), fitted by quantile bins."""
    qs = np.quantile(p, np.linspace(0.0, 1.0, N_BINS + 1))
    qs[0], qs[-1] = -np.inf, np.inf
    xs, ys, ws = [], [], []
    for i in range(N_BINS):
        m = (p >= qs[i]) & (p < qs[i + 1])
        if m.sum() < 5:
            continue
        xs.append(float(np.mean(p[m])))
        ys.append(float(np.mean(y[m])))
        ws.append(float(m.sum()))
    if len(xs) < 2:
        return np.array([0.0, 1.0]), np.array([0.0, 1.0])
    xs = np.array(xs)
    ys = _pava(np.array(ys), np.array(ws))
    # `_pava` may have pooled bins; re-expand to the knot positions equally
    if len(ys) != len(xs):
        ys = np.interp(np.linspace(0, 1, len(xs)), np.linspace(0, 1, len(ys)), ys)
    return xs, ys


def apply_isotonic(p: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    return np.clip(np.interp(p, xs, ys), 1e-6, 1 - 1e-6)


def fit_shrinkage(pr: pd.DataFrame, y: np.ndarray) -> float:
    """The sigma multiplier k minimising Brier through the DEPLOYED fair_value."""
    best, bk = None, 1.0
    for k in np.geomspace(0.5, 12.0, 25):
        p = np.array([fair_value(r.spot, r.strike, r.tau, r.sigma * k,
                                 window_log_int=(None if not np.isfinite(r.J)
                                                 else r.J), window=TAIL)[0]
                      for r in pr.itertuples()])
        b = float(np.mean((p - y) ** 2))
        if best is None or b < best:
            best, bk = b, float(k)
    return bk


def apply_shrinkage(pr: pd.DataFrame, k: float) -> np.ndarray:
    return np.array([fair_value(r.spot, r.strike, r.tau, r.sigma * k,
                                window_log_int=(None if not np.isfinite(r.J)
                                                else r.J), window=TAIL)[0]
                     for r in pr.itertuples()])


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


# --------------------------------------------------------------------------- #
def main() -> None:
    df = load(DB)
    if df.empty:
        print("no PM-era rounds"); return
    pr = build(df, "bot")
    if pr.empty:
        print("no usable rows"); return
    pr = pr.reset_index(drop=True)
    # folds are grouped BY ROUND so no round is fitted and tested at once
    rounds = pr.slug.drop_duplicates().to_numpy()
    rng = np.random.default_rng(7)
    fold_of = {s: i % FOLDS for i, s in
               enumerate(rng.permutation(rounds))}
    pr["fold"] = pr.slug.map(fold_of)
    y = pr.won_up.to_numpy()

    print(f"=== {pr.slug.nunique()} PM-era 5m rounds, {len(pr)} snapshots, "
          f"{FOLDS}-fold by round, calibration fitted OUT-OF-SAMPLE ===")
    print(f"    raw model Brier {brier(pr.p_model.to_numpy(), y):.4f} | "
          f"venue mid {brier(pr.mid_up.fillna(0.5).to_numpy(), y):.4f} | "
          f"coin flip 0.2500")

    oos_iso = np.full(len(pr), np.nan)
    oos_shr = np.full(len(pr), np.nan)
    ks = []
    for f in range(FOLDS):
        tr = (pr.fold != f).to_numpy()
        te = (pr.fold == f).to_numpy()
        ptr, ytr = pr.p_model.to_numpy()[tr], y[tr]
        xs, ys = fit_isotonic(ptr, ytr)
        oos_iso[te] = apply_isotonic(pr.p_model.to_numpy()[te], xs, ys)
        k = fit_shrinkage(pr[tr], ytr)
        ks.append(k)
        oos_shr[te] = apply_shrinkage(pr[te], k)
    pr["p_iso"], pr["p_shr"] = oos_iso, oos_shr

    print(f"\n--- A. HELD-OUT CALIBRATION (each round scored by a map that never "
          f"saw it) ---")
    print(f"    model, raw                 {brier(pr.p_model.to_numpy(), y):.4f}")
    print(f"    model, ISOTONIC calibrated {brier(pr.p_iso, y):.4f}")
    print(f"    model, SHRINKAGE calibrated{brier(pr.p_shr, y):.4f} "
          f"(k per fold {['%.1f' % k for k in ks]})")
    print(f"    VENUE mid                  {brier(pr.mid_up.fillna(0.5).to_numpy(), y):.4f}")
    print(f"    coin flip                  0.2500")
    print("    -> a calibrated model that still loses this column cannot bet "
          "against the price.")

    print(f"\n--- B. RELIABILITY OF THE CALIBRATED MODEL (held-out) ---")
    print(f"    {'p_bucket':>10} {'n':>5} {'iso':>7} {'shrink':>8} {'venue':>7} "
          f"{'realised':>9}")
    for lo, hi in [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]:
        m = (pr.p_iso >= lo) & (pr.p_iso < hi)
        if m.sum() < 15:
            continue
        print(f"    {lo:.1f}-{hi:.1f}    {int(m.sum()):>5d} {pr.p_iso[m].mean():>7.3f} "
              f"{pr.p_shr[m].mean():>8.3f} {pr.mid_up[m].mean():>7.3f} "
              f"{y[m].mean():>9.1%}")

    print(f"\n--- C. THE MONEY TEST: can the CALIBRATED model beat the ask+fee? ---")
    print("    one entry per round, restricted to the arm's own price band "
          "(ask 0.10-0.90);\n    unrestricted, the argmax always picks a 6c "
          "longshot and the test measures the\n    bucket this repo already "
          "measured to lose, not the model.")
    for col, name in (("p_model", "MODEL RAW"), ("p_iso", "ISOTONIC"),
                      ("p_shr", "SHRINKAGE"), ("mid_up", "VENUE (control)")):
        rows = []
        for slug, g in pr.groupby("slug", sort=False):
            g = g[np.isfinite(g[col].to_numpy())]
            if g.empty:
                continue
            pu = g[col].to_numpy()
            au, ad = g.up_ask.to_numpy(), g.dn_ask.to_numpy()
            e_up = pu - au - np.array([fee_per_share(a) for a in au])
            e_dn = (1 - pu) - ad - np.array([fee_per_share(a) for a in ad])
            band = (((au >= 0.10) & (au <= 0.90)), ((ad >= 0.10) & (ad <= 0.90)))
            e_up = np.where(band[0], e_up, -9.0)
            e_dn = np.where(band[1], e_dn, -9.0)
            i = int(np.argmax(np.maximum(e_up, e_dn)))
            up = e_up[i] >= e_dn[i]
            if max(e_up[i], e_dn[i]) < -8.0:
                continue
            rows.append(dict(edge=float(max(e_up[i], e_dn[i])),
                             ask=float(au[i] if up else ad[i]),
                             won=bool((g.won_up.iloc[i] == 1.0) if up
                                      else (g.won_up.iloc[i] == 0.0)),
                             day=str(g.day.iloc[0])))
        d = pd.DataFrame(rows)
        if d.empty:
            continue
        d["pnl"] = np.where(
            d.won, (1.0 - np.array([fee_per_share(a) for a in d.ask])) / d.ask - 1.0, -1.0)
        d["per"] = (d.won.astype(float) - d.ask
                    - np.array([fee_per_share(a) for a in d.ask]))
        print(f"\n    {name}: rounds {len(d)} | claimed edge median {d.edge.median():+.4f} "
              f"| REALISED per-share {d.per.mean():+.4f}")
        for thr in (-1.0, 0.0, 0.02, 0.05):
            s = d[d.edge >= thr] if thr > -1 else d
            if len(s) < 8:
                print(f"      edge >= {thr:+.2f}  n={len(s)} (thin)")
                continue
            dm = [s.pnl[s.day.values == x].mean() for x in s.day.unique()]
            print(f"      edge >= {thr:+.2f}  n={len(s):4d} ask {s.ask.mean():.3f} "
                  f"win {s.won.mean():5.1%} EV/$1 {s.pnl.mean():+.3f} "
                  f"per-share {s.per.mean():+.4f} "
                  f"day-t {np.mean(dm) / (np.std(dm) / math.sqrt(len(dm))) if np.std(dm) else 0.0:+.2f}")
    print("\n--- D. IS THE CLAIMED EDGE INFORMATIVE AT ALL? (deciles, held-out) ---")
    print("    A gate needs the ORDER of the claimed edge to match the order of the")
    print("    realised outcome. `claimed` is the model's p - ask - fee; `realised`")
    print("    is what the trade actually paid per share.")
    rows = []
    for slug, g in pr.groupby("slug", sort=False):
        pu = g.p_shr.to_numpy()
        au, ad = g.up_ask.to_numpy(), g.dn_ask.to_numpy()
        e_up = pu - au - np.array([fee_per_share(a) for a in au])
        e_dn = (1 - pu) - ad - np.array([fee_per_share(a) for a in ad])
        for e, ask_arr, is_up in ((e_up, au, True), (e_dn, ad, False)):
            for k in range(len(g)):
                if not (0.10 <= ask_arr[k] <= 0.90):
                    continue
                won = ((g.won_up.iloc[k] == 1.0) if is_up
                       else (g.won_up.iloc[k] == 0.0))
                rows.append(dict(claimed=float(e[k]), ask=float(ask_arr[k]),
                                 per=float((1.0 if won else 0.0) - ask_arr[k]
                                           - fee_per_share(ask_arr[k]))))
    dd = pd.DataFrame(rows)
    if len(dd) >= 50:
        dd["dec"] = pd.qcut(dd.claimed, 10, labels=False, duplicates="drop")
        gg = dd.groupby("dec").agg(n=("claimed", "size"), claimed=("claimed", "mean"),
                                   realised=("per", "mean"), ask=("ask", "mean"))
        print(gg.round(4).to_string())
        c = float(np.corrcoef(dd.claimed, dd.per)[0, 1])
        print(f"    corr(claimed edge, realised per-share) = {c:+.3f}  "
              f"(A GATE NEEDS THIS POSITIVE AND LARGE)")


if __name__ == "__main__":
    main()
