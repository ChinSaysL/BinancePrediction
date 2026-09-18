"""THE FULL EXIT GRID, on the rule `_cross_exit_check` ACTUALLY RUNS.

Every earlier exit sweep in this repo is void: they were measured on walks that
(a) booked the exit at the tick after the ENTRY, (b) required a SECOND crossing
to fire, or (c) demanded the margin floor AT THE CROSSING -- which is
zero-margin by construction, so nothing ever armed. This is an exact port of
the shipped rule:

    arm    on a CROSSING into the other side (no floor -- a crossing is
           zero-margin by construction)
    fire   when the STATE is still on the other side, past `min_margin_bps`,
           and the flip has persisted `confirm_s`
    disarm when the STATE returns to our side past the floor
    never  inside `cross_exit_min_tau_s` of the bell

Swept on BOTH the tape and the live fills, under TODAY's entry gates. Columns:
  cut nL / mW   losers cut (value saved) / winners cut (value given up)
  x             ratio of the two -- under ~1.5 the rule is a coin flip that
                pays two spreads
  be            per-share fill degradation the cell absorbs before its gain is
                gone. The replay prices exits at `1 - other_ask`; the real fill
                is the bid, ~1c away. A cell at be<2c is not real.

    /root/analysis-venv/bin/python tools/pm_exit_sweep.py
"""
from __future__ import annotations

import argparse
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


