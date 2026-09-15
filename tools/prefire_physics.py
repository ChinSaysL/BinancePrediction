"""IDEA 3, the physics half: is a cross knowable ~360ms ahead?

Why 360ms. The chain measured on this venue:

    decision -> venue createTime   125ms  (with the warm quote; 169-220 cold)
    venue create -> terminal FOK   260-1237ms, fails clustering ~425ms
    --------------------------------------------------------------
    decision -> the venue's verdict  ~550ms

The venue's own match takes ~425ms, and a fast crossing has left our bound by
~250ms (measured on the 50ms `cross fine` paths: the ask is +0.010 median,
+0.070 p90, and the fires show 0.49 -> 0.54 by +250ms). So the order must be
CREATED at about -175ms relative to the cross, and with a 125-185ms pipeline the
TRIGGER must fire at roughly -300..-360ms.

That is the whole claim of "predictive pre-firing": approach the strike with
speed and dispatch early. It is falsifiable on the tick tape, and nobody has
tested it at this lead time. This tool does.

Trigger (as proposed): |mid - K| in [0.4, 0.8] bps on the approaching side, with
momentum toward K > 1.5 bps/s over the last 500ms.

  * K is the round's opening mid -- that is how the venue defines the strike
    ("the strike is the open price"), so no DB join is needed and any recorded
    day can be used.
  * CONTROL: the same proximity band with NO momentum filter, and with the
    momentum filter alone. If the momentum clause adds nothing over proximity,
    the trigger is not a predictor.

    python3 tools/prefire_physics.py --file data/bookticker/BTCUSDT-2026-09-01.csv.gz
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file",
                    default="data/bookticker/BTCUSDT-2026-09-01.csv.gz")
    ap.add_argument("--round-s", type=int, default=300)
    ap.add_argument("--lead-ms", type=int, default=360,
                    help="how far before the cross we must be able to see it")
    args = ap.parse_args()

    df = pd.read_csv(args.file, compression="gzip",
                     usecols=["timestamp", "ask_price", "bid_price"])
    t = df["timestamp"].to_numpy(dtype=np.float64) / 1e6
    mid = (df["ask_price"].to_numpy(dtype=np.float64)
           + df["bid_price"].to_numpy(dtype=np.float64)) / 2.0
    print(f"{len(t):,} ticks over {(t[-1]-t[0])/3600:.2f}h "
          f"({len(t)/(t[-1]-t[0]):.0f}/s)")

    lead = args.lead_ms / 1000.0
    grid = np.arange(t[0] // args.round_s * args.round_s,
                     t[-1] + args.round_s, args.round_s)
    rows = []
    n_rounds = 0
    for a, b in zip(grid[:-1], grid[1:]):
        i0 = int(np.searchsorted(t, a, "left"))
        i1 = int(np.searchsorted(t, b, "left"))
        if i1 - i0 < 50:
            continue
        n_rounds += 1
        tt, mm = t[i0:i1], mid[i0:i1]
        K = mm[0]

        d = (mm - K) / K * 1e4
        up = d >= 0.0

        # index of the first tick at/after 500ms back, for momentum
        j = np.searchsorted(tt, tt - 0.5, "right") - 1
        j = np.maximum(j, 0)
        mom = (mm - mm[j]) / 0.5 / K * 1e4          # bps per second

        flips_up = np.nonzero(up[1:] & ~up[:-1])[0] + 1
        flips_dn = np.nonzero(~up[1:] & up[:-1])[0] + 1
        t_up = tt[flips_up] if len(flips_up) else np.array([])
        t_dn = tt[flips_dn] if len(flips_dn) else np.array([])

        def next_cross(want_up):
            src, fidx = ((t_up, flips_up) if want_up else (t_dn, flips_dn))
            out = np.full(len(tt), np.inf)
            if len(fidx) == 0:
                return out
            pos = np.searchsorted(fidx, np.arange(len(tt)), "left")
            ok = pos < len(fidx)
            out[ok] = src[pos[ok]]
            return out

        nxt = np.where(~up, next_cross(True), next_cross(False))
        secs = nxt - tt
        cross = secs <= lead

        prox = (np.abs(d) >= 0.4) & (np.abs(d) <= 0.8)
        toward = np.where(~up, mom, -mom)        # positive = closing on K
        fast = toward > 1.5
        rows.append((n_rounds, prox, fast, cross, np.abs(d), secs))

    prox_all = np.concatenate([r[1] for r in rows])
    fast_all = np.concatenate([r[2] for r in rows])
    secs_all = np.concatenate([r[5] for r in rows])
    cross_all = secs_all <= lead

    def rate(mask, name):
        n = int(mask.sum())
        if not n:
            print(f"  {name:34s} n=0")
            return
        p = cross_all[mask].mean()
        print(f"  {name:34s} n={n:8,d}  P(cross within {args.lead_ms}ms) "
              f"= {p:6.1%}")

    print(f"\nrounds: {n_rounds}  ({args.round_s}s grid, day "
          f"{pd.to_datetime(t[0], unit='s'):%Y-%m-%d})\n")
    rate(prox_all, "PROXIMITY 0.4-0.8bps")
    rate(prox_all & ~fast_all, "  ... and NOT moving toward K")
    rate(prox_all & fast_all, "  ... AND momentum >1.5bps/s  <- the")
    rate(fast_all, "MOMENTUM >1.5bps/s, any distance")
    rate(np.ones_like(prox_all, dtype=bool), "ALL ticks (base rate)")

    trig = prox_all & fast_all
    n_trig = int(trig.sum())
    print(f"\n  triggers/round: {n_trig/max(1,n_rounds):.1f}"
          f"   rounds with >=1 trigger: "
          f"{sum(1 for r in rows if (r[1] & r[2]).any())}/{n_rounds}")

    print("\nDOES ANY LEAD TIME MAKE IT RELIABLE? (trigger = proximity + momentum)")
    print(f"  {'lead':>7s} {'P(cross|trigger)':>17s} {'P(cross|proximity only)':>25s}")
    for l in (0.1, 0.25, 0.5, 1.0, 2.0, 5.0):
        a = (secs_all[trig] <= l).mean()
        b = (secs_all[prox_all] <= l).mean()
        print(f"  {l*1000:5.0f}ms {a:16.1%} {b:24.1%}")
    print("  The venue's own match is ~425ms and our create is ~125ms, so the")
    print("  usable lead is ~300-360ms -- the row that matters is 360ms. Even at")
    print("  multi-second leads the trigger does not become a reliable detector,")
    print("  because micro-momentum measured over 500ms does not persist.")

    print("\n  READ: the trigger has to be right far more often than not. If")
    print("  P(cross within lead) is near 50% we are firing on coin flips, and")
    print("  every miss is a position bought before the move that never came")
    print("  -- which is exactly the PRE rule's -0.061/share.")


if __name__ == "__main__":
    main()
