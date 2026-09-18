"""ANSWER THE TAU WINDOW: which look-ahead window is best, priced honestly.

THE QUESTION. `cross_min_tau_s=5, cross_max_tau_s=60` means the cross arm may
only act in the LAST MINUTE of the round. Widening it to 120 s measured **+$11.18
against +$0.39** on the same 2,246 rounds (28x the money, `pm_param_sweep.py`),
but it was NOT shipped because (a) its day-clustered t was NEGATIVE and (b) past
tau=60 the forecast `((60-tau)*mean + tau*spot)/60` has a NEGATIVE weight -- at
tau=120 it is `2*spot - mean`, an extrapolation of a quantity the venue computes
as a plain average. So the +$11.18 might be a momentum artifact wearing the
venue's rule's clothes. THIS TOOL ANSWERS WHICH IT IS.

HOW. Three signals over EVERY tau window, same rounds, same book:
  fc     the shipped projection, extrapolating past tau=60 (what was measured)
  fc60   the HONEST one: `fc` where tau<=60, and the SPOT past that -- before the
         paid window opens, a martingale's future 60 s mean is best forecast by
         the spot itself (unbiased, zero estimation variance)
  spot   the point spot, the trigger the arm used before the fc trigger shipped
`fc` vs `fc60` on the same window is the cleanest possible test of the artifact:
identical trigger, identical ticks, only the tau>60 value differs.

EVERY ROW IS SCORED BY THE FOUR-CHECK BATTERY the repo applies to any result --
pooled t, day-clustered t, both halves, drop-best-day -- because the whole reason
this question is still open is that the last answer failed check 2.

WHAT IS STILL NOT MODELLED (same caveats as `pm_cross_replay.py`): entries pay
the recorded ask with no sweep premium and no FAK miss rate, and the live
`cross_max_round_range_bps` gate is NOT applied -- it refuses a round that has
already moved far, which binds hardest on exactly the EARLY entries a wide
window admits, so a wide window's live result can only be worse than this.

Usage (VPS, analysis venv):
    /root/analysis-venv/bin/python tools/pm_tau_window.py
    /root/analysis-venv/bin/python tools/pm_tau_window.py --by-family
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pm_cross_replay import (family_of, load,  # noqa: E402
                             pick, precompute, shipped_hold_s, shipped_look_s)

WINDOWS = [(5, 60), (5, 90), (5, 120), (5, 180), (5, 300), (30, 120),
           (60, 120), (60, 180), (60, 300), (120, 300)]


def stat(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    d = pd.DataFrame(rows)
    v = d.per.to_numpy()
    n = len(d)
    dm = d.groupby("day").per.mean().to_numpy()
    day_t = (np.mean(dm) / (np.std(dm, ddof=1) / math.sqrt(len(dm)))
             if len(dm) > 1 and np.std(dm, ddof=1) else 0.0)
    # drop-best-day: remove the single best day by MEAN and re-average
    if len(dm) > 1:
        best = d.groupby("day").per.mean().idxmax()
        db = d[d.day != best].per.to_numpy()
        drop = db.mean() if len(db) else 0.0
    else:
        drop = v.mean()
    h = n // 2
    return dict(n=n, ask=d.ask.mean(), win=d.won.mean(), per=v.mean(),
                total=v.sum(), t=(v.mean() / (v.std(ddof=1) / math.sqrt(n))
                                  if n > 1 and v.std(ddof=1) else 0.0),
                day_t=day_t, h1=v[:h].mean() if h else 0.0, h2=v[h:].mean(),
                drop=drop)


def line(label: str, s: dict | None, mark: str = " ") -> None:
    if s is None:
        print(f"  {mark} {label:<14} n=   0")
        return
    ok = ("PASS" if s["t"] >= 2 and s["day_t"] >= 2 and s["h1"] > 0
          and s["h2"] > 0 and s["drop"] > 0 else "fail")
    print(f"  {mark} {label:<14} n={s['n']:4d} win={s['win']:5.1%} "
          f"ask={s['ask']:.3f} per-share={s['per']:+.4f} total={s['total']:+6.2f} "
          f"t={s['t']:+5.2f} day-t={s['day_t']:+5.2f} "
          f"halves={s['h1']:+.4f}/{s['h2']:+.4f} drop-best={s['drop']:+.4f} "
          f"[{ok}]")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default="/root/BinancePrediction/data/"
                                             "btcpredict.db")
    ap.add_argument("--look-s", type=float, default=None,
                    help="default: the SHIPPED cross_look_s")
    ap.add_argument("--hold-s", type=float, default=None,
                    help="default: the SHIPPED cross_hold_s (0 = off)")
    ap.add_argument("--floor-bps", type=float, default=0.25,
                    help="the shipped floor; 0.25 bps as of 2026-09-18")
    ap.add_argument("--px-lo", type=float, default=0.10)
    ap.add_argument("--px-hi", type=float, default=0.65)
    ap.add_argument("--by-family", action="store_true")
    args = ap.parse_args()
    if args.look_s is None:
        args.look_s = shipped_look_s()
    if args.hold_s is None:
        args.hold_s = shipped_hold_s()

    r, q = load(args.db)
    if r.empty:
        print("no PM-era settled rounds")
        return
    q = q.copy()
    q["fam"] = q.slug.map(family_of)
    days = sorted({dt.datetime.fromtimestamp(t / 1000, dt.UTC).strftime("%m-%d")
                   for t in r.start_ms})
    print(f"POLYMARKET ERA ONLY: {len(r)} settled rounds, days {days}")
    print(f"floor {args.floor_bps} bps, band [{args.px_lo:.2f},{args.px_hi:.2f}],"
          f" one entry/round, pays the recorded ask")
    print("fc = the shipped projection (extrapolates past tau=60); "
          "fc60 = fc where tau<=60 and the SPOT past that; spot = point spot")

    keys = [(sig, w) for sig in ("fc60", "fc", "spot") for w in WINDOWS]
    hits: dict[tuple, list[dict]] = {k: [] for k in keys}
    for i, (_, rr) in enumerate(r.iterrows()):
        pre = precompute(q, rr, args.look_s, 0.01, 0.99, 1.0, 900.0)
        if pre is None:
            continue
        for sig, (t0, t1) in keys:
            got = pick(pre, signal=sig, floor_bps=args.floor_bps,
                       look_s=args.look_s, px_lo=args.px_lo,
                       px_hi=args.px_hi, tau_lo=float(t0), tau_hi=float(t1),
                       hold_s=args.hold_s)
            if got:
                hits[(sig, (t0, t1))].append(got)
        if i % 700 == 0:
            print(f"    ... {i}/{len(r)}", flush=True)

    for sig, title in (("fc60", "THE HONEST SIGNAL (fc<=60, spot>60)"),
                       ("fc", "THE EXTRAPOLATING SIGNAL (shipped formula)"),
                       ("spot", "THE POINT SPOT")):
        print(f"\n=== {title} ===")
        rows = [(f"[{a},{b}]", stat(hits[(sig, (a, b))])) for a, b in WINDOWS]
        best = max((s["per"], lab) for lab, s in rows if s and s["n"] >= 8)
        for lab, s in rows:
            line(lab, s, "*" if s and lab == best[1] else " ")
        print(f"   (* = best per-share among windows holding n>=8: {best[0]:+.4f} "
              f"at {best[1]})")

    print("\n=== THE ARTIFACT TEST: same window, only the tau>60 value differs ===")
    for a, b in WINDOWS:
        if b <= 60:
            continue
        o, h = stat(hits[("fc", (a, b))]), stat(hits[("fc60", (a, b))])
        if o and h:
            print(f"  [{a},{b}]  extrapolating {o['per']:+.4f}/sh "
                  f"(total {o['total']:+6.2f})  vs  honest {h['per']:+.4f}/sh "
                  f"(total {h['total']:+6.2f})  -> the extrapolation is worth "
                  f"{o['per'] - h['per']:+.4f}/sh")

    if args.by_family:
        print("\n=== PER FAMILY, honest signal ===")
        for a, b in WINDOWS:
            lab = f"[{a},{b}]"
            d = pd.DataFrame(hits[("fc60", (a, b))])
            if d.empty:
                continue
            print(f"\n  {lab}")
            for fam in sorted(d.family.unique()):
                line(f"  {fam}", stat(d[d.family == fam].to_dict("records")))

    print("\n  HOW TO READ: [PASS] needs pooled t>=2 AND day-clustered t>=2 AND")
    print("  both halves positive AND drop-best-day positive. Anything less is")
    print("  the same half-2 effect that made the first answer wrong.")


if __name__ == "__main__":
    main()
