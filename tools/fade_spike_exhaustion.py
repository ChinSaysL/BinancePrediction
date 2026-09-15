#!/usr/bin/env python3
"""Mean reversion on post-spike exhaustion -- buy the unloved side.

THE CLAIM (third-party): when a fast move blows the contract odds out to >=0.80
and the spot momentum STALLS (spot is no longer running), retail is
over-extrapolating. Buy the unloved side at 0.15-0.20. Because spot is stationary
the 249ms latency costs nothing.

Why the "stall" clause matters and is the whole idea: the only reason this could
work when every other fade has failed is that the entry happens when the
underlying is NOT moving, so there is no adverse selection on the way in. That
makes the filter the load-bearing part, and it is therefore swept rather than
guessed.

Method, one entry per round:
  scan the round's quotes in tau [--tau-hi, --tau-lo]
  trigger: the FAVOURITE's displayed price >= --fav-px
  stall:   |spot now - spot --stall-s ago| / spot * 1e4 < --stall-bps
  buy:     the UNLOVED side at its ASK
  edge:    (1 if unloved won else 0) - ask - fee*ask        PER SHARE

PER SHARE, never per $: at an ask of 0.18 a win pays 5.5x, so an "EV/$" number
is dominated by leverage and is not comparable to anything.

Controls, because this is the ninth fade variant tried this session:
  - the MIRROR: same entries, buy the favourite instead
  - NO STALL FILTER: same entries, all of them
  - the fee size the venue actually charges (~96 bps of stake)
  - day-clustered t

Read-only.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import sqlite3
import statistics as st

FEE_BPS = 96.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict_vps.db")
    ap.add_argument("--tau-lo", type=float, default=60.0)
    ap.add_argument("--tau-hi", type=float, default=180.0)
    ap.add_argument("--fav-px", type=float, nargs="+", default=[0.75, 0.80, 0.85])
    ap.add_argument("--stall-s", type=float, default=5.0)
    ap.add_argument("--stall-bps", type=float, nargs="+", default=[0.0, 1.0, 3.0])
    ap.add_argument("--symbols", default="btc,eth,bnb")
    args = ap.parse_args()

    c = sqlite3.connect(args.db)
    c.row_factory = sqlite3.Row
    win = {}
    start = {}
    for x in c.execute("SELECT slug, winner, start_ms FROM rounds WHERE winner IS NOT NULL "
                       "AND slug LIKE '%-updown-5m-%'"):
        win[x["slug"]] = x["winner"]
        start[x["slug"]] = x["start_ms"]

    q = collections.defaultdict(list)
    for x in c.execute("SELECT slug, ts_ms, tau_s, up_price, down_price, up_ask, down_ask, spot "
                       "FROM quotes WHERE slug LIKE '%-updown-5m-%' AND tau_s BETWEEN ? AND ?",
                       (args.tau_lo, args.tau_hi)):
        q[x["slug"]].append((x["ts_ms"], x["tau_s"], x["up_price"], x["down_price"],
                             x["up_ask"], x["down_ask"], x["spot"]))

    feeb = FEE_BPS / 1e4
    # results[(fav_thr, stall_bps)] -> list of dicts
    res = collections.defaultdict(list)
    n_rounds = n_trigger = 0
    asks_seen = []

    for slug, rows in q.items():
        if slug not in win:
            continue
        if slug.split("-")[0] not in args.symbols.split(","):
            continue
        if not (slug.split("-")[0] == "btc" or True):
            continue
        n_rounds += 1
        rows.sort(key=lambda r: r[1], reverse=True)      # tau descending = forward in time
        ts_arr = [r[0] for r in rows]
        for thr in args.fav_px:
            hit = None
            for r in rows:
                up, dn = r[2], r[3]
                if up is None or dn is None:
                    continue
                if max(up, dn) >= thr:
                    hit = r
                    break
            if hit is None:
                continue
            n_trigger += 1
            up, dn = hit[2], hit[3]
            fav_up = up >= dn
            unloved_ask = hit[5] if fav_up else hit[4]     # down_ask if Up is favourite
            fav_ask = hit[4] if fav_up else hit[5]
            if not unloved_ask or not fav_ask:
                continue
            # stall: spot move over the last stall_s seconds, from this round's own quotes
            s_now = hit[6]
            s_then = None
            if s_now:
                j = bisect.bisect_right(ts_arr, hit[0] - args.stall_s * 1000) - 1
                if j >= 0:
                    s_then = rows[j][6]
            move = abs((s_now - s_then) / s_then * 1e4) if (s_now and s_then) else None
            unloved_won = (win[slug] == "Down") if fav_up else (win[slug] == "Up")
            day = dt.datetime.fromtimestamp(start[slug] / 1000, dt.UTC).strftime("%m-%d")
            asks_seen.append((unloved_ask, fav_ask))
            for st_bps in args.stall_bps:
                if st_bps > 0:
                    if move is None or move >= st_bps:
                        continue
                res[(thr, st_bps)].append(dict(
                    day=day, px=unloved_ask,
                    edge=(1.0 if unloved_won else 0.0) - unloved_ask - feeb * unloved_ask,
                    mirror=(1.0 - (1.0 if unloved_won else 0.0)) - fav_ask - feeb * fav_ask,
                    won=1.0 if unloved_won else 0.0, move=move))

    print(f"rounds with quotes in tau [{args.tau_lo:.0f},{args.tau_hi:.0f}]: {n_rounds:,}")
    print(f"rounds that reach a favourite >= the lowest threshold ({min(args.fav_px):.2f}): {n_trigger:,}")
    if asks_seen:
        a = sorted(x[0] for x in asks_seen)
        print(f"unloved-side ASK at the trigger: p10 {a[len(a)//10]:.3f}  median {a[len(a)//2]:.3f}  "
              f"p90 {a[9*len(a)//10]:.3f}   (the claim's 0.15-0.20 band)")
        inband = sum(1 for x in a if 0.15 <= x <= 0.20)
        print(f"  fraction of entries with the unloved ask in 0.15-0.20: {100*inband/len(a):.1f}%")

    def dayt(g, key):
        bd = collections.defaultdict(list)
        for x in g:
            bd[x["day"]].append(x[key])
        dm = [st.mean(v) for v in bd.values()]
        if len(dm) < 2:
            return 0.0, len(dm), 0
        sd = st.stdev(dm)
        return (st.mean(dm) / (sd / len(dm) ** 0.5) if sd else 0.0), len(dm), sum(1 for v in dm if v > 0)

    print(f"\nBUY THE UNLOVED SIDE at its ask, per SHARE, fee {FEE_BPS:.0f}bps of stake")
    print(f"{'fav>=':>6} {'stall<':>7} {'n':>5} {'ask':>7} {'won':>7} {'edge/sh':>9} "
          f"{'day-t':>7} {'days+':>7} | {'MIRROR(fav) edge':>16}")
    for thr in args.fav_px:
        for st_bps in args.stall_bps:
            g = res.get((thr, st_bps))
            if not g or len(g) < 20:
                print(f"{thr:>6.2f} {st_bps:>7.1f} {len(g) if g else 0:>5}   (too few)")
                continue
            t, nd, pos = dayt(g, "edge")
            print(f"{thr:>6.2f} {st_bps:>7.1f} {len(g):>5} {st.mean([x['px'] for x in g]):>7.4f} "
                  f"{st.mean([x['won'] for x in g]):>7.4f} {st.mean([x['edge'] for x in g]):>+9.4f} "
                  f"{t:>+7.2f} {pos:>4}/{nd:<2} | {st.mean([x['mirror'] for x in g]):>+16.4f}")
    print("\n  break-even win rate at an ask of p is p; a positive edge needs realised > ask + fee.")

    print("\nBY THRESHOLD, pooling the stall filter (stall_bps = 0 means 'no filter'):")
    for thr in args.fav_px:
        g = res.get((thr, 0.0))
        if not g or len(g) < 20:
            continue
        bd = collections.defaultdict(list)
        for x in g:
            bd[x["day"]].append(x)
        print(f"  fav>={thr:.2f}")
        for d in sorted(bd):
            v = bd[d]
            print(f"    {d}  n={len(v):>4}  ask {st.mean([y['px'] for y in v]):.4f} "
                  f"realised {st.mean([y['won'] for y in v]):.4f} "
                  f"edge {st.mean([y['edge'] for y in v]):+.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
