"""IS SELLING A LOSING POSITION +EV? The low half of the calibration curve.

The take-profit run measured the HIGH end: a position whose bid touches 0.85
goes on to win 91.1% (tape) / 94.8% (live). The venue is UNDER-confident there,
which is why taking profit loses.

If that under-confidence is symmetric, then for a LOSING position the venue's
bid sits ABOVE the true win probability -- and selling into it would be +EV,
which is the opposite of what every exit sweep found. This resolves that.

    P(win | the bid DROPPED to X) against X
      P(win) <  X  ->  the bid is generous: SELLING THERE PAYS
      P(win) >= X  ->  the bid is stingy: selling hands money away

Unconditional on any signal -- it asks only whether the venue's price is a fair
probability on the way DOWN. If it is not, an exit can be built on the PRICE
alone and needs no flip detector at all.

    /root/analysis-venv/bin/python tools/pm_exit_calibration.py
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_cross_replay as base  # noqa: E402

FEE = base.FEE
LOWS = (0.50, 0.45, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15)


def bid_path(pre, e_ts, held, min_tau):
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
    q = q.copy(); q["fam"] = q.slug.map(base.family_of)

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
        b = bid_path(pre, int(e["ts_ms"]), e["side"], args.min_tau)
        if b:
            tape.append(dict(won=e["won"], ask=e["ask"], mn=min(b)))
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
        b = bid_path(pre, int(f.ts_ms), f.side, args.min_tau)
        if not b:
            continue
        sh = float(f.shares or 0)
        live.append(dict(won=bool(f.winner == f.side), sh=sh,
                         cost=float(f.stake or 0) + float(f.fee_paid or 0),
                         mn=min(b)))
    print(f"TAPE {len(tape)} | LIVE {len(live)} | gates px "
          f"[{px_lo:.2f},{px_hi:.2f}] tau [{tau_lo:.0f},{tau_hi:.0f}]")
    print("\n=== P(win | the bid DROPPED to X) vs X ===")
    print("  P(win) BELOW X means the bid is generous and selling there PAYS.")
    print(f"  {'X':>6} | {'TAPE n':>7} {'P(win)':>7} {'edge':>8} | "
          f"{'LIVE n':>7} {'P(win)':>7} {'edge':>8}")
    for X in LOWS:
        t = [x for x in tape if x["mn"] <= X]
        l = [x for x in live if x["mn"] <= X]
        tp = (sum(x["won"] for x in t) / len(t)) if t else float("nan")
        lp = (sum(x["won"] for x in l) / len(l)) if l else float("nan")
        print(f"  {X:>6.2f} | {len(t):>7d} {tp:>7.1%} {tp-X:>+8.1%} | "
              f"{len(l):>7d} {lp:>7.1%} {lp-X:>+8.1%}")
    print("\n=== the P&L of a pure PRICE stop (no flip detector at all) ===")
    bt = sum((1.0 if x["won"] else 0.0) - x["ask"] - FEE(x["ask"]) for x in tape)
    bl = sum((x["sh"] if x["won"] else 0.0) - x["cost"] for x in live)
    print(f"  {'no stop':<20} TAPE tot={bt:+7.2f}           | LIVE tot={bl:+7.3f}")
    for X in LOWS:
        tt, n = 0.0, 0
        for x in tape:
            if x["mn"] <= X:
                n += 1
                tt += X - FEE(X) - x["ask"] - FEE(x["ask"])
            else:
                tt += (1.0 if x["won"] else 0.0) - x["ask"] - FEE(x["ask"])
        lt, ln, lw = 0.0, 0, 0
        for x in live:
            if x["mn"] <= X:
                ln += 1
                lt += x["sh"] * (X - FEE(X)) - x["cost"]
                lw += x["won"]
            else:
                lt += (x["sh"] if x["won"] else 0.0) - x["cost"]
        print(f"  stop at {X:.2f}{'':<11} TAPE tot={tt:+7.2f} ({tt-bt:+6.2f}) n={n:3d} "
              f"| LIVE tot={lt:+7.3f} ({lt-bl:+6.3f}) n={ln:2d}, {lw} would have won")


main()
