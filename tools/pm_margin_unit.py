"""IS A FLAT bps FLOOR THE RIGHT OBJECT, NOW THAT tau SPANS 5-180 s?

READ THIS BEFORE SWEEPING `cross_min_margin_bps`. The floor asks "is the
forecast far enough past the level to be worth paying the spread for". What
decides that is not the DISTANCE, it is the distance relative to how much the
paid quantity can still move -- and that residual shrinks as the bell nears.

    the bet          TWAP60(bell) > TWAP60(open)
    the observation  fc(ts), our estimate of TWAP60(bell), `m` bps past the level
    what matters     P(the sign survives) = f(m / residual_sd(tau))

At tau = 10 s most of the paid window has already printed, so 0.25 bps is a
large multiple of what is left to move. At tau = 170 s NONE of it has printed
(`twap_forecast` returns the spot there by construction) and the same 0.25 bps
is a small fraction of one standard deviation -- i.e. noise. A single flat bps
floor must therefore be too slack at one end of the window and too strict at
the other, and sweeping it globally can only find a compromise that is wrong at
both.

THIS IS THE ERROR CLASS THE REPO ALREADY SHIPPED A FIX FOR, one knob over:
`cross_max_round_range_bps` was turned off on 2026-09-18 because "range-so-far
is a fixed bps count of a quantity whose expected size grows with the time
already elapsed", and it was measured RIGHT in a band holding 5% of entries and
BACKWARDS in the 64% at tau>120. The margin floor is the mirror image. The
difference that matters: range-so-far carried no signal in any unit, so it was
deleted; the margin IS the signal, so the question is the unit, not the gate.

WHAT THIS PRINTS
  1. per-share by (tau bucket x margin bucket) -- does the margin that pays
     rise with tau? If the profitable threshold is flat, a global floor is fine
     and the sweep is the right next step.
  2. the same entries in a SCALE-FREE unit, z = m / (sigma_bps * sqrt(tau)),
     and whether z collapses the tau dependence that m leaves behind.
  3. a like-for-like: the best flat floor vs the best z floor at equal entry
     count, so the comparison is not "a stricter gate looks better".

    /root/analysis-venv/bin/python tools/pm_margin_unit.py
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

FEE = base.FEE
TAU_BUCKETS = [(5, 30), (30, 60), (60, 120), (120, 180)]
M_BUCKETS = [(0.0, 0.25), (0.25, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 99.0)]


def sigma_bps(tape: list[tuple[int, float]], ts: int, win_s: float = 60.0
              ) -> float | None:
    """Realised 1-second vol over the trailing `win_s`, in bps per sqrt(second).

    The arm carries its own EWMA sigma; this reconstructs the same order of
    magnitude from the tape the replay already has, which keeps the tool
    self-contained. It is a NORMALISER, not a traded quantity -- it only has to
    be monotone in the real thing.
    """
    lo = ts - int(win_s * 1000)
    px = [(t, p) for t, p in tape if lo <= t <= ts and p and p > 0]
    if len(px) < 10:
        return None
    r = []
    for (t0, p0), (t1, p1) in zip(px, px[1:]):
        dt = (t1 - t0) / 1000.0
        if dt <= 0:
            continue
        r.append(math.log(p1 / p0) / math.sqrt(dt))
    if len(r) < 8:
        return None
    s = float(np.std(r, ddof=1)) * 1e4
    return s if s > 1e-9 else None


def collect(r, q, look_s, hold_s, px_lo, px_hi, tau_lo, tau_hi) -> pd.DataFrame:
    """Every entry the chain takes at floor 0, stamped with m, tau and sigma."""
    out = []
    for _, row in r.iterrows():
        pre = base.precompute(q, row, look_s, px_lo, px_hi, tau_lo, tau_hi)
        if pre is None:
            continue
        e = base.pick(pre, signal="fc", floor_bps=0.0, look_s=look_s,
                      px_lo=px_lo, px_hi=px_hi, tau_lo=tau_lo, tau_hi=tau_hi,
                      hold_s=hold_s)
        if e is None:
            continue
        s = sigma_bps(pre["tape"], int(e["ts_ms"]))
        if s is None:
            continue
        e = dict(e)
        e["sigma"] = s
        # RESIDUAL SCALE. Past tau=60 none of the paid window has printed, so
        # the whole of it is still to come; inside it, only the unprinted part
        # moves. sqrt() of that is the honest first-order normaliser.
        resid = math.sqrt(max(1.0, min(e["tau"], 60.0) / 3.0
                              + max(0.0, e["tau"] - 60.0)))
        e["z"] = abs(e["margin"]) / (s * resid)
        e["am"] = abs(e["margin"])
        out.append(e)
    return pd.DataFrame(out)


def cell(d: pd.DataFrame) -> str:
    if d.empty:
        return f"{'n=0':>22s}"
    v = d.per.to_numpy()
    return f"n={len(v):4d} {v.mean():+.4f} w{d.won.mean():4.0%}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default=base.DB)
    ap.add_argument("--px-lo", type=float, default=0.10)
    args = ap.parse_args()
    from btcpredict.config import settings
    tau_lo = float(settings.cross_min_tau_s)
    tau_hi = float(settings.cross_max_tau_s)
    px_hi = float(settings.cross_max_fill_px)
    look_s, hold_s = base.shipped_look_s(), base.shipped_hold_s()
    shipped = float(settings.cross_min_margin_bps)

    r, q = base.load(args.db)
    q = q.copy()
    q["fam"] = q.slug.map(base.family_of)
    D = collect(r, q, look_s, hold_s, args.px_lo, px_hi, tau_lo, tau_hi)
    print(f"entries {len(D)} | tau [{tau_lo:.0f},{tau_hi:.0f}] look_s {look_s} "
          f"hold_s {hold_s} band [{args.px_lo:.2f},{px_hi:.2f}) | "
          f"SHIPPED floor {shipped} bps")
    print(f"sigma over the tape: median {D.sigma.median():.3f} bps/sqrt(s)")

    print("\n=== 1. per-share by tau x |margin| -- is the paying threshold flat? ===")
    hdr = "  tau \\ |m| bps  " + "".join(
        f"{f'[{a:g},{b:g})':>23s}" for a, b in M_BUCKETS)
    print(hdr)
    for tl, th in TAU_BUCKETS:
        row = D[(D.tau >= tl) & (D.tau < th)]
        cells = "".join(f"{cell(row[(row.am >= a) & (row.am < b)]):>23s}"
                        for a, b in M_BUCKETS)
        print(f"  [{tl:3d},{th:3d})      {cells}")

    print("\n=== 2. the same entries in the SCALE-FREE unit z = |m|/(sigma*sqrt(resid)) ===")
    zb = [(0.0, 0.25), (0.25, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 99.0)]
    hdr = "  tau \\ z        " + "".join(
        f"{f'[{a:g},{b:g})':>23s}" for a, b in zb)
    print(hdr)
    for tl, th in TAU_BUCKETS:
        row = D[(D.tau >= tl) & (D.tau < th)]
        cells = "".join(f"{cell(row[(row.z >= a) & (row.z < b)]):>23s}"
                        for a, b in zb)
        print(f"  [{tl:3d},{th:3d})      {cells}")

    print("\n=== 3. like-for-like: flat bps vs z, matched on entry count ===")
    print("  (a stricter gate keeps a better cohort by construction, so the")
    print("   two are compared at the SAME n, not at the same threshold)")
    for frac in (1.0, 0.75, 0.5, 0.25, 0.1):
        n_keep = int(len(D) * frac)
        if n_keep < 20:
            continue
        by_m = D.nlargest(n_keep, "am")
        by_z = D.nlargest(n_keep, "z")
        def line(tag, d):
            v = d.per.to_numpy()
            t = v.mean() / (v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 1 and v.std(ddof=1) else 0.0
            dm = d.groupby("day").per.mean().to_numpy()
            dt_ = (dm.mean() / (dm.std(ddof=1) / math.sqrt(len(dm)))
                   if len(dm) > 1 and dm.std(ddof=1) else 0.0)
            h = len(v) // 2
            return (f"{tag} n={len(v):4d} per-share {v.mean():+.4f} "
                    f"tot {v.sum():+7.2f} t {t:+5.2f} day-t {dt_:+5.2f} "
                    f"halves {v[:h].mean():+.4f}/{v[h:].mean():+.4f}")
        print(f"  keep {frac:4.0%}:")
        print(f"     {line('by |m| >= %6.3f bps' % by_m.am.min(), by_m)}")
        print(f"     {line('by  z  >= %6.3f    ' % by_z.z.min(), by_z)}")


main()