def walk(pre, e_ts, held, *, tau_gate, confirm_s, floor, look_s, hold_s):
    """Exact port of Bot._cross_exit_check."""
    other = "Down" if held == "Up" else "Up"
    src, src_ts, level = pre["fc"], pre["fc_ts"], pre["level"]
    if not src or not level:
        return None
    rows = pre["rows"]
    tail = max(10, int((hold_s + look_s + 2.0) * 1.6) + 6)
    armed = None
    for ts, tau, ua, da in zip(rows.ts_ms, rows.tau_s, rows.up_ask, rows.down_ask):
        ts = int(ts)
        tau = float(tau)
        if ts <= e_ts:
            continue
        if tau <= tau_gate:
            break
        j = int(np.searchsorted(src_ts, ts, side="right"))
        if j < 4:
            continue
        side = base.crossed_side(src[max(0, j - tail):j], level, look_s,
                                 hold_s=hold_s)
        m = 1e4 * (src[j - 1][1] - level) / level
        side_now = "Up" if m > 0 else "Down"
        past = (floor <= 0) or (abs(m) >= floor)
        if side_now == held and past:
            armed = None
            continue
        if armed is None:
            if side is not None and side != held:
                armed = ts
            continue
        if side_now == held or not past:
            continue
        if (ts - armed) / 1000.0 < confirm_s:
            continue
        opp = da if held == "Up" else ua
        if not opp or not (0.0 < float(opp) < 1.0):
            continue
        px = 1.0 - float(opp)
        if 0.0 < px < 1.0:
            return dict(px=px, tau=tau, lag=(ts - e_ts) / 1000.0)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default=base.DB)
    args = ap.parse_args()
    from btcpredict.config import settings as S
    look_s, hold_s = base.shipped_look_s(), base.shipped_hold_s()
    e_floor = float(S.cross_min_margin_bps)
    px_lo, px_hi = float(S.cross_min_price), float(S.cross_max_fill_px)
    tau_lo, tau_hi = float(S.cross_min_tau_s), float(S.cross_max_tau_s)

    r, q = base.load(args.db)
    q = q.copy()
    q["fam"] = q.slug.map(base.family_of)

    # ---- tape cohort, cached ------------------------------------------
    ents = []
    for _, row in r.iterrows():
        pre = base.precompute(q, row, look_s, px_lo, px_hi, tau_lo, tau_hi)
        if pre is None:
            continue
        e = base.pick(pre, signal="fc", floor_bps=e_floor, look_s=look_s,
                      px_lo=px_lo, px_hi=px_hi, tau_lo=tau_lo, tau_hi=tau_hi,
                      hold_s=hold_s)
        if e is not None:
            ents.append((pre, e))

    # ---- live cohort, cached ------------------------------------------
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    d = pd.read_sql_query("""
      select f.slug,f.ts_ms,f.side,f.shares,f.stake,f.fee_paid,s.pnl,rr.winner
      from fills f join settlements s on s.fill_id=f.id join rounds rr on rr.slug=f.slug
      where f.mode in ('cross','train') and s.void=0 and s.pnl is not null
        and rr.winner in ('Up','Down') and f.filled_px between ? and ?
      order by f.ts_ms""", con, params=(px_lo, px_hi))
    rounds = {row.slug: row for _, row in r.iterrows()}
    live = []
    for _, f in d.iterrows():
        row = rounds.get(f.slug)
        if row is None:
            continue
        pre = base.precompute(q, row, look_s, 0.0, 1.0, 0.0, 1e9)
        if pre is None or not pre["fc"] or not pre["level"]:
            continue
        sh = float(f.shares or 0)
        cost = float(f.stake or 0) + float(f.fee_paid or 0)
        won = bool(f.winner == f.side)
        live.append((pre, int(f.ts_ms), f.side, sh, cost, won,
                     (sh if won else 0.0) - cost))
    print(f"TAPE entries {len(ents)} | LIVE fills {len(live)} "
          f"| gates px [{px_lo:.2f},{px_hi:.2f}] tau [{tau_lo:.0f},{tau_hi:.0f}] "
          f"entry floor {e_floor}")

    def row(lab, tg, cs, fl, base_tape, base_live):
        # tape
        per, cw, cl, sw, sl, n = [], 0, 0, 0.0, 0.0, 0
        for pre, e in ents:
            hold = (1.0 if e["won"] else 0.0) - e["ask"] - FEE(e["ask"])
            g = walk(pre, int(e["ts_ms"]), e["side"], tau_gate=tg,
                     confirm_s=cs, floor=fl, look_s=look_s, hold_s=hold_s)
            if g is None:
                per.append(hold)
                continue
            n += 1
            v = g["px"] - FEE(g["px"]) - e["ask"] - FEE(e["ask"])
            per.append(v)
            if e["won"]:
                cw += 1; sw += v - hold
            else:
                cl += 1; sl += v - hold
        v = np.array(per)
        t = v.mean() / (v.std(ddof=1) / math.sqrt(len(v))) if v.std(ddof=1) else 0.0
        be = ((v.sum() - base_tape) / n) if n else float("nan")
        x = (sl / abs(sw)) if sw else float("inf")
        # live
        lt, ln, lcw, lcl, lsw, lsl = 0.0, 0, 0, 0, 0.0, 0.0
        for pre, ts, side, sh, cost, won, hold in live:
            g = walk(pre, ts, side, tau_gate=tg, confirm_s=cs, floor=fl,
                     look_s=look_s, hold_s=hold_s)
            if g is None:
                lt += hold
                continue
            ln += 1
            pnl = sh * (g["px"] - FEE(g["px"])) - cost
            lt += pnl
            if won:
                lcw += 1; lsw += pnl - hold
            else:
                lcl += 1; lsl += pnl - hold
        lx = (lsl / abs(lsw)) if lsw else float("inf")
        print(f"  {lab:<26} TAPE ex={n:3d} tot={v.sum():+7.2f} t={t:+5.2f} "
              f"{cl:3d}L/{cw:3d}W x{x:4.2f} be={be*100:5.1f}c | "
              f"LIVE ex={ln:2d} tot={lt:+7.3f} ({lt-base_live:+6.3f}) "
              f"{lcl:2d}L/{lcw:2d}W x{lx:4.2f}")

    bt = sum((1.0 if e["won"] else 0.0) - e["ask"] - FEE(e["ask"]) for _, e in ents)
    bl = sum(h for *_, h in live)
    print(f"\n  BASELINE (no exit)          TAPE tot={bt:+7.2f}          "
          f"            | LIVE tot={bl:+7.3f}\n")
    print("=== the exit's TAU gate (confirm 3s, floor = the entry's) ===")
    for tg in (8, 20, 30, 45, 60, 90):
        row(f"tau>={tg}s", tg, 3.0, e_floor, bt, bl)
    print("\n=== CONFIRM_S (tau>=8, floor = the entry's) ===")
    for cs in (0.0, 1.0, 3.0, 5.0, 10.0):
        row(f"confirm={cs:.0f}s", 8.0, cs, e_floor, bt, bl)
    print("\n=== the EXIT's OWN margin floor (tau>=8, confirm 3s) ===")
    for fl in (0.0, e_floor, 1.0, 2.0, 3.0):
        row(f"exit floor={fl:.2f}bps", 8.0, 3.0, fl, bt, bl)
    print("\n=== COMBINATIONS ===")
    for tg, cs, fl in ((60, 3.0, e_floor), (60, 5.0, e_floor), (45, 3.0, 1.0),
                       (60, 3.0, 1.0), (30, 5.0, 1.0), (60, 10.0, e_floor)):
        row(f"tau>={tg} c={cs:.0f} fl={fl:.2f}", tg, cs, fl, bt, bl)


main()
