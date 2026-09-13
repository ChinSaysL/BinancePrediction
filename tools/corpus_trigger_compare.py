"""Whose trigger is better: theirs or ours? Same tape, same windows.

The arena's bots and ours trade the same idea -- late entry, momentum-confirmed
-- with different parameters:
  ours        : |2s momentum| >= 4 bps,  entry in the last 10s
  sniper      : |1s momentum| >= 3 bps,  entry in the last 10s   (their 0.0003)
  late_window : |avg momentum| >= 8 bps, entry in the last 90s   (their 0.0008)

Momentum sign is the predicted direction; the window's close versus its open is
the outcome. What matters is not the win rate alone but the win rate RELATIVE to
the price the market charges for that side -- which is fixed by the delta, so the
delta distribution of each trigger's signals is reported alongside.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os

import numpy as np
import pandas as pd


def battery(win: np.ndarray, days: np.ndarray) -> dict:
    s = {"n": len(win), "wr": float(win.mean())}
    sd = win.std(ddof=1) if len(win) > 1 else 0.0
    s["t"] = float(win.mean() / (sd / math.sqrt(len(win)))) if sd > 0 else float("nan")
    d = pd.DataFrame({"w": win, "day": days})
    g = d.groupby("day")["w"].mean()
    s["t_day"] = (float(g.mean() / (g.std(ddof=1) / math.sqrt(len(g))))
                  if len(g) > 1 and g.std(ddof=1) > 0 else float("nan"))
    h = len(win) // 2
    s["h1"], s["h2"] = float(win[:h].mean()), float(win[h:].mean())
    s["drop1"] = float(d[d["day"] != g.idxmax()]["w"].mean())
    return s


def line(tag: str, s: dict) -> str:
    return (f"  {tag:26s} n={s['n']:5d}  hold {s['wr']:6.1%}  t {s['t']:+6.2f}  "
            f"day-t {s['t_day']:+6.2f}  halves {s['h1']:5.1%}/{s['h2']:5.1%}  "
            f"drop1 {s['drop1']:5.1%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/btc5m")
    ap.add_argument("--min-n", type=int, default=50)
    ap.add_argument("--top", type=int, default=500,
                    help="compare the top-N windows by each trigger's own strength")
    args = ap.parse_args()

    rows = []
    for f in sorted(glob.glob(os.path.join(args.root, "data", "*", "*", "ticks_dense.parquet"))):
        d = os.path.dirname(f)
        mp = os.path.join(d, "metadata.json")
        if not os.path.exists(mp):
            continue
        try:
            t = pd.read_parquet(f, columns=["open", "close"])
            m = json.load(open(mp))
            outs, pr = m.get("outcomes"), m.get("outcomePrices")
            outs = json.loads(outs) if isinstance(outs, str) else outs
            pr = json.loads(pr) if isinstance(pr, str) else pr
            if not outs or not pr or "Up" not in outs:
                continue
            up_won = float(pr[outs.index("Up")]) > 0.5
        except Exception:  # noqa: BLE001
            continue
        c = t["close"].to_numpy(float)
        if len(c) < 200:
            continue
        strike = float(t["open"].iloc[0])
        i10, i90 = len(c) - 10, max(0, len(c) - 90)
        ret = np.diff(c) / c[:-1]

        def at(i: int) -> dict:
            r1 = c[i] / c[i - 1] - 1 if i > 1 else 0.0
            r2 = c[i] / c[i - 2] - 1 if i > 2 else 0.0
            avg = float(ret[max(0, i - 60):i].mean()) if i > 5 else 0.0
            return {"d": (c[i] - strike) / strike * 100.0, "r1": r1, "r2": r2, "avg": avg}

        a10, a90 = at(i10), at(i90)
        rec = {"date": f.split("/")[-3], "up_won": up_won, "absd10": abs(a10["d"]),
               "absd90": abs(a90["d"]),
               "m_ours": abs(a10["r2"]), "m_sniper": abs(a10["r1"]),
               "m_late": abs(a90["avg"]),
               "d_ours": 1 if a10["r2"] > 0 else 0,
               "d_sniper": 1 if a10["r1"] > 0 else 0,
               "d_late": 1 if a90["avg"] > 0 else 0}
        rows.append(rec)

    df = pd.DataFrame(rows)
    days = df["date"].nunique()
    print(f"windows: {len(df)} over {days} days")
    print(f"median |1s return|: {np.median(np.abs(df['m_sniper'])) * 1e4:.2f} bps   "
          f"| median |2s|: {np.median(df['m_ours']) * 1e4:.2f} bps   "
          f"| median |60s avg|: {np.median(df['m_late']) * 1e4:.2f} bps\n")
    print(f"--- top {args.top} windows by each trigger's own strength "
          f"(matched n, so the comparison is about selection quality) ---\n")
    for mcol, dcol, name, dcol_delta in (
            ("m_ours", "d_ours", "ours   2s momentum @tau<=10", "absd10"),
            ("m_sniper", "d_sniper", "sniper 1s momentum @tau<=10", "absd10"),
            ("m_late", "d_late", "late   60s avg mom @tau<=90", "absd90")):
        top = df.nlargest(args.top, mcol)
        if top.empty:
            continue
        win = ((top[dcol] == 1) == top["up_won"]).to_numpy(float)
        s = battery(win, top["date"].to_numpy())
        print(f"=== {name} ===")
        print(f"  threshold implied: |mom| >= {top[mcol].min() * 1e4:.2f} bps   "
              f"median |delta| {top[dcol_delta].median():.4f}%")
        print(line("top signals", s))
        for lo, hi in ((0.0, 0.02), (0.02, 0.05), (0.05, 99.0)):
            c = top[(top[dcol_delta] >= lo) & (top[dcol_delta] < hi)]
            if len(c) < 30:
                continue
            w2 = ((c[dcol] == 1) == c["up_won"]).to_numpy(float)
            print(line(f"  |delta| {lo:.2f}-{hi:.2f}%", battery(w2, c["date"].to_numpy())))
        print()


if __name__ == "__main__":
    main()
