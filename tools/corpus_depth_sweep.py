"""Depth gate on 31 days with the REAL Chainlink strike.

data/btc5m_ml.parquet carries `strike_cl` (the venue's actual Chainlink strike),
`cl_delta_bps` (spot minus that strike, in bps -- the cross arm's own moneyness)
and `y_up` (the venue's settlement). So this needs no reconstruction at all,
which matters more than it sounds: the spot-vs-Chainlink basis measured on this
same file is a MEDIAN 4.01 bps (mean 5.73, 5-95% 0.41-15.64). A strike taken
from spot is therefore wrong by ~8x the 0.5bps gate, which is why every
spot-derived backtest of this arm -- including this session's L2 reconstruction
-- could not measure it.

A cross is a sign change in cl_delta_bps; depth is |cl_delta_bps| at entry.
CIs and t are clustered by DAY, which is the repo's bar.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings as S      # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ml", default="data/btc5m_ml.parquet")
    ap.add_argument("--lo", type=float, default=None)
    ap.add_argument("--hi", type=float, default=None)
    a = ap.parse_args()
    lo = a.lo if a.lo is not None else S.cross_min_tau_s
    hi = a.hi if a.hi is not None else S.cross_max_tau_s

    m = pd.read_parquet(a.ml, columns=["window_id", "day", "tau_s",
                                       "cl_delta_bps", "y_up"])
    m = m.dropna(subset=["cl_delta_bps", "y_up"])
    m = m.sort_values(["window_id", "tau_s"], ascending=[True, False])
    print(f"corpus: {m.window_id.nunique()} rounds over {m.day.nunique()} days | "
          f"tau gate {lo:.0f}-{hi:.0f}s | REAL Chainlink strike, REAL settlement")

    rows = []
    for wid, g in m.groupby("window_id", sort=False):
        d = g.cl_delta_bps.to_numpy()
        tau = g.tau_s.to_numpy()
        y = int(g.y_up.iloc[0])
        day = g.day.iloc[0]
        n = 0
        for i in range(1, len(d)):
            if not (lo <= tau[i] <= hi):
                continue
            if np.sign(d[i]) == np.sign(d[i - 1]) or d[i] == 0:
                continue                       # no crossing at this step
            side_up = d[i] > 0
            rows.append((day, wid, abs(d[i]), int(side_up == bool(y))))
            n += 1
            if n >= S.cross_max_per_round:
                break
    t = pd.DataFrame(rows, columns=["day", "wid", "depth", "won"])
    print(f"crossings found: {len(t)} over {t.wid.nunique()} rounds\n")

    print(f"{'gate':>8} {'n':>6} {'rounds':>7} {'win':>6} {'per-day':>8} "
          f"{'day-t vs .5':>11} {'drop-best':>10} {'neg days':>9}")
    for gate in (0.5, 1.0, 2.0, 5.0, 10.0, 1e9):
        x = t[t.depth <= gate]
        if len(x) < 20:
            continue
        g = x.groupby("day").won.mean()
        dt = (g.mean() - 0.5) / (g.std(ddof=1) / np.sqrt(len(g))) if len(g) > 1 else float("nan")
        db = g.drop(g.idxmax()).mean()
        neg = int((g < 0.5).sum())
        lab = "all" if gate > 1e8 else f"{gate:g}"
        print(f"{lab:>8} {len(x):6d} {x.wid.nunique():7d} {x.won.mean():6.3f} "
              f"{g.mean():8.3f} {dt:11.2f} {db:10.3f} {neg:4d}/{len(g)}")

    print("\nINCREMENTAL cells (what widening the gate actually buys):")
    print(f"{'cell(bps)':>12} {'n':>6} {'win':>6} {'per-day':>8} {'day-t vs .5':>11}")
    for a_, b_ in ((0, 0.5), (0.5, 1), (1, 2), (2, 5), (5, 10), (10, 1e9)):
        x = t[(t.depth >= a_) & (t.depth < b_)]
        if len(x) < 20:
            continue
        g = x.groupby("day").won.mean()
        dt = (g.mean() - 0.5) / (g.std(ddof=1) / np.sqrt(len(g))) if len(g) > 1 else float("nan")
        lab = f"{a_:g}-{'inf' if b_ > 1e8 else f'{b_:g}'}"
        print(f"{lab:>12} {len(x):6d} {x.won.mean():6.3f} {g.mean():8.3f} {dt:11.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def ev_cells(ml_path="data/btc5m_ml.parquet", px_path="data/btc5m_prices.parquet",
             lo=None, hi=None):
    """Same crossings, priced at the corpus's REAL asks.

    A higher win rate at depth does not settle the knob: a cross further through
    the strike buys a side the venue has already marked up. `*_sell_px` is the
    ASK (the `*_buy_px` column is the BID -- that naming cost a whole earlier
    result), so this is what the arm would actually have paid.
    """
    lo = S.cross_min_tau_s if lo is None else lo
    hi = S.cross_max_tau_s if hi is None else hi
    m = pd.read_parquet(ml_path, columns=["window_id", "day", "tau_s",
                                          "cl_delta_bps", "y_up"]).dropna()
    p = pd.read_parquet(px_path, columns=["window_id", "tau_s",
                                          "up_sell_px", "dn_sell_px"])
    m = m.merge(p, on=["window_id", "tau_s"], how="inner")
    m = m.sort_values(["window_id", "tau_s"], ascending=[True, False])
    rows = []
    for wid, g in m.groupby("window_id", sort=False):
        d = g.cl_delta_bps.to_numpy(); tau = g.tau_s.to_numpy()
        ua = g.up_sell_px.to_numpy(); da = g.dn_sell_px.to_numpy()
        y = int(g.y_up.iloc[0]); day = g.day.iloc[0]
        n = 0
        for i in range(1, len(d)):
            if not (lo <= tau[i] <= hi):
                continue
            if np.sign(d[i]) == np.sign(d[i - 1]) or d[i] == 0:
                continue
            up = d[i] > 0
            ask = ua[i] if up else da[i]
            if not (0.0 < ask < 1.0):
                continue
            w = int(up == bool(y))
            fee = 200.0 * min(1.0, (1 - ask) / ask) / 1e4
            ret = (1 - ask) / ask * (1 - fee) if w else -1.0
            rows.append((day, wid, abs(d[i]), w, ask, ret))
            n += 1
            if n >= S.cross_max_per_round:
                break
    t = pd.DataFrame(rows, columns=["day", "wid", "depth", "won", "ask", "ret"])
    print(f"\n=== PRICED at the corpus's real asks: {len(t)} crossings, "
          f"{t.day.nunique()} days ===")
    print(f"{'gate':>8} {'n':>6} {'ask':>6} {'win':>6} {'EV/$1':>8} "
          f"{'per-day':>9} {'day-t':>7} {'drop-best':>10}")
    for gate in (0.5, 1.0, 2.0, 5.0, 10.0, 1e9):
        x = t[t.depth <= gate]
        if len(x) < 10:
            continue
        g = x.groupby("day").ret.mean()
        dt = g.mean() / (g.std(ddof=1) / np.sqrt(len(g))) if len(g) > 1 else float("nan")
        db = g.drop(g.idxmax()).mean() if len(g) > 1 else float("nan")
        lab = "all" if gate > 1e8 else f"{gate:g}"
        print(f"{lab:>8} {len(x):6d} {x.ask.mean():6.3f} {x.won.mean():6.3f} "
              f"{x.ret.mean():+8.3f} {g.mean():+9.3f} {dt:7.2f} {db:+10.3f}")
    print(f"\n{'cell(bps)':>12} {'n':>6} {'ask':>6} {'win':>6} {'EV/$1':>8} {'per-day':>9}")
    for a_, b_ in ((0, 0.5), (0.5, 1), (1, 2), (2, 5), (5, 1e9)):
        x = t[(t.depth >= a_) & (t.depth < b_)]
        if len(x) < 10:
            continue
        g = x.groupby("day").ret.mean()
        lab = f"{a_:g}-{'inf' if b_ > 1e8 else f'{b_:g}'}"
        print(f"{lab:>12} {len(x):6d} {x.ask.mean():6.3f} {x.won.mean():6.3f} "
              f"{x.ret.mean():+8.3f} {g.mean():+9.3f}")
