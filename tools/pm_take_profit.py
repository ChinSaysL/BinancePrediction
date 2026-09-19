"""TAKE-PROFIT: sell when the position's own bid reaches X. Does it pay?

THE THEORY, and why this is NOT the flip exit. Selling at a bid of X banks X;
holding pays 1.0 with probability P(win). So a take-profit is EV-NEGATIVE BY THE
SPREAD **if the venue's price is a fair probability** -- you give up (P - X) and
pay a taker fee to do it.

It only pays if the venue is OVER-CONFIDENT high up: if positions whose bid
touches 0.85 go on to win LESS than 85% of the time. This repo has a prior that
says they might -- README: "Deep favourites are a trap ... measured top-bucket
reliability is 0.964 against 0.973 predicted."

So the whole question is one calibration curve: **P(win | the bid touched X)
against X.** Everything else follows from it. Both are printed, on the tape and
on the live fills, under today's entry gates.

    /root/analysis-venv/bin/python tools/pm_take_profit.py
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
LEVELS = (0.70, 0.75, 0.80, 0.85, 0.90, 0.95)


def path(pre, e_ts, held, min_tau):
    """The bid series for OUR side after the entry.

    Priced as `1 - other_ask`, the complementary identity -- the same
    convention the exit replay uses, and the number the bot would actually be
    able to hit. The replay's loader carries only the asks.
    """
    rows = pre["rows"]
    out = []
    for ts, tau, ua, da in zip(rows.ts_ms, rows.tau_s, rows.up_ask, rows.down_ask):
        ts = int(ts)
        if ts <= e_ts or float(tau) <= min_tau:
            continue
        opp = da if held == "Up" else ua
        if opp is None:
            continue
        b = 1.0 - float(opp)
        if 0.0 < b < 1.0:
            out.append(b)
    return out


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
    q = q.copy()
    q["fam"] = q.slug.map(base.family_of)

    tape = []
    for _, row in r.iterrows():
        pre = base.precompute(q, row, look_s, px_lo, px_hi, tau_lo, tau_hi)
        if pre is None:
            continue
        e = base.pick(pre, signal="fc", floor_bps=floor, look_s=look_s,
                      px_lo=px_lo, px_hi=px_hi, tau_lo=tau_lo, tau_hi=tau_hi,
                      hold_s=hold_s)
        if e is None:
            continue
        bids = path(pre, int(e["ts_ms"]), e["side"], args.min_tau)
        if bids:
            tape.append(dict(won=e["won"], ask=e["ask"], mx=max(bids),
                             bids=bids, day=e["day"]))

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    d = pd.read_sql_query("""
      select f.slug,f.ts_ms,f.side,f.shares,f.stake,f.fee_paid,rr.winner
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
        if pre is None:
            continue
        bids = path(pre, int(f.ts_ms), f.side, args.min_tau)
        if not bids:
            continue
        sh = float(f.shares or 0)
        cost = float(f.stake or 0) + float(f.fee_paid or 0)
        live.append(dict(won=bool(f.winner == f.side), sh=sh, cost=cost,
                         mx=max(bids), bids=bids))
    print(f"TAPE entries {len(tape)} | LIVE fills {len(live)} | gates "
          f"px [{px_lo:.2f},{px_hi:.2f}] tau [{tau_lo:.0f},{tau_hi:.0f}]")

    print("\n=== THE CALIBRATION CURVE -- the whole question ===")
    print("  P(win | the bid TOUCHED X) against X. Below X = the venue is")
    print("  over-confident there and a take-profit pays; at or above X it")
    print("  cannot, because you give up (P - X) plus a fee.")
    print(f"  {'X':>6} | {'TAPE n':>7} {'P(win)':>7} {'edge':>7} | "
          f"{'LIVE n':>7} {'P(win)':>7} {'edge':>7}")
    for X in LEVELS:
        t = [x for x in tape if x["mx"] >= X]
        l = [x for x in live if x["mx"] >= X]
        tp = (sum(x["won"] for x in t) / len(t)) if t else float("nan")
        lp = (sum(x["won"] for x in l) / len(l)) if l else float("nan")
        print(f"  {X:>6.2f} | {len(t):>7d} {tp:>7.1%} {tp-X:>+7.1%} | "
              f"{len(l):>7d} {lp:>7.1%} {lp-X:>+7.1%}")

    print("\n=== and the P&L of actually doing it ===")
    bt = sum((1.0 if x["won"] else 0.0) - x["ask"] - FEE(x["ask"]) for x in tape)
    bl = sum((x["sh"] if x["won"] else 0.0) - x["cost"] for x in live)
    print(f"  {'no take-profit':<22} TAPE tot={bt:+7.2f}            "
          f"| LIVE tot={bl:+7.3f}")
    for X in LEVELS:
        tt, n = 0.0, 0
        for x in tape:
            if x["mx"] >= X:
                n += 1
                tt += X - FEE(X) - x["ask"] - FEE(x["ask"])
            else:
                tt += (1.0 if x["won"] else 0.0) - x["ask"] - FEE(x["ask"])
        lt, ln, cw = 0.0, 0, 0
        for x in live:
            if x["mx"] >= X:
                ln += 1
                lt += x["sh"] * (X - FEE(X)) - x["cost"]
                if x["won"]:
                    cw += 1
            else:
                lt += (x["sh"] if x["won"] else 0.0) - x["cost"]
        print(f"  TP at {X:.2f}{'':<15} TAPE tot={tt:+7.2f} ({tt-bt:+6.2f}) n={n:3d} "
              f"| LIVE tot={lt:+7.3f} ({lt-bl:+6.3f}) n={ln:2d}, {cw} of them won")


main()
