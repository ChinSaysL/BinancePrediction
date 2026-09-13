"""Does order flow add anything beyond window delta?

HorseDev77's repo is a scaffold with no data or results, but its feature list
names three things we have never tested: CVD (taker buys minus sells), VWAP
deviation, and volume surge. The corpus has all of them per second -- taker_buy_
volume, taker_sell_volume, volume, n_trades, vwap -- for every window, so the
question can be asked properly instead of guessed.

The test that matters: window delta alone gets a 99.4% hold rate at tau<=10s, but
the market charges ~0.98 for it. An extra feature is only worth having if it
finds a bucket where the hold rate is HIGH while the price is still LOW. So this
measures hold rate per (delta band x flow bucket), using only data up to tau, and
reports the battery on each cell.

Search warning: this is a scan, and scans find flattering cells by chance. Every
cell is shown with n, both halves and drop-best-day, and nothing here is
actionable on its own.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os

import numpy as np
import pandas as pd

TAU = 10  # seconds before close; matches the gist's entry and our impulse arm


def battery(win: np.ndarray, days: np.ndarray) -> dict:
    s = {"n": len(win), "wr": float(win.mean())}
    if len(win) > 1:
        sd = win.std(ddof=1)
        s["t"] = float(win.mean() / (sd / math.sqrt(len(win)))) if sd > 0 else float("nan")
    else:
        s["t"] = float("nan")
    d = pd.DataFrame({"w": win, "day": days})
    g = d.groupby("day")["w"].mean()
    s["t_day"] = (float(g.mean() / (g.std(ddof=1) / math.sqrt(len(g))))
                  if len(g) > 1 and g.std(ddof=1) > 0 else float("nan"))
    h = len(win) // 2
    s["h1"], s["h2"] = float(win[:h].mean()), float(win[h:].mean())
    s["drop1"] = float(d[d["day"] != g.idxmax()]["w"].mean())
    return s


def load(root: str) -> pd.DataFrame:
    rows = []
    for f in sorted(glob.glob(os.path.join(root, "data", "*", "*", "ticks_dense.parquet"))):
        d = os.path.dirname(f)
        mp = os.path.join(d, "metadata.json")
        if not os.path.exists(mp):
            continue
        try:
            t = pd.read_parquet(f)
            m = json.load(open(mp))
            outs, pr = m.get("outcomes"), m.get("outcomePrices")
            outs = json.loads(outs) if isinstance(outs, str) else outs
            pr = json.loads(pr) if isinstance(pr, str) else pr
            if not outs or not pr or "Up" not in outs:
                continue
            up_won = float(pr[outs.index("Up")]) > 0.5
        except Exception:  # noqa: BLE001
            continue
        if len(t) < 120:
            continue
        i = len(t) - TAU
        vol = t["volume"].to_numpy(float)
        buy = t["taker_buy_volume"].to_numpy(float)
        sell = t["taker_sell_volume"].to_numpy(float)
        close = t["close"].to_numpy(float)
        strike = float(t["open"].iloc[0])
        if strike <= 0:
            continue
        spot = close[i]
        delta = (spot - strike) / strike * 100.0
        tot = vol[:i + 1].sum()
        cvd = float((buy[:i + 1].sum() - sell[:i + 1].sum()) / tot) if tot > 0 else 0.0
        pv = float((t["vwap"].to_numpy(float)[:i + 1] * vol[:i + 1]).sum())
        vwap = pv / tot if tot > 0 else spot
        vdev = (spot - vwap) / vwap * 100.0 if vwap > 0 else 0.0
        recent = vol[max(0, i - 59):i + 1].sum()
        prior = vol[max(0, i - 119):max(0, i - 59)].sum()
        surge = recent / prior if prior > 0 else 0.0
        rows.append({
            "date": f.split("/")[-3], "delta": delta, "absd": abs(delta),
            "cvd": cvd, "vdev": vdev, "surge": surge,
            "aligned": (cvd > 0) == (delta > 0),
            "win": (delta > 0) == bool(up_won),
        })
    return pd.DataFrame(rows)


def show(tag: str, s: dict) -> None:
    print(f"  {tag:34s} n={s['n']:5d}  hold {s['wr']:6.1%}  t {s['t']:+5.2f}  "
          f"day-t {s['t_day']:+5.2f}  halves {s['h1']:5.1%}/{s['h2']:5.1%}  "
          f"drop1 {s['drop1']:5.1%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/btc5m")
    ap.add_argument("--min-n", type=int, default=60)
    args = ap.parse_args()
    df = load(args.root)
    if df.empty:
        print("no windows")
        return
    print(f"windows: {len(df)} over {df['date'].nunique()} days  (tau={TAU}s)\n")

    for lo, hi in [(0.0, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.30)]:
        sub = df[(df["absd"] >= lo) & (df["absd"] < hi)]
        if len(sub) < args.min_n:
            continue
        print(f"=== |delta| {lo:.2f}-{hi:.2f}%  (n={len(sub)}) ===")
        show("all", battery(sub["win"].to_numpy(), sub["date"].to_numpy()))
        for name, mask in (("\n  CVD agrees with move", sub["aligned"]),
                           ("  CVD disagrees", ~sub["aligned"])):
            c = sub[mask]
            if len(c) >= args.min_n:
                show(name, battery(c["win"].to_numpy(), c["date"].to_numpy()))
        # continuous features, split at the median
        for col in ("vdev", "surge"):
            med = sub[col].median()
            for tag, c in ((f"  {col} > median", sub[sub[col] > med]),
                           (f"  {col} <= median", sub[sub[col] <= med])):
                if len(c) >= args.min_n:
                    show(tag, battery(c["win"].to_numpy(), c["date"].to_numpy()))
        print()


if __name__ == "__main__":
    main()
