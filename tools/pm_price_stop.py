"""A PURE PRICE STOP, priced at the REAL recorded bid. No flip detector.

THREE CHANGES from every exit measurement before it:
  1. NO SIGNAL. It never asks `crossed_side` anything. Every exit failure in
     this repo came from the detector -- arming bugs, zero-margin crossings,
     confirmations that could not fire. A price stop has none of that surface.
  2. THE REAL BID. `quotes.up_bid` / `down_bid` are recorded and every earlier
     exit tool ignored them, pricing at `1 - other_ask` instead. On a ~3-point
     edge that ~1c assumption is a third of the result, so it is the difference
     between an edge and a rounding error. Both are printed side by side.
  3. THE THRESHOLD IS THE WHOLE RULE: sell when our side's bid touches X.

WHY IT MIGHT WORK WHERE THE FLIP DID NOT. `pm_exit_calibration.py` measured
P(win | the bid dropped to X) against X: 25.4% at X=0.30 on the tape, i.e. the
bid is generous by ~4.6 points there, against a ~1.5 point taker fee. The flip
exit never had that -- its loser/winner value ratio sat at 0.77-1.13, a coin
flip. This asks only whether the venue's price is fair on the way down.

    /root/analysis-venv/bin/python tools/pm_price_stop.py
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
STOPS = (0.50, 0.45, 0.42, 0.40, 0.38, 0.35, 0.32, 0.30, 0.25, 0.20)


def load_bids(db, slugs):
    """slug -> [(ts, tau, up_bid, down_bid, up_ask, down_ask)], the REAL book."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = collections.defaultdict(list)
    qmarks = ",".join("?" * len(slugs))
    for r in con.execute(f"""select slug,ts_ms,tau_s,up_bid,down_bid,up_ask,down_ask
                             from quotes where slug in ({qmarks}) and tau_s is not null
                             order by ts_ms""", list(slugs)):
        out[r["slug"]].append((int(r["ts_ms"]), float(r["tau_s"]),
                               r["up_bid"], r["down_bid"],
                               r["up_ask"], r["down_ask"]))
    con.close()
    return out


def first_touch(series, e_ts, held, X, min_tau):
    """First tick at or below X. Returns (real_bid, derived_bid) or None."""
    for ts, tau, ub, db, ua, da in series:
        if ts <= e_ts or tau <= min_tau:
            continue
        b = ub if held == "Up" else db
        if b is None:
            continue
        b = float(b)
        if not (0.0 < b < 1.0) or b > X:
            continue
        opp = da if held == "Up" else ua
        der = (1.0 - float(opp)) if opp is not None else None
        return b, der
    return None


def battery(rows, label, base_tot):
    d = pd.DataFrame(rows)
    v = d.per.to_numpy()
    n = len(v)
    t = v.mean() / (v.std(ddof=1) / math.sqrt(n)) if n > 1 and v.std(ddof=1) else 0.0
    dm = d.groupby("day").per.mean().to_numpy()
    day_t = (dm.mean() / (dm.std(ddof=1) / math.sqrt(len(dm)))
             if len(dm) > 1 and dm.std(ddof=1) else 0.0)
    h = n // 2
    best = d.groupby("day").per.mean().idxmax()
    drop = d[d.day != best].per.mean()
    ok = (t >= 2 and day_t >= 2 and v[:h].mean() > 0 and v[h:].mean() > 0 and drop > 0)
    print(f"  {label:<24} tot={v.sum():+7.2f} ({v.sum()-base_tot:+6.2f}) "
          f"per={v.mean():+.4f} t={t:+5.2f} day-t={day_t:+5.2f} "
          f"halves={v[:h].mean():+.3f}/{v[h:].mean():+.3f} [{'PASS' if ok else 'fail'}]")


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
    bids = load_bids(args.db, {e["slug"] for e in ents})

    # how wrong was the 1-other_ask assumption?
    gaps = []
    for e in ents:
        g = first_touch(bids.get(e["slug"], []), int(e["ts_ms"]), e["side"],
                        0.40, args.min_tau)
        if g and g[1] is not None:
            gaps.append(g[0] - g[1])
    print(f"TAPE entries {len(ents)} | gates px [{px_lo:.2f},{px_hi:.2f}] "
          f"tau [{tau_lo:.0f},{tau_hi:.0f}]")
    if gaps:
        print(f"\n  REAL bid minus the `1 - other_ask` every earlier tool used:"
              f"  median {np.median(gaps):+.4f}  mean {np.mean(gaps):+.4f}  "
              f"n={len(gaps)}")

    base_rows = [dict(day=e["day"],
                      per=(1.0 if e["won"] else 0.0) - e["ask"] - FEE(e["ask"]))
                 for e in ents]
    bt = sum(x["per"] for x in base_rows)
    print("\n=== TAPE: a pure price stop, at the REAL bid ===")
    battery(base_rows, "no stop", bt)
    for X in STOPS:
        rows, n = [], 0
        for e in ents:
            g = first_touch(bids.get(e["slug"], []), int(e["ts_ms"]), e["side"],
                            X, args.min_tau)
            if g is None:
                rows.append(dict(day=e["day"],
                                 per=(1.0 if e["won"] else 0.0) - e["ask"] - FEE(e["ask"])))
            else:
                n += 1
                b = g[0]
                rows.append(dict(day=e["day"],
                                 per=b - FEE(b) - e["ask"] - FEE(e["ask"])))
        battery(rows, f"stop {X:.2f}  [{n} stops]", bt)

    # ---- LIVE -------------------------------------------------------
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    d = pd.read_sql_query("""
      select f.slug,f.ts_ms,f.side,f.shares,f.stake,f.fee_paid,rr.winner
      from fills f join settlements s on s.fill_id=f.id join rounds rr on rr.slug=f.slug
      where f.mode in ('cross','train') and s.void=0 and s.pnl is not null
        and rr.winner in ('Up','Down') and f.filled_px between ? and ?
      order by f.ts_ms""", con, params=(px_lo, px_hi))
    lb = load_bids(args.db, set(d.slug))
    print(f"\n=== LIVE fills {len(d)} ===")
    bl = sum((float(f.shares or 0) if f.winner == f.side else 0.0)
             - (float(f.stake or 0) + float(f.fee_paid or 0)) for _, f in d.iterrows())
    print(f"  {'no stop':<24} tot={bl:+7.3f}")
    for X in STOPS:
        tot, n, wl = 0.0, 0, 0
        for _, f in d.iterrows():
            sh = float(f.shares or 0)
            cost = float(f.stake or 0) + float(f.fee_paid or 0)
            won = (f.winner == f.side)
            g = first_touch(lb.get(f.slug, []), int(f.ts_ms), f.side, X, args.min_tau)
            if g is None:
                tot += (sh if won else 0.0) - cost
            else:
                n += 1
                wl += won
                b = g[0]
                tot += sh * (b - FEE(b)) - cost
        print(f"  stop {X:.2f}  [{n:3d} stops]      tot={tot:+7.3f} ({tot-bl:+6.3f})"
              f"   {wl} of them would have won")


main()
