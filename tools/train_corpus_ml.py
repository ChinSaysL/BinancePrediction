"""Train a model on the 1-second tape and try to beat the digital baseline.

The question is narrow on purpose. The market for these windows is priced by
people and bots watching the same tape, and the closed form

    P(Up) = Phi( D / (sigma * sqrt(tau)) )        D = tape price vs the Chainlink strike

is available to everyone. Anything we build must beat THAT, not 0.5. So this
script fits three things on identical folds and prints them side by side:

  * digital   -- the closed form, no fitting at all
  * gbm       -- xgboost on the same inputs plus the tape microstructure
  * remaining -- a regression on the part the closed form cannot see: the move
                 still to come, normalised to sigma*sqrt(tau)

Protocol
--------
Days are sorted; the first 70% train, the last 30% test. Early stopping uses a
later slice of the TRAIN days, never the test days. Rows from one window are
near-duplicates, so every pooled number is also reported grouped by day, and the
headline metrics are computed on windows, not rows.

Leakage guard: strike levels, the final Chainlink price, the settlement label and
the tape's own end price are excluded from the feature set. `cl_delta_bps` is
allowed because a trader can read the Chainlink stream live; it is the quantity
the market settles on.
"""
from __future__ import annotations

import argparse
import math
import os

import numpy as np
import pandas as pd

DROP = {
    "window_id", "start_ms", "day", "strike_tape", "strike_cl", "final_cl",
    "close_end_tape", "open_tape", "px", "true_delta_bps", "y_up", "y_tape",
}

BANDS = [(0.0, 0.25), (0.25, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 99.0)]


def phi(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.vectorize(math.erf)(np.asarray(x) / math.sqrt(2.0)))


def logloss(y, p, eps=1e-9):
    p = np.clip(p, eps, 1 - eps)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier(y, p):
    return float(np.mean((p - y) ** 2))


def auc(y, p):
    y = np.asarray(y)
    if y.min() == y.max():
        return float("nan")
    order = np.argsort(p)
    ranks = np.empty(len(p), float)
    ranks[order] = np.arange(1, len(p) + 1)
    n1, n0 = int(y.sum()), int((1 - y).sum())
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def day_t(diff: pd.Series, days: pd.Series) -> tuple[float, float]:
    d = pd.DataFrame({"d": diff.values, "day": days.values})
    g = d.groupby("day")["d"].mean()
    if len(g) < 2 or g.std(ddof=1) == 0:
        return float("nan"), float("nan")
    return float(g.mean()), float(g.mean() / (g.std(ddof=1) / math.sqrt(len(g))))


