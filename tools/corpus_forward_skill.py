"""Is there any predictable structure left in the tape? The honest forecast.

The previous model predicted the SETTLEMENT. Since settlement is close to a
deterministic function of how far the tape has already moved, what it mostly
learned was to nowcast a delta that was already visible -- which is exactly why
its apparent edge died the moment entry was priced one second after the snapshot.

This tests the only thing that could be tradeable: the FORWARD move. The target is
the tape's own return from the decision second to h seconds later, expressed in
trailing sigmas. Features stop at the decision second. The settlement, the basis
and the market do not enter at all, so this is a pure predictability test:

    does the 1-second tape contain exploitable structure at horizon h?

Baselines the model must beat:
  * zero         random walk, E[forward] = 0, R2 = 0
  * continuation go WITH the last move (momentum)
  * reversal     go AGAINST it (mean reversion)

The trade evaluation is deliberately tape-level (EV in bps of the underlying),
because a token-level number needs a token price and that is the step that
previously smuggled in a same-second look-ahead.
"""
from __future__ import annotations

import argparse
import math

import numpy as np
import pandas as pd

DROP = {
    "window_id", "start_ms", "day", "strike_tape", "strike_cl", "final_cl",
    "close_end_tape", "open_tape", "px", "true_delta_bps", "y_up", "y_tape",
}

# exact horizon / grid pairs: decision tau -> future tau = tau - h
PAIRS = {
    2: [5, 8, 10, 12, 15, 20],
    5: [10, 15, 20, 25, 30, 40],
    10: [20, 30, 40, 50, 60, 75],
    15: [30, 45, 75, 90, 105, 120],
    30: [60, 90, 120, 150, 180, 210, 240],
    60: [90, 120, 180, 210, 240, 270],
    120: [150, 180, 210, 240, 270],
}


def battery(v: np.ndarray, days: np.ndarray) -> str:
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
    return (f"t {t:+5.2f} day-t {td:+5.2f} h1 {v[:h].mean():+.2f} "
            f"h2 {v[h:].mean():+.2f} drop1 {drop1:+.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/btc5m_ml.parquet")
    ap.add_argument("--horizons", type=int, nargs="+", default=sorted(PAIRS))
    ap.add_argument("--train-frac", type=float, default=0.7)
    ap.add_argument("--rounds", type=int, default=3000)
    ap.add_argument("--depth", type=int, default=4)
    ap.add_argument("--lr", type=float, default=0.03)
    args = ap.parse_args()

    df = pd.read_parquet(args.data)
    df = df[df["sigma_ref_bps"] > 0].copy()
    feats = [c for c in df.columns
             if c not in DROP and df[c].dtype.kind in "fiu"]
    print(f"rows {len(df):,}  windows {df['window_id'].nunique():,}  "
          f"days {df['day'].nunique()}  features {len(feats)}")
    lut = df[["window_id", "tau_s", "px", "last_ret_bps", "mom_10_bps"]].rename(
        columns={"tau_s": "tf", "px": "px_f", "last_ret_bps": "ret_f",
                 "mom_10_bps": "mom_f"})

    import xgboost as xgb
    days = np.sort(df["day"].unique())
    cut = days[int(len(days) * args.train_frac)]

    for h in args.horizons:
        if h not in PAIRS:
            continue
        d = df[df["tau_s"].isin(PAIRS[h])].copy()
        d["tf"] = d["tau_s"] - h
        m = d.merge(lut, on=["window_id", "tf"], how="inner")
        if len(m) < 5000:
            print(f"\n--- h={h}s: only {len(m)} pairs, skipped")
            continue
        m["y_bps"] = (m["px_f"] / m["px"] - 1.0) * 1e4
        m["y_z"] = m["y_bps"] / (m["sigma_ref_bps"] * math.sqrt(h))
        m = m.replace([np.inf, -np.inf], np.nan).dropna(subset=["y_z", "y_bps"])
        tr = m[m["day"] < cut]
        te = m[m["day"] >= cut]
        if len(tr) < 3000 or len(te) < 1500:
            print(f"\n--- h={h}s: too few rows (train {len(tr)} test {len(te)})")
            continue
        print(f"\n--- horizon {h}s: {len(m):,} pairs, {m['window_id'].nunique():,} "
              f"windows, test {te['window_id'].nunique():,} windows / "
              f"{te['day'].nunique()} days ---")
        print(f"    forward move: sd {m['y_bps'].std():.2f} bps  "
              f"mean {m['y_bps'].mean():+.3f} bps  "
              f"sd in sigmas {m['y_z'].std():.3f}")

        vdays = np.sort(tr["day"].unique())
        vcut = vdays[int(len(vdays) * 0.85)]
        fit, val = tr[tr["day"] < vcut], tr[tr["day"] >= vcut]
        rg = xgb.XGBRegressor(
            n_estimators=args.rounds, learning_rate=args.lr, max_depth=args.depth,
            subsample=0.8, colsample_bytree=0.7, min_child_weight=50,
            reg_lambda=2.0, tree_method="hist", device="cuda", n_jobs=-1,
            early_stopping_rounds=100, random_state=0)
        rg.fit(fit[feats].to_numpy(np.float32), fit["y_z"].to_numpy(),
               eval_set=[(val[feats].to_numpy(np.float32), val["y_z"].to_numpy())],
               verbose=False)
        pred = rg.predict(te[feats].to_numpy(np.float32))
        yz, yb = te["y_z"].to_numpy(), te["y_bps"].to_numpy()
        r2 = 1 - np.mean((yz - pred) ** 2) / np.mean(yz ** 2)
        ic = float(np.corrcoef(yz, pred)[0, 1])
        ics = te.assign(p=pred).groupby("day").apply(
            lambda g: g["y_z"].corr(g["p"]), include_groups=False)
        ics = ics.dropna()
        t_ic = (ics.mean() / (ics.std(ddof=1) / math.sqrt(len(ics)))
                if len(ics) > 1 and ics.std(ddof=1) > 0 else float("nan"))
        print(f"    GBM   R2 {r2:+.5f}  IC {ic:+.4f}  "
              f"per-day IC {ics.mean():+.4f} (day-t {t_ic:+.2f})")

        pos = np.sign(pred)
        ev = pos * yb
        sel = pos != 0
        print(f"    GBM      EV {ev[sel].mean():+.3f} bps  {battery(ev[sel], te['day'].to_numpy()[sel])}")
        for name, s in (("cont (1s)", np.sign(te["last_ret_bps"].to_numpy())),
                        ("rev  (1s)", -np.sign(te["last_ret_bps"].to_numpy())),
                        ("cont (10s)", np.sign(te["mom_10_bps"].to_numpy())),
                        ("rev  (10s)", -np.sign(te["mom_10_bps"].to_numpy()))):
            e = s * yb
            k = s != 0
            print(f"    {name:10s} EV {e[k].mean():+.3f} bps  {battery(e[k], te['day'].to_numpy()[k])}")
        gain = rg.feature_importances_
        top = ", ".join(f"{feats[i]}" for i in np.argsort(gain)[::-1][:6])
        print(f"    top features: {top}")

    print("\nRead this as: an R2 of ~0 with an IC near 0 means the tape is a random "
          "walk at that horizon and no amount of modelling will fix it.")


if __name__ == "__main__":
    main()
