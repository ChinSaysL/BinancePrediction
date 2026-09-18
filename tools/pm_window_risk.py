"""THE WINDOW IS THE RISK UNIT, AND THE OLD REFUTATION SCORED THE WRONG ONE.

`config.cross_max_round_stake_mult` records a replay (`tools/pm_window_cap.py`)
that refused a per-window cap: "every dropped cohort is POSITIVE... the stacking
is not the leak, it is where the edge is". That measurement has two properties
that make it unable to see the risk it was asked about:

  1. IT COUNTS FILLS AS INDEPENDENT DRAWS. Nine families cross within seconds of
     each other because ONE macro move pushed btc, eth, sol, doge and xrp across
     their levels at once. Four $1 positions on that move are one bet of $4, not
     four bets of $1 -- and a per-FILL mean cannot tell the difference. The unit
     has to be the WINDOW.
  2. IT PRICES EVERY ENTRY AT THE TRIGGER-INSTANT ASK. The simultaneous fires
     are exactly when every book reprices at once, and the live slippage in
     those windows is 15x the single-entry case (+0.0415 vs +0.0027/share).

So this re-asks it with the window as the unit, on BOTH the tape and the live
fills, and it also asks the question that comes first: **did the margin floor
0.25 -> 0.50 already remove the stacking?** If the entries a window stacks are
the marginal ones, the floor is the cap and no new knob is needed.

    /root/analysis-venv/bin/python tools/pm_window_risk.py
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import math
import os
import sqlite3
import statistics as st
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_cross_replay as base  # noqa: E402

FEE = base.FEE


def wkey(end_ms: int) -> int:
    """The 5-minute BELL the round settles on -- what makes entries one bet."""
    return (int(end_ms) // 1000) // 300 * 300


def battery(vals, label, unit="window"):
    v = np.asarray(vals, dtype=float)
    n = len(v)
    if n == 0:
        print(f"  {label:<30} n=0")
        return
    t = v.mean() / (v.std(ddof=1) / math.sqrt(n)) if n > 1 and v.std(ddof=1) else 0.0
    print(f"  {label:<30} {unit}s={n:4d}  total {v.sum():+8.3f}  "
          f"mean {v.mean():+.4f}  t {t:+5.2f}")


def tape_side(args):
    from btcpredict.config import settings
    look_s, hold_s = base.shipped_look_s(), base.shipped_hold_s()
    px_hi = float(settings.cross_max_fill_px)
    tau_lo, tau_hi = float(settings.cross_min_tau_s), float(settings.cross_max_tau_s)
    r, q = base.load(args.db)
    q = q.copy(); q["fam"] = q.slug.map(base.family_of)
    rows_by_floor = {}
    pres = {}
    for _, row in r.iterrows():
        pre = base.precompute(q, row, look_s, args.px_lo, px_hi, tau_lo, tau_hi)
        if pre is not None:
            pres[row.slug] = (pre, row)
    for floor in (0.25, 0.50):
        es = []
        for slug, (pre, row) in pres.items():
            e = base.pick(pre, signal="fc", floor_bps=floor, look_s=look_s,
                          px_lo=args.px_lo, px_hi=px_hi, tau_lo=tau_lo,
                          tau_hi=tau_hi, hold_s=hold_s)
            if e is not None:
                e = dict(e); e["end_ms"] = int(row.end_ms)
                es.append(e)
        rows_by_floor[floor] = es
    print("=== DID THE FLOOR ALREADY REMOVE THE STACKING? (tape) ===")
    for floor, es in rows_by_floor.items():
        win = collections.defaultdict(list)
        for e in es:
            win[wkey(e["end_ms"])].append(e)
        sizes = collections.Counter(len(v) for v in win.values())
        big = sum(1 for v in win.values() if len(v) >= 3)
        print(f"  floor {floor:.2f}: {len(es):4d} entries in {len(win):4d} windows "
              f"| mean {len(es)/len(win):.2f}/window | 3+ windows: {big:3d} "
              f"({100*big/len(win):.1f}%) | sizes {dict(sorted(sizes.items()))}")
    print("\n=== TAPE, floor 0.50: per-window P&L by how many the window took ===")
    es = rows_by_floor[0.50]
    win = collections.defaultdict(list)
    for e in es:
        win[wkey(e["end_ms"])].append(e)
    byk = collections.defaultdict(list)
    for v in win.values():
        byk[min(len(v), 4)].append(sum(x["per"] for x in v))
    for k in sorted(byk):
        battery(byk[k], f"windows taking {k}{'+' if k == 4 else ''} entr"
                        f"{'y' if k == 1 else 'ies'}")
    print("\n=== TAPE: a cap on entries per WINDOW (keep the first k) ===")
    allw = sorted(win.items())
    for cap in (99, 1, 2, 3):
        tot, per_w = 0.0, []
        for _, v in allw:
            vv = sorted(v, key=lambda x: x["ts_ms"])[:cap]
            s = sum(x["per"] for x in vv)
            tot += s; per_w.append(s)
        battery(per_w, f"cap {cap if cap < 99 else 'none':>4}/window")


def live_side(args):
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    d = pd.read_sql_query("""
      select f.id, f.slug, f.ts_ms, f.side, f.shares, s.pnl, r.end_ms
      from fills f join settlements s on s.fill_id=f.id join rounds r on r.slug=f.slug
      where f.mode in ('cross','train') and s.void=0 and s.pnl is not null
        and r.winner in ('Up','Down') and f.ts_ms >= ?
      order by f.ts_ms""", con, params=(int(dt.datetime(2026, 9, 16,
                                       tzinfo=dt.timezone.utc).timestamp()*1000),))
    con.close()
    if d.empty:
        print("\nno live fills"); return
    win = collections.defaultdict(list)
    for _, f in d.iterrows():
        win[wkey(f.end_ms)].append(f)
    print(f"\n=== LIVE: {len(d)} settled fills in {len(win)} windows ===")
    byk = collections.defaultdict(list)
    for v in win.values():
        byk[min(len(v), 3)].append(sum(float(x.pnl) for x in v))
    for k in sorted(byk):
        battery(byk[k], f"windows taking {k}{'+' if k == 3 else ''} entr"
                        f"{'y' if k == 1 else 'ies'}")
    print("\n=== LIVE: what a per-window cap would have banked ===")
    for cap in (99, 1, 2, 3):
        per_w = []
        for _, v in win.items():
            vv = sorted(v, key=lambda x: x.ts_ms)[:cap]
            per_w.append(sum(float(x.pnl) for x in vv))
        battery(per_w, f"cap {cap if cap < 99 else 'none':>4}/window")
    print("\n=== LIVE: the worst windows, and what they had in common ===")
    ws = sorted(((sum(float(x.pnl) for x in v), k, v) for k, v in win.items()))
    for tot, k, v in ws[:6]:
        fams = ",".join(sorted({str(x.slug).split("-")[0] for x in v}))
        sides = collections.Counter(str(x.side) for x in v)
        print(f"  bell {dt.datetime.fromtimestamp(k, dt.timezone.utc):%m-%d %H:%M}  "
              f"n={len(v)}  pnl {tot:+7.3f}  sides {dict(sides)}  {fams}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default=base.DB)
    ap.add_argument("--px-lo", type=float, default=0.10)
    args = ap.parse_args()
    tape_side(args)
    live_side(args)


main()