def battery(v: np.ndarray, days: np.ndarray) -> str:
    """The four checks every earlier false winner had to survive."""
    v = np.asarray(v, float)
    if len(v) < 20:
        return "n<20"
    sd = v.std(ddof=1)
    t = v.mean() / (sd / math.sqrt(len(v))) if sd > 0 else float("nan")
    d = pd.DataFrame({"v": v, "day": np.asarray(days)})
    g = d.groupby("day")["v"].mean()
    td = (g.mean() / (g.std(ddof=1) / math.sqrt(len(g)))
          if len(g) > 1 and g.std(ddof=1) > 0 else float("nan"))
    h = len(v) // 2
    drop1 = d[d["day"] != g.idxmax()]["v"].mean()
    return (f"t {t:+5.2f} day-t {td:+5.2f} h1 {v[:h].mean():+.4f} "
            f"h2 {v[h:].mean():+.4f} drop1 {drop1:+.4f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/btc5m_ml.parquet")
    ap.add_argument("--prices", default="")
    ap.add_argument("--taus", type=int, nargs="+", default=None,
                    help="restrict to these tau values")
    ap.add_argument("--train-frac", type=float, default=0.7)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--rounds", type=int, default=4000)
    ap.add_argument("--depth", type=int, default=5)
    ap.add_argument("--lr", type=float, default=0.03)
    args = ap.parse_args()

    df = pd.read_parquet(args.data)
    if args.taus:
        df = df[df["tau_s"].isin(args.taus)].copy()
    df = df[np.isfinite(df["cl_delta_bps"]) & np.isfinite(df["sigma_ref_bps"])]
    df = df[df["sigma_ref_bps"] > 0].reset_index(drop=True)
    print(f"rows {len(df):,}  windows {df['window_id'].nunique():,}  "
          f"days {df['day'].nunique()}  taus {sorted(df['tau_s'].unique())}")

    df["z"] = df["cl_delta_bps"] / (df["sigma_ref_bps"] * np.sqrt(df["tau_s"]))
    df["p_dig"] = phi(df["z"].to_numpy())
    df["remain_bps"] = df["true_delta_bps"] - df["cl_delta_bps"]
    df["remain_z"] = df["remain_bps"] / (df["sigma_ref_bps"] * np.sqrt(df["tau_s"]))
    print(f"basis (tape open vs Chainlink strike): mean {df['basis_bps'].mean():+.3f} bps  "
          f"median {df['basis_bps'].median():+.3f}  sd {df['basis_bps'].std():.3f}  "
          f"mean |basis| {df['basis_bps'].abs().mean():.3f} bps")
    print(f"sigma_ref: median {df['sigma_ref_bps'].median():.3f} bps/s  "
          f"=> per-day {df['sigma_ref_bps'].median() * math.sqrt(86400) / 1e4 * 100:.3f}%  "
          f"(BTC realised is ~2-3%/day, so the 1s closes are over-smoothed)")

    feats = [c for c in df.columns if c not in DROP and
             c not in {"z", "p_dig", "remain_bps", "remain_z"} and
             df[c].dtype.kind in "fiu"]
    print(f"features: {len(feats)}")

    days = np.sort(df["day"].unique())
    cut = days[int(len(days) * args.train_frac)]
    tr = df[df["day"] < cut]
    te = df[df["day"] >= cut].reset_index(drop=True)
    tdays = np.sort(tr["day"].unique())
    vcut = tdays[int(len(tdays) * 0.85)]
    fit = tr[tr["day"] < vcut]
    val = tr[tr["day"] >= vcut]
    print(f"train {fit['window_id'].nunique():,}w ({len(fit):,} rows) | "
          f"val {val['window_id'].nunique():,}w | test {te['window_id'].nunique():,}w "
          f"({len(te):,} rows) | test days {te['day'].nunique()}")
    if len(fit) < 500 or len(te) < 500:
        print("not enough data"); return

    import xgboost as xgb
    Xf, yf = fit[feats].to_numpy(np.float32), fit["y_up"].to_numpy()
    Xv, yv = val[feats].to_numpy(np.float32), val["y_up"].to_numpy()
    Xt, yt = te[feats].to_numpy(np.float32), te["y_up"].to_numpy()

    def fit_clf(dev):
        m = xgb.XGBClassifier(
            n_estimators=args.rounds, learning_rate=args.lr, max_depth=args.depth,
            subsample=0.8, colsample_bytree=0.8, min_child_weight=20,
            reg_lambda=1.0, tree_method="hist", device=dev, n_jobs=-1,
            eval_metric="logloss", early_stopping_rounds=100, random_state=args.seed)
        m.fit(Xf, yf, eval_set=[(Xv, yv)], verbose=False)
        return m

    try:
        clf = fit_clf("cuda")
        dev_used = "cuda"
    except Exception as e:  # noqa: BLE001
        print(f"  cuda failed ({str(e)[:80]}), falling back to cpu")
        clf = fit_clf("cpu")
        dev_used = "cpu"
    print(f"gbm device={dev_used}  best_iteration={clf.best_iteration}\n")

    p_gbm = clf.predict_proba(Xt)[:, 1]

    # regression on the move still to come, normalised
    rg = xgb.XGBRegressor(
        n_estimators=args.rounds, learning_rate=args.lr, max_depth=args.depth,
        subsample=0.8, colsample_bytree=0.8, min_child_weight=20, reg_lambda=1.0,
        tree_method="hist", device=dev_used, n_jobs=-1,
        early_stopping_rounds=100, random_state=args.seed)
    rg.fit(fit[feats].to_numpy(np.float32), fit["remain_z"].to_numpy(),
           eval_set=[(val[feats].to_numpy(np.float32), val["remain_z"].to_numpy())],
           verbose=False)
    r_gbm = rg.predict(Xt)

    te = te.assign(p_gbm=p_gbm, r_gbm=r_gbm, p_reg=phi((te["z"] + r_gbm).to_numpy()))
    print(f"=== TEST, pooled ({len(te):,} rows) ===")
    for name, col in (("coin flip 0.5", None), ("digital Phi(z)", "p_dig"),
                      ("gbm classify ", "p_gbm"), ("gbm->regress ", "p_reg")):
        p = np.full(len(te), 0.5) if col is None else te[col].to_numpy()
        print(f"  {name}  logloss {logloss(yt, p):.5f}  brier {brier(yt, p):.5f}  "
              f"auc {auc(yt, p):.4f}  acc {np.mean((p > 0.5) == yt):.4f}")
    print()
    print(f"=== TEST, window level (one row per window: mean prediction vs outcome) ===")
    w = te.groupby("window_id").agg(y=("y_up", "first"), p_dig=("p_dig", "mean"),
                                    p_gbm=("p_gbm", "mean"), p_reg=("p_reg", "mean"),
                                    day=("day", "first"), tau=("tau_s", "mean"))
    for name, col in (("digital", "p_dig"), ("gbm", "p_gbm"), ("regress", "p_reg")):
        d = w[col] - w["y"]
        mu, t = day_t(d, w["day"])
        print(f"  {name:8s} logloss {logloss(w['y'], w[col]):.5f}  "
              f"brier {brier(w['y'], w[col]):.5f}  mean(p)-mean(y) {d.mean():+.5f}  "
              f"day-t {t:+.2f}")

    print(f"\n=== by |z| band (z = cl_delta / (sigma*sqrt(tau))) ===")
    print(f"{'band':>12s} {'n':>7s} {'realised':>9s} {'p_dig':>8s} {'p_gbm':>8s} "
          f"{'ll_dig':>8s} {'ll_gbm':>8s} {'gain':>8s}")
    for lo, hi in BANDS:
        c = te[(te["z"].abs() >= lo) & (te["z"].abs() < hi)]
        if len(c) < 200:
            continue
        ll_d = logloss(c["y_up"], c["p_dig"])
        ll_g = logloss(c["y_up"], c["p_gbm"])
        print(f"{lo:5.2f}-{hi:5.2f} {len(c):7,d} {c['y_up'].mean():9.4f} "
              f"{c['p_dig'].mean():8.4f} {c['p_gbm'].mean():8.4f} {ll_d:8.5f} "
              f"{ll_g:8.5f} {ll_d - ll_g:+8.5f}")

    print(f"\n=== top features by gain ===")
    gain = clf.feature_importances_
    for i in np.argsort(gain)[::-1][:15]:
        print(f"  {feats[i]:20s} {gain[i]:.4f}")

    print(f"\n=== regression on the move still to come (remain_z) ===")
    yv = te["remain_z"].to_numpy()
    print(f"  baseline (predict 0): mse {np.mean(yv ** 2):.5f}")
    print(f"  gbm                 : mse {np.mean((yv - r_gbm) ** 2):.5f}  "
          f"R2 {1 - np.mean((yv - r_gbm) ** 2) / np.mean(yv ** 2):+.5f}  "
          f"corr {np.corrcoef(yv, r_gbm)[0, 1]:+.4f}")
    w = te.groupby("window_id").agg(y=("remain_z", "mean"), p=("r_gbm", "mean"),
                                    day=("day", "first"))
    print(f"  window-level corr: {np.corrcoef(w['y'], w['p'])[0, 1]:+.4f}")
    pred = te.assign(pred=r_gbm).groupby(["tau_s"])["pred"].mean()
    print(f"  predicted remaining (z) by tau: " +
          "  ".join(f"{t}s:{v:+.3f}" for t, v in pred.items()))

    if args.prices and os.path.exists(args.prices):
        px = pd.read_parquet(args.prices)
        m = te.merge(px[["window_id", "tau_s", "up_px", "up_from_dn"]],
                     on=["window_id", "tau_s"], how="inner")
        m["mkt"] = m["up_px"].where(m["up_px"].notna(), m["up_from_dn"])
        m = m[m["mkt"].notna() & (m["mkt"] > 0) & (m["mkt"] < 1)]
        print(f"\n=== vs the real market price ({len(m):,} matched rows, "
              f"{m['window_id'].nunique():,} windows, {m['day'].nunique()} days) ===")
        if len(m) < 200:
            print("  too few matched rows"); return
        for name, col in (("market ", "mkt"), ("digital", "p_dig"),
                          ("gbm    ", "p_gbm")):
            print(f"  {name} logloss {logloss(m['y_up'], m[col]):.5f}  "
                  f"brier {brier(m['y_up'], m[col]):.5f}")
        print(f"  model-minus-market logloss (negative = model better): "
              f"{logloss(m['y_up'], m['p_gbm']) - logloss(m['y_up'], m['mkt']):+.5f}")

        print("\n=== realised EV of trading the model's disagreement with the market ===")
        print("entry at the market's own traded price; $1 staked per signal;")
        print("'slip' adds 2c of adverse fill to every entry")
        up, mk, day = m["y_up"].to_numpy(), m["mkt"].to_numpy(), m["day"].to_numpy()
        for thr in (0.0, 0.02, 0.05, 0.10):
            for slip in (0.0, 0.02):
                side = np.where(m["p_gbm"].to_numpy() - mk > thr, 1,
                                np.where(mk - m["p_gbm"].to_numpy() > thr, -1, 0))
                ev = np.where(side == 1, up - mk - slip,
                              np.where(side == -1, mk - slip - up, 0.0))
                sel = side != 0
                if sel.sum() < 50:
                    continue
                v = ev[sel]
                per_day = v.sum() / m.loc[sel, "day"].nunique()
                print(f"  thr {thr:.2f} slip {slip:.2f}  n={sel.sum():6,d} "
                      f"({sel.mean():5.1%} of rows)  EV ${v.mean():+.4f}  "
                      f"{battery(v, day[sel])}  ${per_day:+.2f}/day")

        print("\n=== same, by tau bucket ===")
        for lo, hi in ((0, 15), (15, 60), (60, 150), (150, 300)):
            c = m[(m["tau_s"] > lo) & (m["tau_s"] <= hi)]
            if len(c) < 100:
                continue
            u, k = c["y_up"].to_numpy(), c["mkt"].to_numpy()
            side = np.where(c["p_gbm"].to_numpy() - k > 0.02, 1,
                            np.where(k - c["p_gbm"].to_numpy() > 0.02, -1, 0))
            ev = np.where(side == 1, u - k, np.where(side == -1, k - u, 0.0))[side != 0]
            ll_m = logloss(c["y_up"], c["mkt"])
            ll_g = logloss(c["y_up"], c["p_gbm"])
            print(f"  tau {lo:3d}-{hi:3d}  n={len(c):6,d}  "
                  f"ll_mkt {ll_m:.5f} ll_gbm {ll_g:.5f} diff {ll_g - ll_m:+.5f}  "
                  f"EV(thr .02) {ev.mean():+.4f} on {len(ev):,}"
                  if len(ev) else
                  f"  tau {lo:3d}-{hi:3d}  n={len(c):6,d}  ll_mkt {ll_m:.5f} "
                  f"ll_gbm {ll_g:.5f} diff {ll_g - ll_m:+.5f}  no signals")
        print("\n=== timing test: is that EV just the tape being fresher than the print? ===")
        print("entry s seconds AFTER the decision second, thr 0.02, no slip")
        base = m[["window_id", "tau_s", "y_up", "day", "p_gbm"]].copy()
        for s in (0, 1, 2, 3, 5, 10):
            p2 = px.assign(tau_s=px["tau_s"] + s)[["window_id", "tau_s", "up_px",
                                                     "up_from_dn"]]
            q = base.merge(p2, on=["window_id", "tau_s"], how="inner")
            q["mkt"] = q["up_px"].where(q["up_px"].notna(), q["up_from_dn"])
            q = q[q["mkt"].notna() & (q["mkt"] > 0) & (q["mkt"] < 1)]
            if len(q) < 100:
                print(f"  +{s:2d}s: too few rows ({len(q)})")
                continue
            sd = np.where(q["p_gbm"].to_numpy() - q["mkt"].to_numpy() > 0.02, 1,
                          np.where(q["mkt"].to_numpy() - q["p_gbm"].to_numpy() > 0.02,
                                   -1, 0))
            ev = np.where(sd == 1, q["y_up"] - q["mkt"],
                          np.where(sd == -1, q["mkt"] - q["y_up"], 0.0))[sd != 0]
            print(f"  +{s:2d}s: n={len(ev):5,d}  EV ${ev.mean():+.4f}  "
                  f"{battery(ev, q['day'].to_numpy()[sd != 0])}")


if __name__ == "__main__":
    main()
