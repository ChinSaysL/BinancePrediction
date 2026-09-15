#!/usr/bin/env python3
"""Round-start ("strike-pegging / initialization lag") test on the LIVE venue DB.

THE CLAIM (third-party idea #1): a 5-minute contract initializes every 300 s; the
makers' books take 1-4 s to spin up, so early in the round the quote is still
~0.50 while the spot has ALREADY moved off the strike. Buy the side the spot has
moved toward.

This is the live half, and it is the half that decides. The April-May corpus does
show an apparent edge here, but the corpus's round-start `*_sell_px` does not
agree with what the live venue actually quotes -- see FINDINGS. Live quotes
govern, because they are what we could have paid.

Method: one row per ROUND (never per quote -- row inflation inflates day-t).
  dist_bps = (spot - strike) / strike * 1e4      (both columns are in the DB)
  A        = the LEADER  (the side the spot moved toward)
  B        = the LAGGARD
  edge     = (1 if that side won else 0) - ask - fee      PER SHARE
  fee      = --fee-bps of the price paid (venue charges ~96 bps of stake)

Both sides are always reported together, because for a binary pair
  edge_A + edge_B = 1 - ask_A - ask_B - fees
so a positive A is only possible if the two asks are jointly cheap. That identity
is the control: it is why "buy the leader" and "fade the leader" cannot both win.

Read-only.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import sqlite3
import statistics as st

import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict_vps.db")
    ap.add_argument("--slug-like", default="btc-updown-5m%")
    ap.add_argument("--tau-lo", type=float, default=294.0)
    ap.add_argument("--tau-hi", type=float, default=300.0)
    ap.add_argument("--fee-bps", type=float, default=96.0,
                    help="venue fee in bps OF STAKE (measured mean 95.9); "
                         "per share that is fee_bps/1e4 * price")
    args = ap.parse_args()

    c = sqlite3.connect(args.db)
    c.row_factory = sqlite3.Row
    rows = c.execute("""
        SELECT r.slug, r.start_ms, r.strike, r.winner,
               q.up_ask, q.down_ask, q.up_bid, q.down_bid, q.spot, q.tau_s
        FROM rounds r JOIN quotes q ON q.slug = r.slug
        WHERE r.winner IS NOT NULL AND r.slug LIKE ?
          AND q.tau_s BETWEEN ? AND ? AND q.up_ask IS NOT NULL
          AND q.down_ask IS NOT NULL AND r.strike IS NOT NULL
          AND q.spot IS NOT NULL
    """, (args.slug_like, args.tau_lo, args.tau_hi)).fetchall()

    # one row per round: the quote CLOSEST to the round open in the window
    best: dict[str, sqlite3.Row] = {}
    for r in rows:
        k = r["slug"]
        if k not in best or r["tau_s"] > best[k]["tau_s"]:
            best[k] = r

    fee = args.fee_bps / 1e4
    recs = []
    for r in best.values():
        d = (r["spot"] - r["strike"]) / r["strike"] * 1e4
        lead_up = d > 0
        pa = r["up_ask"] if lead_up else r["down_ask"]
        pb = r["down_ask"] if lead_up else r["up_ask"]
        ba = (r["up_bid"] if lead_up else r["down_bid"]) or 0.0
        won_a = (r["winner"] == "Up") if lead_up else (r["winner"] == "Down")
        recs.append(dict(
            day=dt.datetime.fromtimestamp(int(r["start_ms"]) / 1000, dt.UTC).strftime("%m-%d"),
            d=abs(d), pa=pa, pb=pb, wa=1.0 if won_a else 0.0,
            ea=(1.0 if won_a else 0.0) - pa - fee * pa,
            eb=(0.0 if won_a else 1.0) - pb - fee * pb,
            spread=pa - ba,
        ))
    if not recs:
        print("no rows")
        return 0
    print(f"{args.db}  {args.slug_like}  tau {args.tau_lo}-{args.tau_hi}  "
          f"fee {args.fee_bps:.0f}bps of stake.  rounds={len(recs)}")
    print(f"  mean ask sum A+B {st.mean([x['pa']+x['pb'] for x in recs]):.4f}   "
          f"mean leader spread {st.mean([x['spread'] for x in recs]):.4f}")

    def dayt(g, key):
        bd = collections.defaultdict(list)
        for x in g:
            bd[x["day"]].append(x[key])
        dm = [st.mean(v) for v in bd.values()]
        if len(dm) < 2:
            return 0.0, len(dm)
        sd = st.stdev(dm)
        return (st.mean(dm) / (sd / len(dm) ** 0.5) if sd else 0.0), len(dm)

    print(f"\n  {'|dist| bps':>12} {'n':>5} | {'LEADER ask':>10} {'real':>7} {'edge':>8} | "
          f"{'LAGGARD ask':>11} {'real':>7} {'edge':>8} {'day-t':>7}")
    for lo, hi in [(0, 0.25), (0.25, 0.5), (0.5, 1), (1, 2), (2, 5), (5, 1e18)]:
        g = [x for x in recs if lo <= x["d"] < hi]
        if len(g) < 15:
            continue
        ta, _ = dayt(g, "ea")
        tb, nb = dayt(g, "eb")
        print(f"  {lo:>5.2f}-{hi:<6.2f} {len(g):>5} | {np.mean([x['pa'] for x in g]):>10.4f} "
              f"{np.mean([x['wa'] for x in g]):>7.4f} {np.mean([x['ea'] for x in g]):>+8.4f} | "
              f"{np.mean([x['pb'] for x in g]):>11.4f} {1-np.mean([x['wa'] for x in g]):>7.4f} "
              f"{np.mean([x['eb'] for x in g]):>+8.4f} {tb:>+7.2f}")
    print("\n  by |dist| floor:")
    for m in (0.0, 0.25, 0.5, 1.0, 2.0):
        g = [x for x in recs if x["d"] >= m]
        if len(g) < 25:
            continue
        ta, na = dayt(g, "ea")
        tb, nb = dayt(g, "eb")
        print(f"    |d|>={m:<4.2f} n={len(g):>4}  LEADER {np.mean([x['ea'] for x in g]):+.4f} "
              f"(day-t {ta:+.2f}, {na}d)   FADE {np.mean([x['eb'] for x in g]):+.4f} "
              f"(day-t {tb:+.2f}, {nb}d)")
    print("\n  by day (leader at its ask):")
    bd = collections.defaultdict(list)
    for x in recs:
        bd[x["day"]].append(x)
    for d in sorted(bd):
        v = bd[d]
        print(f"    {d} n={len(v):>4} ask {st.mean([y['pa'] for y in v]):.4f} "
              f"realized {st.mean([y['wa'] for y in v]):.4f} "
              f"edge {st.mean([y['ea'] for y in v]):+.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
