"""IDEA 3: order-book depth evaporation ("air pockets").

The claim: informed market makers pull resting asks before a jump; if ask depth
within 3bps of spot collapses >65%, even modest retail buying displaces spot 3-5
bps upward.

CAVEAT, stated first because it limits the reach: the free `bookDepth` archive is
percentage BANDS (-5..+5% in 0.2% steps = 20bps resolution) sampled every ~30s
(recorded in FINDINGS), so it cannot see a 3bps band or a 100ms event. The only
sub-100ms depth on disk is `data/bookticker/*.csv.gz`, which is **top-1 only**
-- best price AND its size. There is no 5-level history here.

So this tests the idea in the form the data allows: a collapse in the size
resting AT THE INSIDE ask. That is a strictly weaker instrument than the claim
(which wants cumulative depth across 3bps), and if the weak version shows nothing
the strong one is not worth recording for; if it DOES show something, the strong
version becomes worth recording.

Controls, because "depth collapsed" and "price is about to move" can be the same
fact read twice:
  A  ask size collapses >65%          <- the claim (expect UP)
  B  bid size collapses >65%          <- the mirror (expect DOWN, else it is vol)
  C  ask size SURGES >2x              <- the opposite (expect nothing / down)
  D  all bins (base rate)

    python3 tools/depth_evaporation.py --symbol BTCUSDT --day 2026-09-01
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

GRID = 0.1            # 100ms
TRAIL = 50            # 5s trailing window for the median size
HORIZONS = (5, 10, 20, 50)   # 500ms, 1s, 2s, 5s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--day", default="2026-09-01")
    ap.add_argument("--drop", type=float, default=0.35,
                    help="ask size must fall below this x its trailing median")
    args = ap.parse_args()

    df = pd.read_csv(f"data/bookticker/{args.symbol}-{args.day}.csv.gz",
                     compression="gzip",
                     usecols=["timestamp", "ask_price", "ask_amount",
                              "bid_price", "bid_amount"])
    t = df["timestamp"].to_numpy(dtype=np.int64) / 1e6
    ap_ = df["ask_price"].to_numpy(float)
    bp_ = df["bid_price"].to_numpy(float)
    asz = df["ask_amount"].to_numpy(float)
    bsz = df["bid_amount"].to_numpy(float)
    mid = (ap_ + bp_) / 2.0

    t0 = t[0]
    n = int((t[-1] - t0) / GRID)
    i = ((t - t0) / GRID).astype(np.int64)
    ok = (i >= 0) & (i < n)
    i = i[ok]

    def last(v):
        out = np.full(n, np.nan)
        out[i] = v[ok]
        return pd.Series(out).ffill().to_numpy()

    A, B, M = last(asz), last(bsz), last(mid)
    print(f"{args.symbol} {args.day}: {n:,} bins of {GRID*1000:.0f}ms, "
          f"top-of-book only (no 5-level history on disk)")

    def trailing_med(v):
        s = pd.Series(v)
        return s.rolling(TRAIL, min_periods=10).median().shift(1).to_numpy()

    tA, tB = trailing_med(A), trailing_med(B)
    with np.errstate(invalid="ignore", divide="ignore"):
        ratioA = A / tA
        ratioB = B / tB

    ev = {
        "A ask size collapses >65%": ratioA < args.drop,
        "B bid size collapses >65%": ratioB < args.drop,
        "C ask size SURGES >2x": ratioA > 2.0,
        "D all bins (base rate)": np.ones(n, dtype=bool),
    }
    for name, mask in ev.items():
        mask = mask & np.isfinite(M) & np.isfinite(ratioA)
        k = int(mask.sum())
        if k < 50:
            print(f"  {name:28s} n={k} -- too few")
            continue
        j = np.nonzero(mask)[0]
        line = f"  {name:28s} n={k:7,d}"
        for h in HORIZONS:
            q = j[j + h < n]
            fwd = (M[q + h] - M[q]) / M[q] * 1e4
            line += f"  {h*GRID*1000:>5.0f}ms {np.nanmean(fwd):+5.2f}"
        print(line + "  bps")

    print("\n  The claim needs row A to be a LARGE positive number at 500ms-1s.")
    print("  Scale check from this repo: the BTC mid needs a MEDIAN 6,640ms to")
    print("  travel 1bp and only 16.1% of 1bp moves complete inside 1s, so '3-5")
    print("  bps with near-zero resistance' is a median ~15,567ms event.")


if __name__ == "__main__":
    main()
