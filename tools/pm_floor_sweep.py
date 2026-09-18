"""THE MARGIN FLOOR, RE-ASKED ON THE WINDOW THE ARM NOW RUNS.

WHY THIS TOOL EXISTS AND WHY THE ANSWER MAY DIFFER FROM THE RECORD. Every
previous ranking of `cross_min_margin_bps` was measured at tau **[5,60]** --
`pm_param_sweep.py` and the `pm_tau_window.py` follow-up both -- where the whole
cohort is 68-111 entries and a floor of 1.0 leaves **16**. config.py closes that
argument with the right verdict for that sample: "n is 42-69 either way, so
neither value is quotable as fitted... Do not re-open the floor on this table; it
needs a sample that can separate 42 entries from 69."

`cross_max_tau_s` went 60 -> 180 on 2026-09-18. The cohort is now ~850 entries,
so the sample the record asked for exists. That is the ONLY reason this is being
re-opened, and the bar is the record's own:

  * the repo's PASS bar -- pooled t>=2 AND day-clustered t>=2 AND both halves
    positive AND drop-best-day positive (`pm_tau_window.stat`/`line`);
  * THE NUISANCE-PARAMETER TEST, which is what actually kept 0.25: the ranking
    must not FLIP between `--look-s 1.0` (shipped) and 2.0 (the tools' old
    default). A knob that changes its mind when a parameter the arm does not
    trade is perturbed has not been measured, it has been fitted.

Both are printed. A floor that wins at 1.0 and loses at 2.0 is REFUSED here, for
the same reason 0.50 was refused before.

    /root/analysis-venv/bin/python tools/pm_floor_sweep.py
    /root/analysis-venv/bin/python tools/pm_floor_sweep.py --tau-lo 5 --tau-hi 60
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

FLOORS = [0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]


def stat(rows: list[dict]) -> dict | None:
    """Verbatim `pm_tau_window.stat`, so the two tables are comparable."""
    if not rows:
        return None
    d = pd.DataFrame(rows)
    v = d.per.to_numpy()
    n = len(d)
    dm = d.groupby("day").per.mean().to_numpy()
    day_t = (np.mean(dm) / (np.std(dm, ddof=1) / math.sqrt(len(dm)))
             if len(dm) > 1 and np.std(dm, ddof=1) else 0.0)
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


def verdict(s: dict) -> str:
    return ("PASS" if s["t"] >= 2 and s["day_t"] >= 2 and s["h1"] > 0
            and s["h2"] > 0 and s["drop"] > 0 else "fail")


def line(label: str, s: dict | None, mark: str = " ") -> None:
    if s is None:
        print(f"  {mark} {label:<12} n=   0")
        return
    print(f"  {mark} {label:<12} n={s['n']:4d} win={s['win']:5.1%} "
          f"ask={s['ask']:.3f} per-share={s['per']:+.4f} total={s['total']:+7.2f} "
          f"t={s['t']:+5.2f} day-t={s['day_t']:+5.2f} "
          f"halves={s['h1']:+.4f}/{s['h2']:+.4f} drop-best={s['drop']:+.4f} "
          f"[{verdict(s)}]")


def entries_for(r, q, look_s, hold_s, floor, px_lo, px_hi, tau_lo, tau_hi,
                cache):
    """One entry per round at this floor. `cache` holds the per-round
    precompute, which does not depend on the floor."""
    out = []
    for _, row in r.iterrows():
        pre = cache.get(row.slug)
        if pre is None:
            pre = base.precompute(q, row, look_s, px_lo, px_hi, tau_lo, tau_hi)
            cache[row.slug] = pre if pre is not None else False
        if not pre:
            continue
        e = base.pick(pre, signal="fc", floor_bps=floor, look_s=look_s,
                      px_lo=px_lo, px_hi=px_hi, tau_lo=tau_lo, tau_hi=tau_hi,
                      hold_s=hold_s)
        if e is not None:
            out.append(e)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default=base.DB)
    ap.add_argument("--tau-lo", type=float, default=None)
    ap.add_argument("--tau-hi", type=float, default=None)
    ap.add_argument("--px-lo", type=float, default=0.10)
    ap.add_argument("--px-hi", type=float, default=None)
    args = ap.parse_args()
    from btcpredict.config import settings
    tau_lo = args.tau_lo if args.tau_lo is not None else float(settings.cross_min_tau_s)
    tau_hi = args.tau_hi if args.tau_hi is not None else float(settings.cross_max_tau_s)
    px_hi = args.px_hi if args.px_hi is not None else float(settings.cross_max_fill_px)
    hold_s = base.shipped_hold_s()
    shipped_look = base.shipped_look_s()
    shipped_floor = float(settings.cross_min_margin_bps)

    r, q = base.load(args.db)
    q = q.copy()
    q["fam"] = q.slug.map(base.family_of)
    print(f"PM era: {len(r)} settled rounds | tau [{tau_lo:.0f},{tau_hi:.0f}] "
          f"band [{args.px_lo:.2f},{px_hi:.2f}) hold_s {hold_s} | "
          f"SHIPPED floor {shipped_floor}")

    tables = {}
    for look in (shipped_look, 2.0):
        cache: dict = {}
        rows = {}
        print(f"\n=== floor sweep at look_s {look:.1f}"
              f"{'  (SHIPPED)' if look == shipped_look else '  (the nuisance check)'} ===")
        for f in FLOORS:
            es = entries_for(r, q, look, hold_s, f, args.px_lo, px_hi,
                             tau_lo, tau_hi, cache)
            s = stat(es)
            rows[f] = s
            line(f"floor {f:.2f}", s,
                 mark="*" if abs(f - shipped_floor) < 1e-9 else " ")
        tables[look] = rows

    print("\n=== THE NUISANCE TEST: does the ranking survive the look? ===")
    print("  A floor that beats the shipped one at 1.0 but not at 2.0 has been")
    print("  fitted to a parameter the arm does not trade. That is the reason")
    print("  0.50 was refused in config.cross_min_margin_bps; it applies here.")
    a, b = tables[shipped_look], tables[2.0]
    base_a = a.get(shipped_floor)
    base_b = b.get(shipped_floor)
    for f in FLOORS:
        if abs(f - shipped_floor) < 1e-9:
            continue
        sa, sb = a.get(f), b.get(f)
        if not (sa and sb and base_a and base_b):
            continue
        da = sa["per"] - base_a["per"]
        db_ = sb["per"] - base_b["per"]
        agree = "AGREE" if (da > 0) == (db_ > 0) else "FLIPS"
        both = "both halves +" if sa["h1"] > 0 and sa["h2"] > 0 else "a half is -"
        print(f"    floor {f:<5.2f} vs {shipped_floor:.2f}: "
              f"look1.0 {da:+.4f}/sh   look2.0 {db_:+.4f}/sh   "
              f"{agree}   ({both}, {verdict(sa)})")

    print("\n  HOW TO READ: [PASS] needs pooled t>=2 AND day-clustered t>=2 AND")
    print("  both halves positive AND drop-best-day positive. A floor ships only")
    print("  if it PASSES *and* AGREEs across the two looks.")


main()
