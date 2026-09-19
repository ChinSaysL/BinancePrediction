"""MAKER vs TAKER on the ENTRY. The one lever big enough to close the gap.

THE ARITHMETIC THAT MOTIVATES IT. The live record pays an average 0.599 and
needs 61.1% to break even, against a 54.4% actual -- a 6.7 point gap, of which
3.7 points survive inside the shipped [0.45,0.65] band. Taking with FAK pays the
SPREAD plus the taker fee on every entry; this repo measured the MAKER fee at
$0.00. At a 1-2c spread that is worth ~2-3 points from arithmetic alone, with no
better signal required.

THE CATCH, and it is the whole question: a resting bid fills when someone
crosses INTO it, which is exactly when the market is moving against you. So the
fills a maker gets are adversely selected, and the test is not "does it fill
cheaper" -- it obviously does -- but **do maker fills win often enough to beat
paying up.** Both numbers are printed.

THE FILL MODEL, stated because it is the weak point: we post at the bid at the
decision instant and count it FILLED if the best ask later touches our price
inside the round's remaining tau window -- i.e. somebody was willing to sell
there. That is a PROXY. It cannot see queue position, so it is OPTIMISTIC: a
real resting order may sit behind size that absorbs the seller first. Read the
fill rate as an upper bound.

    /root/analysis-venv/bin/python tools/pm_maker_entry.py
"""
from __future__ import annotations

import argparse
import collections
import math
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_cross_replay as base  # noqa: E402

FEE = base.FEE


def series(db, slugs):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = collections.defaultdict(list)
    qm = ",".join("?" * len(slugs))
    for r in con.execute(f"""select slug,ts_ms,tau_s,up_bid,down_bid,up_ask,down_ask
                             from quotes where slug in ({qm}) and tau_s is not null
                             order by ts_ms""", list(slugs)):
        out[r["slug"]].append((int(r["ts_ms"]), float(r["tau_s"]),
                               r["up_bid"], r["down_bid"], r["up_ask"], r["down_ask"]))
    con.close()
    return out


def maker_fill(ser, e_ts, side, post, min_tau):
    """Did a resting buy at `post` get hit before the window closed?"""
    for ts, tau, ub, db, ua, da in ser:
        if ts <= e_ts or tau <= min_tau:
            continue
        a = ua if side == "Up" else da
        if a is None:
            continue
        if float(a) <= post + 1e-9:
            return ts, tau
    return None


def battery(rows, label):
    if not rows:
        print(f"  {label:<30} n=0")
        return
    d = pd.DataFrame(rows)
    v = d.per.to_numpy()
    n = len(v)
    t = v.mean() / (v.std(ddof=1) / math.sqrt(n)) if n > 1 and v.std(ddof=1) else 0.0
    dm = d.groupby("day").per.mean().to_numpy()
    dt_ = (dm.mean() / (dm.std(ddof=1) / math.sqrt(len(dm)))
           if len(dm) > 1 and dm.std(ddof=1) else 0.0)
    h = n // 2
    print(f"  {label:<30} n={n:4d} win={d.won.mean():5.1%} px={d.px.mean():.3f} "
          f"per={v.mean():+.4f} tot={v.sum():+7.2f} t={t:+5.2f} day-t={dt_:+5.2f} "
          f"halves={v[:h].mean():+.3f}/{v[h:].mean():+.3f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default=base.DB)
    ap.add_argument("--min-tau", type=float, default=8.0)
    args = ap.parse_args()
    from btcpredict.config import settings as S
    look_s, hold_s = base.shipped_look_s(), base.shipped_hold_s()
    floor = float(S.cross_min_margin_bps)
    px_lo, px_hi = float(S.cross_min_price), float(S.cross_max_fill_px)
    tau_lo, tau_hi = float(S.cross_min_tau_s), float(S.cross_max_tau_s)
    r, q = base.load(args.db)
    q = q.copy(); q["fam"] = q.slug.map(base.family_of)
    ents = []
    for _, row in r.iterrows():
        pre = base.precompute(q, row, look_s, px_lo, px_hi, tau_lo, tau_hi)
        if pre is None:
            continue
        e = base.pick(pre, signal="fc", floor_bps=floor, look_s=look_s,
                      px_lo=px_lo, px_hi=px_hi, tau_lo=tau_lo, tau_hi=tau_hi,
                      hold_s=hold_s)
        if e is not None:
            ents.append(e)
    ser = series(args.db, {e["slug"] for e in ents})
    print(f"entries {len(ents)} | band [{px_lo:.2f},{px_hi:.2f}] "
          f"tau [{tau_lo:.0f},{tau_hi:.0f}] floor {floor} bps")

    # taker baseline
    tk = [dict(day=e["day"], won=e["won"], px=e["ask"],
               per=(1.0 if e["won"] else 0.0) - e["ask"] - FEE(e["ask"]))
          for e in ents]
    print("\n=== TAKER (what ships today) ===")
    battery(tk, "take at the ask + fee")

    print("\n=== MAKER: post and wait. Maker fee = $0.00 ===")
    for tick_back in (0, 1, 2):
        rows, nofill = [], 0
        for e in ents:
            s = ser.get(e["slug"], [])
            # the touch at the decision instant
            cur = None
            for ts, tau, ub, db, ua, da in s:
                if ts >= int(e["ts_ms"]):
                    cur = (ub if e["side"] == "Up" else db,
                           ua if e["side"] == "Up" else da)
                    break
            if not cur or cur[0] is None:
                nofill += 1
                continue
            post = round(float(cur[0]) - 0.01 * tick_back, 2)
            if not (px_lo <= post <= px_hi):
                nofill += 1
                continue
            g = maker_fill(s, int(e["ts_ms"]), e["side"], post, args.min_tau)
            if g is None:
                nofill += 1
                continue
            rows.append(dict(day=e["day"], won=e["won"], px=post,
                             per=(1.0 if e["won"] else 0.0) - post))
        lab = "post at the bid" if tick_back == 0 else f"post {tick_back} tick(s) under"
        battery(rows, lab)
        print(f"     fill rate {len(rows)}/{len(ents)} = {100*len(rows)/len(ents):.0f}%"
              f"   (never filled: {nofill})")

    print("\n=== THE ADVERSE-SELECTION CHECK ===")
    print("  Same entries, taker vs maker, on the SUBSET the maker would fill.")
    filled = {}
    for e in ents:
        s = ser.get(e["slug"], [])
        cur = None
        for ts, tau, ub, db, ua, da in s:
            if ts >= int(e["ts_ms"]):
                cur = (ub if e["side"] == "Up" else db,)
                break
        if not cur or cur[0] is None:
            continue
        post = round(float(cur[0]), 2)
        if not (px_lo <= post <= px_hi):
            continue
        if maker_fill(s, int(e["ts_ms"]), e["side"], post, args.min_tau):
            filled[id(e)] = post
    sub_t = [x for e, x in zip(ents, tk) if id(e) in filled]
    sub_m = [dict(day=e["day"], won=e["won"], px=filled[id(e)],
                  per=(1.0 if e["won"] else 0.0) - filled[id(e)])
             for e in ents if id(e) in filled]
    battery(sub_t, "  those entries, TAKEN")
    battery(sub_m, "  those entries, MADE")
    miss = [dict(day=e["day"], won=e["won"], px=e["ask"],
                 per=(1.0 if e["won"] else 0.0) - e["ask"] - FEE(e["ask"]))
            for e in ents if id(e) not in filled]
    battery(miss, "  the ones a maker MISSES")


main()
