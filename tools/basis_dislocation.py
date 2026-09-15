"""IDEA 1: perpetual basis dislocation as a 1-2s advance warning.

The claim: "when a massive directional sweep hits Futures, Perp gaps 3-6bps
instantly, blowing out the basis. Cash-and-carry arb takes 800-2500ms to buy
spot and close it. If Basis expands > +4bps over a 500ms window while Spot is
1bp below the strike, Spot crossing K is near-certain over the next 1-2 seconds."

This is NOT the perp-spot lead-lag already tested (correlation of returns, which
peaked at lag 0 with symmetric shoulders). This is the BASIS as a level --
futures minus spot -- and it is only the third time the basis has come up in
FINDINGS: the first two are the ORACLE basis (Chainlink vs Binance spot), which
is a different quantity entirely.

So it is tested here exactly as specified, on the five days holding both tapes
(09-08..09-12, BTC/ETH/BNB).

WHAT MATTERS. The physics already measured in this repo is the bar the claim has
to clear. Time for the BTC mid to travel N bps (30,000 random instants,
bookticker 09-01):

    1 bp   reaches in 30s  52.4%   median 6,640ms   <1s 16.1%   <500ms 10.1%
    2 bp   reaches in 30s  37.6%   median 9,816ms   <1s  6.2%   <500ms  3.1%

The claim is that crossing a strike 1bp away is "near-certain" inside 1-2s. The
unconditional base rate for a 1bp move inside 1s is 16.1%. The basis signal has
to lift that enormously to be "near-certain", and that lift is what is measured.

    python3 tools/basis_dislocation.py --symbol BTCUSDT --day 2026-09-12
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
from perp_spot_lead import GRID, PERP, SPOT, grid_series, load

ROUND_S = 300
LOOK = 10          # 500ms rolling window at the 50ms grid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--day", default="2026-09-12")
    ap.add_argument("--bps", type=float, default=4.0,
                    help="basis expansion threshold, bps over 500ms")
    args = ap.parse_args()

    sp = load(SPOT.format(s=args.symbol, d=args.day))
    pp = load(PERP.format(s=args.symbol, d=args.day))
    t0 = max(sp["t"].min(), pp["t"].min())
    t1 = min(sp["t"].max(), pp["t"].max())
    n = int((t1 - t0) / GRID) + 1
    S = grid_series(sp, t0, n)
    P = grid_series(pp, t0, n)
    print(f"{args.symbol} {args.day}: {(t1-t0)/3600:.1f}h, {n:,} bins of "
          f"{GRID*1000:.0f}ms")

    basis = (P - S) / S * 1e4
    db = np.full(n, np.nan)
    db[LOOK:] = basis[LOOK:] - basis[:-LOOK]
    b0 = np.nanmedian(np.abs(basis))
    print(f"  |basis| median {b0:.2f} bps   p99 {np.nanpercentile(np.abs(basis), 99):.2f}")

    grid = np.arange(t0 // ROUND_S * ROUND_S, t1 + ROUND_S, ROUND_S)
    rows = []          # (tz_ms, up, dist_bps, fwd_by_horizon, crossed_by_horizon)
    HZ = (1, 2, 5, 10, 20, 40)     # 50ms steps -> 50ms..2s
    for a, b in zip(grid[:-1], grid[1:]):
        i0 = int(np.searchsorted(sp["t"].to_numpy(), a, "left"))
        j0 = int(np.searchsorted(pp["t"].to_numpy(), a, "left"))
        k0 = max(int((a - t0) / GRID), 0)
        k1 = int((b - t0) / GRID)
        if k0 < 0 or k1 >= n or k1 - k0 < 200 or i0 >= len(sp) or j0 >= len(pp):
            continue
        K = S[k0]                     # the round's opening mid = the strike
        seg_s = S[k0:k1]
        seg_b = db[k0:k1]
        tm = (np.arange(k0, k1) * GRID + t0) * 1000
        up = seg_s < K                # need spot to RISE to cross
        dist = np.abs(seg_s - K) / K * 1e4
        rows.append((tm, up, dist, seg_s, K, seg_b))
    if not rows:
        raise SystemExit("no usable rounds")

    tm = np.concatenate([r[0] for r in rows])
    up = np.concatenate([r[1] for r in rows])
    dist = np.concatenate([r[2] for r in rows])
    seg = np.concatenate([r[3] for r in rows])
    Ks = np.concatenate([np.full(len(r[0]), r[4]) for r in rows])
    dbs = np.concatenate([r[5] for r in rows])
    N = len(tm)

    def fwd_and_cross(h):
        idx = np.arange(N)
        j = np.minimum(idx + h, N - 1)
        fwd = (seg[j] - seg) / seg * 1e4
        # "crossed" = the needed side of K is reached on the way
        cross = np.where(up, seg[j] >= Ks, seg[j] <= Ks)
        return fwd, cross

    near = dist <= 1.0
    expand_up = dbs >= args.bps
    expand_dn = dbs <= -args.bps

    print(f"\n  spot within 1bp of the strike: {near.sum():,} bins "
          f"({near.mean():.1%})")
    print(f"  basis expands >={args.bps:.0f}bps in 500ms: {expand_up.sum():,} bins"
          f"   contracts <={-args.bps:.0f}bps: {expand_dn.sum():,}")

    for h in (20, 40):      # 1s, 2s -- the window the claim names
        fwd, cross = fwd_and_cross(h)
        print(f"\n  --- horizon {(h*GRID):.1f}s ---")
        for name, m in (
                ("A  basis expansion ALONE", expand_up),
                ("B  expansion AND spot within 1bp (the claim)", expand_up & near),
                ("C  spot within 1bp, any basis", near),
                ("D  ALL bins (base rate)", np.ones(N, bool)),
                ("E  basis contracts AND within 1bp (mirror)", expand_dn & near)):
            m = m & np.isfinite(fwd)
            if m.sum() < 50:
                print(f"    {name:46s} n={int(m.sum()):7,d}  too few")
                continue
            print(f"    {name:46s} n={int(m.sum()):7,d}  "
                  f"P(cross)={cross[m].mean():6.1%}  "
                  f"fwd spot {np.nanmean(fwd[m]):+6.2f}bps")

    print("\n  READ: the claim needs P(cross) for row B to be near 100% of the")
    print("  time inside 1-2s. Compare it with row D, the base rate, and with")
    print("  row C, which is the SAME proximity filter with no basis clause --")
    print("  if C is as high as B, the basis is doing nothing and the claim is")
    print("  really about being close to the strike. Row E is the mirror: if it")
    print("  also shows a high P(cross), the signal is volatility, not direction.")
    print("\n  Scale: this repo measured the BTC mid needing a MEDIAN 6,640ms to")
    print("  travel 1bp, reaching it inside 1s only 16.1% of the time. That is")
    print("  the base rate row D should reproduce, and it is what 'near-certain")
    print("  over the next 1-2 seconds' has to beat by a large margin.")


if __name__ == "__main__":
    main()
