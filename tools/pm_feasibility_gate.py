"""DON'T TAKE A BET THE CLOCK CANNOT PAY. The gate the margin floor cannot see.

THE OPERATOR'S POINT (2026-09-18), on `sol-updown-5m-1789756200`: rather than
entering late into a position that then runs out of time, do not enter at all.

WHY THE MARGIN FLOOR CANNOT DO THIS. The payout is `TWAP60(bell)` and at an
entry with `tau` seconds left, `(60 - tau)` seconds of that window are ALREADY
PRINTED and unchangeable. If they printed against us, the REMAINING `tau`
seconds must average far enough the other way to drag the whole 60 s mean over:

    required_remaining_mean = (60*level - printed_secs*printed_mean) / tau

On the sol round that was **+4.83 bps sustained for 31 s** -- an enormous move --
while the entry's own margin read a perfectly ordinary +0.5 bps. The margin is a
statement about NOW; this is a statement about what the clock still allows. They
are different numbers and only one of them knows about the locked-in half.

TWO FORMS, both swept here:
  REQ     the required remaining move in bps (absolute)
  REQ/sig the same, divided by `sigma*sqrt(tau)` -- how many standard deviations
          of the REMAINING time it asks for. This is the scale-free form, and
          unlike the margin's scale-free variant it is not a re-expression of
          the same quantity: it prices FEASIBILITY, not strength.

Also sweeps the blunt version (`cross_min_tau_s`) for comparison, because if a
plain tau floor captures the same thing there is no reason for a new knob.

    /root/analysis-venv/bin/python tools/pm_feasibility_gate.py
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_cross_replay as base  # noqa: E402

WIN_S = 60.0


def stat(rows):
    if not rows:
        return None
    d = pd.DataFrame(rows)
    v = d.per.to_numpy()
    n = len(v)
    t = v.mean() / (v.std(ddof=1) / math.sqrt(n)) if n > 1 and v.std(ddof=1) else 0.0
    dm = d.groupby("day").per.mean().to_numpy()
    day_t = (dm.mean() / (dm.std(ddof=1) / math.sqrt(len(dm)))
             if len(dm) > 1 and dm.std(ddof=1) else 0.0)
    if len(dm) > 1:
        best = d.groupby("day").per.mean().idxmax()
        drop = d[d.day != best].per.mean()
    else:
        drop = v.mean()
    h = n // 2
    return dict(n=n, per=v.mean(), tot=v.sum(), t=t, day_t=day_t,
                h1=v[:h].mean() if h else 0.0, h2=v[h:].mean(), drop=drop,
                win=d.won.mean())


def line(lab, s):
    if not s:
        print(f"  {lab:<26} n=0"); return
    ok = (s["t"] >= 2 and s["day_t"] >= 2 and s["h1"] > 0 and s["h2"] > 0
          and s["drop"] > 0)
    print(f"  {lab:<26} n={s['n']:4d} win={s['win']:5.1%} per={s['per']:+.4f} "
          f"tot={s['tot']:+7.2f} t={s['t']:+5.2f} day-t={s['day_t']:+5.2f} "
          f"halves={s['h1']:+.3f}/{s['h2']:+.3f} [{'PASS' if ok else 'fail'}]")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default=base.DB)
    ap.add_argument("--px-lo", type=float, default=0.10)
    args = ap.parse_args()
    from btcpredict.config import settings
    look_s, hold_s = base.shipped_look_s(), base.shipped_hold_s()
    floor = float(settings.cross_min_margin_bps)
    px_hi = float(settings.cross_max_fill_px)
    tau_lo, tau_hi = float(settings.cross_min_tau_s), float(settings.cross_max_tau_s)

    r, q = base.load(args.db)
    q = q.copy(); q["fam"] = q.slug.map(base.family_of)
    rows = []
    for _, row in r.iterrows():
        pre = base.precompute(q, row, look_s, args.px_lo, px_hi, tau_lo, tau_hi)
        if pre is None:
            continue
        e = base.pick(pre, signal="fc", floor_bps=floor, look_s=look_s,
                      px_lo=args.px_lo, px_hi=px_hi, tau_lo=tau_lo,
                      tau_hi=tau_hi, hold_s=hold_s)
        if e is None:
            continue
        end_ms, level, tape = int(pre["r"].end_ms), pre["level"], pre["tape"]
        ts, tau = int(e["ts_ms"]), float(e["tau"])
        # the PRINTED part of the paid window, as of the entry
        pr = [p for t, p in tape if end_ms - 60_000 <= t <= ts]
        rem_s = min(WIN_S, max(1e-9, tau))
        pr_s = max(0.0, WIN_S - rem_s)
        if pr_s > 0 and pr:
            need = (WIN_S * level - pr_s * (sum(pr) / len(pr))) / rem_s
        else:
            need = level                       # nothing printed: just the level
        req = 1e4 * (need - level) / level     # bps the REMAINING time must average
        # the entry's side decides the sign that hurts
        req = req if e["side"] == "Up" else -req
        # sigma of the remaining stretch, from the tape's own 1 s returns
        w = [p for t, p in tape if ts - 60_000 <= t <= ts and p > 0]
        if len(w) > 10:
            rr = [math.log(b / a) for a, b in zip(w, w[1:]) if a > 0 and b > 0]
            sig = float(np.std(rr, ddof=1)) * 1e4 if len(rr) > 8 else 0.0
        else:
            sig = 0.0
        z = (req / (sig * math.sqrt(rem_s))) if sig > 0 else 0.0
        rows.append(dict(day=e["day"], per=e["per"], won=e["won"], tau=tau,
                         req=req, z=z, margin=abs(e["margin"])))
    D = pd.DataFrame(rows)
    print(f"entries {len(D)} | floor {floor} tau [{tau_lo:.0f},{tau_hi:.0f}]")
    print(f"required-remaining-move (bps): median {D.req.median():+.2f} "
          f"p90 {D.req.quantile(.9):+.2f} max {D.req.max():+.2f}")
    print(f"  of these, {int((D.tau < 60).sum())} entries are at tau<60 "
          f"(where the window is partly locked in)\n")

    print("=== A. per-share by REQUIRED remaining move (bps) ===")
    for lo, hi in ((-1e9, 0), (0, 1), (1, 2), (2, 4), (4, 8), (8, 1e9)):
        sel = D[(D.req >= lo) & (D.req < hi)]
        if len(sel) < 5:
            continue
        line(f"req [{lo if lo > -1e8 else '-inf'},{hi if hi < 1e8 else 'inf'})",
             stat(sel.to_dict("records")))
    print("\n=== B. a CEILING on the required remaining move ===")
    base_s = stat(D.to_dict("records"))
    line("no ceiling", base_s)
    for c in (8, 4, 3, 2, 1):
        line(f"req <= {c} bps", stat(D[D.req <= c].to_dict("records")))
    print("\n=== C. the scale-free form: req / (sigma*sqrt(remaining)) ===")
    for c in (3.0, 2.0, 1.5, 1.0, 0.5):
        line(f"z <= {c:.1f}", stat(D[D.z <= c].to_dict("records")))
    print("\n=== D. the blunt version for comparison: a tau floor ===")
    for c in (5, 15, 30, 45, 60, 90):
        line(f"tau >= {c}s", stat(D[D.tau >= c].to_dict("records")))
    print("\n  HOW TO READ: a gate only earns if it REMOVES a negative cohort.")
    print("  Compare each row against 'no ceiling' -- more total, not just more")
    print("  per-share, or it is just trading less.")


main()
