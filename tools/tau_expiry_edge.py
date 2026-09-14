"""Is there money in the last seconds? Tested on event-level book, no strike needed.

The earlier answer rested on 14 and 8 priced snapshots from the corpus, which is
not enough to conclude anything. This uses data/hf2/kartik -- every BTC 5m round
of 2026-07-17, ~99k book updates per round, full depth -- and sidesteps the
strike entirely.

The test: at each tau, buy the side the BOOK currently favours, and see what it
costs and how often it wins. Near expiry that is the same trade the cross arm
makes (the side spot just crossed into is the side the book favours), and it
needs no Chainlink strike -- the winner comes from the book at expiry.

If buying the favourite near the bell is profitable, the tau floor is costing
money. If the ask rises to match the certainty, it is not.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = "data/hf2/kartik/top10_parquet/2026-07-17"
BUCKETS = [0, 3, 5, 10, 15, 20, 30, 45, 60, 90, 150, 300]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="btc")
    ap.add_argument("--tf", default="5m")
    ap.add_argument("--fee-bps", type=float, default=200.0)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    files = sorted(glob.glob(f"{ROOT}/{a.coin}/{a.tf}/*.parquet"))
    if a.limit:
        files = files[:a.limit]
    rows = []
    for f in files:
        try:
            t = pd.read_parquet(f, columns=["secs", "yes_bid", "yes_ask",
                                            "no_bid", "no_ask"])
        except Exception:
            continue
        t = t.dropna(subset=["yes_bid", "yes_ask", "no_ask"])
        t = t[(t.secs >= 0) & (t.secs <= 300)]
        if len(t) < 200:
            continue
        secs = t.secs.to_numpy()
        pup = ((t.yes_bid + t.yes_ask) / 2.0).to_numpy()
        # winner from the book at expiry
        j = int(np.argmin(secs))
        if not (pup[j] > 0.9 or pup[j] < 0.1):
            continue                      # unresolved book: skip
        up_won = pup[j] > 0.5
        ya = t.yes_ask.to_numpy(); na = t.no_ask.to_numpy()
        # one observation per whole second: buy whichever side the book favours
        seen = set()
        for i in range(len(secs)):
            s = int(secs[i])
            if s in seen or s < 1:
                continue
            seen.add(s)
            fav_up = pup[i] > 0.5
            if abs(pup[i] - 0.5) < 1e-9:
                continue
            ask = ya[i] if fav_up else na[i]
            if not (0.0 < ask < 1.0):
                continue
            won = int(fav_up == up_won)
            fee = a.fee_bps * min(1.0, (1 - ask) / ask) / 1e4
            rows.append((s, ask, won, (1 - ask) / ask * (1 - fee) if won else -1.0))

    d = pd.DataFrame(rows, columns=["tau", "ask", "won", "ret"])
    print(f"{a.coin}-{a.tf} 2026-07-17: {len(files)} rounds, "
          f"{len(d):,} one-per-second observations\n")
    print("BUY THE BOOK'S FAVOURITE AT TAU X")
    print(f"{'tau':>10} {'n':>7} {'ask':>7} {'win':>7} {'margin':>8} {'EV/$1':>8}")
    d['tb'] = pd.cut(d.tau, BUCKETS)
    g = d.groupby('tb', observed=True).agg(n=('ret', 'size'), ask=('ask', 'mean'),
                                           win=('won', 'mean'), ev=('ret', 'mean'))
    for tb, r in g.iterrows():
        print(f"{str(tb):>10} {int(r.n):7d} {r.ask:7.3f} {r.win:7.3f} "
              f"{r.win - r.ask:+8.3f} {r.ev:+8.3f}")

    print("\nIS THE BOOK MISPRICED NEAR THE BELL? (margin = win - ask; 0 = fair)")
    for lo, hi in ((1, 5), (5, 10), (10, 20), (10, 60), (60, 300)):
        x = d[(d.tau >= lo) & (d.tau < hi)]
        if len(x) < 50:
            continue
        se = (x.won.std() / np.sqrt(len(x)))
        m = x.won.mean() - x.ask.mean()
        print(f"  tau {lo:3d}-{hi:3d}: n={len(x):6d}  win {x.won.mean():.4f}  "
              f"ask {x.ask.mean():.4f}  margin {m:+.4f} "
              f"({m/se:+.1f} SE)  EV {x.ret.mean():+.4f}/$1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def flip_test(coin="btc", tf="5m", fee_bps=200.0, limit=0):
    """The CROSS trade, near expiry, without needing a strike.

    `buy the favourite` above is a different, much more certain trade (ask
    ~0.93). The cross arm buys the side the market has JUST flipped into, which
    is the same event the book shows when its implied P(Up) crosses 0.5. So:
    detect the book's own side flip, buy the new favourite at that instant, and
    measure by tau. No Chainlink strike required, and the winner is the book's
    own at expiry.
    """
    import glob as _g
    files = sorted(_g.glob(f"{ROOT}/{coin}/{tf}/*.parquet"))
    if limit:
        files = files[:limit]
    rows = []
    for f in files:
        try:
            t = pd.read_parquet(f, columns=["secs", "yes_bid", "yes_ask",
                                            "no_bid", "no_ask"])
        except Exception:
            continue
        t = t.dropna(subset=["yes_bid", "yes_ask", "no_ask"])
        t = t[(t.secs >= 0) & (t.secs <= 300)]
        if len(t) < 200:
            continue
        t = t.sort_values("secs", ascending=False)
        secs = t.secs.to_numpy()
        pup = ((t.yes_bid + t.yes_ask) / 2.0).to_numpy()
        ya = t.yes_ask.to_numpy(); na = t.no_ask.to_numpy()
        j = int(np.argmin(secs))
        if not (pup[j] > 0.9 or pup[j] < 0.1):
            continue
        up_won = pup[j] > 0.5
        fav = pup > 0.5
        for i in range(1, len(secs)):
            if fav[i] == fav[i - 1] or abs(pup[i] - 0.5) < 1e-9:
                continue                      # no flip here
            ask = ya[i] if fav[i] else na[i]
            if not (0.0 < ask < 1.0):
                continue
            won = int(fav[i] == up_won)
            fee = fee_bps * min(1.0, (1 - ask) / ask) / 1e4
            rows.append((int(secs[i]), ask, won,
                         (1 - ask) / ask * (1 - fee) if won else -1.0))
    d = pd.DataFrame(rows, columns=["tau", "ask", "won", "ret"])
    print(f"\n\nBUY THE SIDE THE BOOK JUST FLIPPED INTO  ({len(d):,} flips, "
          f"{len(files)} rounds)")
    print(f"{'tau':>10} {'n':>7} {'ask':>7} {'win':>7} {'margin':>8} {'EV/$1':>9}")
    d['tb'] = pd.cut(d.tau, BUCKETS)
    g = d.groupby('tb', observed=True).agg(n=('ret', 'size'), ask=('ask', 'mean'),
                                           win=('won', 'mean'), ev=('ret', 'mean'))
    for tb, r in g.iterrows():
        print(f"{str(tb):>10} {int(r.n):7d} {r.ask:7.3f} {r.win:7.3f} "
              f"{r.win - r.ask:+8.3f} {r.ev:+9.3f}")
    print("\n  grouped:")
    for lo, hi in ((1, 5), (5, 10), (1, 10), (10, 60), (60, 300)):
        x = d[(d.tau >= lo) & (d.tau < hi)]
        if len(x) < 20:
            continue
        se = x.ret.std() / np.sqrt(len(x))
        print(f"    tau {lo:3d}-{hi:3d}: n={len(x):5d}  ask {x.ask.mean():.3f}  "
              f"win {x.won.mean():.3f}  EV {x.ret.mean():+.3f}/$1 "
              f"({x.ret.mean()/se:+.1f} SE)")
    return d
