"""PRICE THE PERSISTENCE HOLD: does requiring the crossing to HOLD pay?

THE DEFECT. `cross_look_s = 1.0` was the only freshness requirement, and past
tau=60 `fc` IS the spot, so a single 1-second print over the level was the whole
signal. Live `btc-updown-5m-1789711500` (2026-09-18): 25 s flat at 77552.85, one
print at 77560.04, back under the level 5 s later -- and the venue repriced TO
the print, so the FAK paid 0.600 for a book that was 0.41 by the time the tape
reverted. `cross_hold_s` requires the crossed side to have been held for that
long before it counts.

WHAT THIS PRINTS, for each hold, over the whole Polymarket record:

  * the battery on the arm's own rule chain (one entry/round, floor, band) for
    tau in 5-180 AND for the 60-180 slice the hold actually bites in;
  * the INCREMENT: the entries hold=0 takes that hold=H drops, scored on their
    own -- a hold is only worth it if that subset is negative, which is a
    different question from whether the whole cohort stays positive;
  * the same split for the entries every hold keeps.

`per` pays the recorded ask with the real fee, same as every other replay here,
and carries the same two unmodelled caveats (no sweep premium, no FAK miss
rate).

Usage (VPS, analysis venv):
    /root/analysis-venv/bin/python tools/pm_hold_sweep.py
    /root/analysis-venv/bin/python tools/pm_hold_sweep.py --holds 0 1 2 3 5 8
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, "/root/BinancePrediction")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pm_cross_replay import family_of, load, pick, precompute  # noqa: E402
from pm_tau_window import line, stat           # noqa: E402

HOLDS = [0.0, 1.0, 2.0, 3.0, 5.0, 8.0]
WINDOWS = [(5.0, 180.0), (60.0, 180.0)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?",
                    default="/root/BinancePrediction/data/btcpredict.db")
    ap.add_argument("--holds", type=float, nargs="*", default=HOLDS)
    ap.add_argument("--look-s", type=float, default=1.0,
                    help="the SHIPPED cross_look_s")
    ap.add_argument("--floor-bps", type=float, default=0.25)
    ap.add_argument("--px-lo", type=float, default=0.10)
    ap.add_argument("--px-hi", type=float, default=0.65)
    args = ap.parse_args()

    r, q = load(args.db)
    if r.empty:
        print("no PM-era settled rounds")
        return
    q = q.copy()
    q["fam"] = q.slug.map(family_of)
    print(f"POLYMARKET ERA: {len(r)} settled rounds | look {args.look_s}, "
          f"floor {args.floor_bps} bps, band [{args.px_lo:.2f},{args.px_hi:.2f}]")
    print("one entry per round, pays the recorded ask + the per-share fee")

    hits: dict[tuple[float, float, float], list[dict]] = {}
    rounds = list(r.iterrows())
    for i, (_, rr) in enumerate(rounds):
        pre = precompute(q, rr, args.look_s, 0.01, 0.99, 1.0, 900.0)
        if pre is None:
            continue
        for hold in args.holds:
            for (t0, t1) in WINDOWS:
                got = pick(pre, signal="fc", floor_bps=args.floor_bps,
                           look_s=args.look_s, px_lo=args.px_lo,
                           px_hi=args.px_hi, tau_lo=t0, tau_hi=t1,
                           hold_s=hold)
                if got:
                    hits.setdefault((hold, t0, t1), []).append(got)
        if i % 700 == 0:
            print(f"    ... {i}/{len(rounds)}", flush=True)

    for (t0, t1) in WINDOWS:
        print(f"\n=== tau [{t0:.0f},{t1:.0f}] -- every hold, same rounds ===")
        for hold in args.holds:
            line(f"hold={hold:.0f}s", stat(hits.get((hold, t0, t1), [])),
                 "*" if hold == 0 else " ")

    print("\n=== THE INCREMENT: what hold=H DROPS against hold=0 ===")
    print("  (a hold is worth it only if the entries it removes are negative)")
    for (t0, t1) in WINDOWS:
        base = {e["slug"]: e for e in hits.get((0.0, t0, t1), [])}
        print(f"\n  tau [{t0:.0f},{t1:.0f}]")
        for hold in args.holds:
            if hold == 0:
                continue
            kept = {e["slug"]: e for e in hits.get((hold, t0, t1), [])}
            drop = [v for k, v in base.items() if k not in kept]
            add = [v for k, v in kept.items() if k not in base]
            d = pd.DataFrame(drop)
            if d.empty:
                print(f"    hold={hold:.0f}s  drops nothing")
                continue
            h = len(d) // 2
            print(f"    hold={hold:.0f}s  DROPS n={len(d):3d} "
                  f"win={d.won.mean():5.1%} per-share={d.per.mean():+.4f} "
                  f"total={d.per.sum():+7.2f} "
                  f"halves={d.per[:h].mean():+.4f}/{d.per[h:].mean():+.4f}"
                  f"  |  ADDS n={len(add)}"
                  f"  |  keeps n={len(kept):3d} "
                  f"per-share={pd.DataFrame(list(kept.values())).per.mean():+.4f}")

    print("\n  HOW TO READ: a hold that removes a NEGATIVE subset and leaves the")
    print("  rest positive is doing what it was added for. If the dropped subset")
    print("  is positive, the hold is throwing away edge and 0 is the answer.")


if __name__ == "__main__":
    main()
