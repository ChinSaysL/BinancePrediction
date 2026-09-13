"""How well is this market priced? The bar any model has to clear.

Joins the 1-second tape features to Polymarket's own traded prices and scores the
market against settlement, next to the closed form a competent bot would run:

    digital = Phi( cl_delta / (sigma_ref * sqrt(tau)) )

If the market's log loss is already below the closed form's, then the market knows
something the tape-level closed form does not, and a model trained only on tape
features has nothing to add there. The band table localises where (if anywhere)
the market is soft -- the flat band is the only place a momentum signal was shown
to carry information, so it is the place to look.
"""
from __future__ import annotations

import argparse
import math

import numpy as np
import pandas as pd


def phi(x):
    return 0.5 * (1.0 + np.vectorize(math.erf)(np.asarray(x) / math.sqrt(2.0)))


def logloss(y, p, eps=1e-9):
    p = np.clip(np.asarray(p, float), eps, 1 - eps)
    y = np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier(y, p):
    return float(np.mean((np.asarray(p, float) - np.asarray(y, float)) ** 2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/btc5m_ml.parquet")
    ap.add_argument("--prices", default="data/btc5m_prices.parquet")
    args = ap.parse_args()

    df = pd.read_parquet(args.data)
    px = pd.read_parquet(args.prices)
    df["z"] = df["cl_delta_bps"] / (df["sigma_ref_bps"] * np.sqrt(df["tau_s"]))
    df["p_dig"] = phi(df["z"].to_numpy())
    m = df.merge(px[["window_id", "tau_s", "up_px", "up_from_dn", "mid",
                     "vol_up", "vol_dn"]], on=["window_id", "tau_s"], how="inner")
    print(f"joined rows: {len(m):,}   windows: {m['window_id'].nunique():,}   "
          f"days: {m['day'].nunique()}")
    if m.empty:
        return
    # prefer the direct Up print, fall back to the Down-implied one
    m["mkt"] = m["up_px"].where(m["up_px"].notna(), m["up_from_dn"])
    m = m[m["mkt"].notna() & (m["mkt"] > 0) & (m["mkt"] < 1)]
    print(f"rows with a market print: {len(m):,}\n")

    print("=== overall ===")
    for name, col in (("market  ", "mkt"), ("digital ", "p_dig")):
        p = m[col].to_numpy()
        y = m["y_up"].to_numpy()
        acc = float(np.mean((p > 0.5) == y))
        print(f"  {name} logloss {logloss(y, p):.5f}  brier {brier(y, p):.5f}  "
              f"acc {acc:.4f}  mean p {p.mean():.4f}  realised {y.mean():.4f}")

    print("\n=== by tau ===")
    print(f"{'tau':>5s} {'n':>7s} {'realised':>9s} {'mkt':>7s} {'dig':>7s} "
          f"{'ll_mkt':>8s} {'ll_dig':>8s} {'mkt better by':>13s}")
    for tau, c in m.groupby("tau_s"):
        if len(c) < 100:
            continue
        lmk, ldig = logloss(c["y_up"], c["mkt"]), logloss(c["y_up"], c["p_dig"])
        print(f"{tau:5d} {len(c):7,d} {c['y_up'].mean():9.4f} {c['mkt'].mean():7.4f} "
              f"{c['p_dig'].mean():7.4f} {lmk:8.5f} {ldig:8.5f} {ldig - lmk:+13.5f}")

    bands = [(0.0, 0.25), (0.25, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 99.0)]
    print("\n=== by |z| band (z = cl_delta / (sigma*sqrt(tau))) ===")
    print(f"{'band':>12s} {'n':>7s} {'realised':>9s} {'mkt':>7s} {'dig':>7s} "
          f"{'ll_mkt':>8s} {'ll_dig':>8s} {'mkt better by':>13s}")
    for lo, hi in bands:
        c = m[(m["z"].abs() >= lo) & (m["z"].abs() < hi)]
        if len(c) < 150:
            continue
        lmk, ldig = logloss(c["y_up"], c["mkt"]), logloss(c["y_up"], c["p_dig"])
        print(f"{lo:5.2f}-{hi:5.2f} {len(c):7,d} {c['y_up'].mean():9.4f} "
              f"{c['mkt'].mean():7.4f} {c['p_dig'].mean():7.4f} {lmk:8.5f} "
              f"{ldig:8.5f} {ldig - lmk:+13.5f}")

    # how many seconds before the close is the market still uncertain?
    print("\n=== market price distribution by tau (share of prints in each bucket) ===")
    buck = [(0.0, 0.05), (0.05, 0.3), (0.3, 0.7), (0.7, 0.95), (0.95, 1.01)]
    print(f"{'tau':>5s} " + " ".join(f"{a:.2f}-{b:.2f}" for a, b in buck) + "   n")
    for tau, c in m.groupby("tau_s"):
        if len(c) < 100:
            continue
        p = c["mkt"].to_numpy()
        sh = " ".join(f"{np.mean((p >= a) & (p < b)):8.3f}" for a, b in buck)
        print(f"{tau:5d} {sh} {len(c):7,d}")

    # the market's implied sigma, inverted from its own prices
    print("\n=== implied per-second vol the market is paying for ===")
    sub = m[(m["z"].abs() > 1e-9) & (m["mkt"] > 0.02) & (m["mkt"] < 0.98)].copy()
    if len(sub) > 500:
        p = np.clip(sub["mkt"].to_numpy(), 1e-6, 1 - 1e-6)
        z_mkt = np.array([math.sqrt(2) * _erfinv(2 * x - 1) for x in p])
        with np.errstate(divide="ignore", invalid="ignore"):
            sig_mkt = (sub["cl_delta_bps"].to_numpy()
                       / (z_mkt * np.sqrt(sub["tau_s"].to_numpy())))
        ok = np.isfinite(sig_mkt) & (sig_mkt > 0)
        print(f"  median implied sigma {np.median(sig_mkt[ok]):.3f} bps/s   "
              f"median tape sigma_ref {np.median(sub['sigma_ref_bps']):.3f} bps/s   "
              f"ratio {np.median(sig_mkt[ok]) / np.median(sub['sigma_ref_bps']):.3f}")


def _erfinv(x):
    x = np.asarray(x, float)
    w = -np.log((1.0 - x) * (1.0 + x))
    out = np.empty_like(w)
    m = w < 5.0
    ww = w[m] - 2.5
    p = np.array([2.81022636e-08, 3.43273939e-07, -3.5233877e-06,
                  -4.39150654e-06, 0.00021858087, -0.00125372503,
                  -0.00417768164, 0.246640727, 1.50140941])
    a = p[0]
    for c in p[1:]:
        a = a * ww + c
    out[m] = a * x[m]
    ww = np.sqrt(w[~m]) - 3.0
    q = np.array([-0.000200214257, 0.000100950558, 0.00134934322,
                  -0.00367342844, 0.00573950773, -0.0076224613,
                  0.00943887047, 1.00167406, 2.83297682])
    b = q[0]
    for c in q[1:]:
        b = b * ww + c
    out[~m] = b * x[~m]
    return out


if __name__ == "__main__":
    main()
