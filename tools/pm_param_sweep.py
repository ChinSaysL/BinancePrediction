"""SWEEP the cross arm's knobs over the POLYMARKET era and rank them.

WHAT IT IS FOR: the user asked "which value gets the highest win rate". That
question has a trap in it -- raising the margin floor raises the win rate almost
by construction (you enter further from the strike), and the venue's ask rises
with it, one for one. `tools/pm_accuracy_ceiling.py` measured exactly that on
2026-09-17: at 100.000% accuracy the ask was 1.000. So every table here prints
WIN RATE AND MONEY ON THE SAME LINE, and the money column is the one to read.

WHY IT IS CHEAP: the level, the forecast and the round's quote rows do not
depend on any knob, so they are computed ONCE per round and every variant is
evaluated from that cache. One pass, ~40 s, ~40 knob combinations.

WHAT IT SWEEPS, at tau [5,60] / band [0.10,0.65] unless the section says so:
    A  the margin floor, 0 -> 5 bps, for BOTH triggers (spot and fc)
    B  the tau window at the shipped floor
    C  the price band at the shipped floor
    D  side (Up vs Down), free from the same entries
    E  the floor sweep re-run on EACH HALF SEPARATELY -- the 5th check in the
       artifact battery: a parameter must rank the same way in both halves, or
       the optimum is fitting

THE SAME CAVEATS AS `pm_cross_replay.py` (it imports its engine): entries pay
the recorded ask, so no sweep premium and no FAK miss rate; read the ORDER of
the rows, not the level of the totals. Three days of data.

Usage (on the VPS, analysis venv -- the bot venv has no numpy):
    /root/analysis-venv/bin/python tools/pm_param_sweep.py
    /root/analysis-venv/bin/python tools/pm_param_sweep.py --by-family
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/BinancePrediction")
# the TOOLS dir LAST so it wins: a stale `pm_cross_replay.py` at the repo root
# (from an earlier rsync) otherwise shadows this one and the import fails on a
# missing `pick`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pm_cross_replay import (FEE, family_of, load,  # noqa: E402
                             pick, precompute, shipped_hold_s, shipped_look_s)

FLOORS = [0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0]
TAUS = [(5, 60), (5, 30), (10, 60), (20, 60), (30, 60), (5, 120), (1, 60),
        (1, 30), (60, 120)]
BANDS = [(0.10, 0.65), (0.05, 0.65), (0.15, 0.65), (0.20, 0.65),
         (0.10, 0.55), (0.10, 0.60), (0.10, 0.70), (0.10, 0.80),
         (0.01, 0.99), (0.25, 0.65)]


def stat(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    d = pd.DataFrame(rows)
    v = d.per.to_numpy()
    n = len(d)
    by_day = d.groupby("day").per.mean()
    days = sorted(d.day.unique())
    dm = by_day.to_numpy()
    day_t = (np.mean(dm) / (np.std(dm, ddof=1) / math.sqrt(len(dm)))
             if len(dm) > 1 and np.std(dm, ddof=1) else 0.0)
    h = n // 2
    return dict(n=n, ask=d.ask.mean(), win=d.won.mean(), per=v.mean(),
                total=v.sum(), t=(v.mean() / (v.std(ddof=1) / math.sqrt(n))
                                  if n > 1 and v.std(ddof=1) else 0.0),
                day_t=day_t, h1=v[:h].mean() if h else 0.0,
                h2=v[h:].mean(), days=days)


def line(label: str, s: dict | None, mark: str = " ") -> None:
    if s is None:
        print(f"   {mark} {label:<24} n=   0")
        return
    print(f"   {mark} {label:<24} n={s['n']:4d} ask={s['ask']:.3f} "
          f"win={s['win']:5.1%} per-share={s['per']:+.4f} "
          f"total={s['total']:+6.2f} t={s['t']:+5.2f} "
          f"day-t={s['day_t']:+5.2f} halves={s['h1']:+.4f}/{s['h2']:+.4f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default="/root/BinancePrediction/data/"
                                             "btcpredict.db")
    ap.add_argument("--look-s", type=float, default=None,
                    help="default: the SHIPPED cross_look_s")
    ap.add_argument("--hold-s", type=float, default=None,
                    help="default: the SHIPPED cross_hold_s (0 = off)")
    ap.add_argument("--by-family", action="store_true")
    args = ap.parse_args()
    if args.look_s is None:
        args.look_s = shipped_look_s()
    if args.hold_s is None:
        args.hold_s = shipped_hold_s()

    r, q = load(args.db)
    if r.empty:
        print("no Polymarket-era settled rounds yet")
        return
    q = q.copy()
    q["fam"] = q.slug.map(family_of)
    print(f"POLYMARKET ERA ONLY: {len(r)} settled rounds, {len(q)} quotes, "
          f"days "
          f"{sorted({dt.datetime.fromtimestamp(t / 1000, dt.UTC).strftime('%m-%d') for t in r.start_ms})}")

    # ---- ONE pass. Everything a knob can change is filtered at pick time.
    combos: list[tuple] = []
    for sig in ("spot", "fc"):
        for fl in FLOORS:
            combos.append(("floor", sig, fl, (5, 60), (0.10, 0.65)))
    for tw in TAUS:
        combos.append(("tau", "fc", 1.0, tw, (0.10, 0.65)))
    for bd in BANDS:
        combos.append(("band", "fc", 1.0, (5, 60), bd))
    hits: dict[tuple, list[dict]] = {c: [] for c in combos}
    order: dict[tuple, list[int]] = {c: [] for c in combos}
    for i, (_, rr) in enumerate(r.iterrows()):
        pre = precompute(q, rr, args.look_s, 0.01, 0.99, 1.0, 900.0)
        if pre is None:
            continue
        base = pre["rows"]
        for c in combos:
            _, sig, fl, (t0, t1), (p0, p1) = c
            got = pick(pre, signal=sig, floor_bps=fl, look_s=args.look_s,
                       px_lo=p0, px_hi=p1, tau_lo=t0, tau_hi=t1,
                       hold_s=args.hold_s)
            if got:
                got["half"] = 0            # filled in below, by round order
                hits[c].append(got)
                order[c].append(i)
        if i % 500 == 0:
            print(f"    ... {i}/{len(r)} rounds", flush=True)
    mid = len(r) // 2
    for c in combos:
        for got, idx in zip(hits[c], order[c]):
            got["half"] = 1 if idx < mid else 2
    for c in combos:
        for got in hits[c]:
            if got["ts_ms"]:
                got["day"] = dt.datetime.fromtimestamp(
                    got["ts_ms"] / 1000, dt.UTC).strftime("%m-%d")

    # ---- A. THE MARGIN FLOOR (what the user asked for)
    print("\n=== A. THE MARGIN FLOOR: win rate AND money, both triggers ===")
    print("    (tau [5,60], band [0.10,0.65], one entry per round; the win rate")
    print("     rises with the floor BY CONSTRUCTION -- read per-share with it)")
    for sig in ("spot", "fc"):
        print(f"\n   trigger = {sig}"
              f"{'  <- the fc trigger ships' if sig == 'fc' else ''}")
        rows = [(f"{fl:4.2f} bps", stat(hits[("floor", sig, fl,
                                             (5, 60), (0.10, 0.65))]))
                for fl in FLOORS]
        best = max((s["per"], lab) for lab, s in rows if s)[1] \
            if any(s for _, s in rows) else ""
        bestw = max((s["win"], lab) for lab, s in rows if s)[1] \
            if any(s for _, s in rows) else ""
        for lab, s in rows:
            mark = ("$" if lab == best else " ") + ("W" if lab == bestw else " ")
            line(lab, s, mark)
        print(f"     $ = best per-share    W = best win rate   "
              f"(per-share {best}, win rate {bestw})")

    # ---- B. THE TAU WINDOW
    print("\n=== B. THE TAU WINDOW (fc trigger, floor 1.0 bps) ===")
    for tw in TAUS:
        line(f"tau [{tw[0]},{tw[1]}]", stat(hits[("tau", "fc", 1.0, tw,
                                                   (0.10, 0.65))]))

    # ---- C. THE PRICE BAND
    print("\n=== C. THE PRICE BAND (fc trigger, floor 1.0 bps, tau [5,60]) ===")
    for bd in BANDS:
        line(f"band [{bd[0]:.2f},{bd[1]:.2f}]",
             stat(hits[("band", "fc", 1.0, (5, 60), bd)]))

    # ---- D. SIDE
    print("\n=== D. SIDE (fc trigger) ===")
    for fl in (0.0, 1.0):
        d = pd.DataFrame(hits[("floor", "fc", fl, (5, 60), (0.10, 0.65))])
        if d.empty:
            continue
        for side in ("Up", "Down"):
            sub = d[d.side == side]
            line(f"floor {fl:.2f} {side:<4}", stat(sub.to_dict("records")))

    # ---- E. THE DIRECTION TEST: the same sweep on each half
    print("\n=== E. THE 5th CHECK: does the floor rank the same in BOTH halves? ===")
    for sig in ("fc", "spot"):
        print(f"\n   trigger = {sig}")
        for hnum in (1, 2):
            for fl in FLOORS:
                d = hits[("floor", sig, fl, (5, 60), (0.10, 0.65))]
                sub = [x for x in d if x["half"] == hnum]
                s = stat(sub)
                if s is None:
                    continue
                print(f"     half {hnum}  floor {fl:4.2f} bps  n={s['n']:4d} "
                      f"win={s['win']:5.1%} per-share={s['per']:+.4f}")

    if args.by_family:
        print("\n=== F. PER FAMILY x FLOOR (fc trigger) ===")
        print("   (1 bps is ~77 ticks on BTC at 77k and ~1 tick on XRP at 1.30, so")
        print("    the SAME floor is a different filter on every family)")
        for fl in (0.0, 0.25, 0.5, 1.0, 1.5):
            d = pd.DataFrame(hits[("floor", "fc", fl, (5, 60), (0.10, 0.65))])
            print(f"\n   floor {fl:.2f} bps")
            if d.empty:
                print("     (no entries at all)")
                continue
            for fam in sorted(d.family.unique()):
                line(fam, stat(d[d.family == fam].to_dict("records")))

    print("\n  HOW TO READ THIS: a win rate with no per-share next to it is not")
    print("  information -- the ask rises with every filter that raises the win")
    print("  rate. Ship the setting whose DIRECTION repeats in both halves (E)")
    print("  and whose per-share is positive, not the one with the best win rate.")


if __name__ == "__main__":
    main()
