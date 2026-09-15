"""Test four proposed re-geometries of the cross arm against the live quote tape.

The cross arm buys the side the spot JUST swapped into the money. Its config
hard-gates it to `5s <= tau <= 60s` (cross_min_tau_s / cross_max_tau_s), so every
cross entry in the database sits in the final minute. Four proposals to change
that geometry:

  V1 EARLY-TAU     raise the ceiling so the arm fires with lots of time left.
                   Claim: at high tau a crossing only moves fair 0.50 -> 0.53, so
                   249ms of transit fits inside the spread and we fill at fair.
  V2 ORIGIN-DEPTH  only fire if spot originated >=6bps from the strike within the
                   prior 45s -- a thrust, not chop around K.
  V3 SPILLOVER     do not trade round N at all; use its late cross as the trigger
                   to buy the un-repriced open of round N+1.
  V4 PROB-CROSS    re-anchor the trigger from "spot crossed K" to "contract ask
                   crossed 0.70 from below" with spot displacement >=4bps, on the
                   theory that delta is flat up there so the ask cannot gap.

Method. Rebuild the trigger from the `quotes` tape with the SAME rule the live arm
uses (`crossed_side`: newest tick at least look_s old is on the other side of the
strike, and is not staler than look_s+slack), then price the entry at the ask that
was actually displayed. One entry per round per variant -- the discipline the arm
runs under.

Every variant is granted its BEST case on fills: it pays the DISPLAYED ask with
zero slippage beyond it. That is strictly kinder than the live arm, which pays
whatever the ask has become 249ms later.

    python3 tools/cross_geometry.py --db data/btcpredict_vps.db
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import math
import sqlite3
import statistics
from collections import defaultdict

LOOK_S = 1.0
SLACK_S = 2.0


def crossed_side(spots, strike, i, look_s=LOOK_S, slack_s=SLACK_S):
    """The side that just crossed INTO the money at index i, or None.

    Mirrors btcpredict.main.crossed_side. `spots` is [(ts_ms, spot), ...]
    chronological; i is the index of the newest tick.
    """
    ts_now, px_now = spots[i]
    if not px_now or px_now <= 0 or not strike or strike <= 0:
        return None
    side_now = "Up" if px_now > strike else "Down"
    target = ts_now - int(look_s * 1000)
    oldest = ts_now - int((look_s + slack_s) * 1000)
    for j in range(i - 1, -1, -1):
        ts, px = spots[j]
        if ts > target:
            continue
        if not px or px <= 0 or ts < oldest:
            return None
        return side_now if ("Up" if px > strike else "Down") != side_now else None
    return None


def day_clustered_t(rows):
    """t over daily means. Pooled-t is misleading here: same-day rounds share a
    regime, which is the standing rule in this repo."""
    by_day = defaultdict(list)
    for ts_ms, ev in rows:
        d = dt.datetime.fromtimestamp(ts_ms / 1000, dt.timezone.utc).date()
        by_day[d].append(ev)
    days = [statistics.fmean(v) for v in by_day.values() if v]
    if len(days) < 2:
        return float("nan"), len(days)
    sd = statistics.stdev(days)
    if sd == 0:
        return float("nan"), len(days)
    return statistics.fmean(days) / (sd / math.sqrt(len(days))), len(days)


def report(name, entries, denom, entered):
    if not entries:
        print(f"  {name:<44} n=0")
        return
    n = len(entries)
    ask = statistics.fmean(e[1] for e in entries)
    win = statistics.fmean(1.0 if e[2] else 0.0 for e in entries)
    gross = win - ask
    net = statistics.fmean((1.0 if e[2] else 0.0) - e[1] - e[3] * e[1] for e in entries)
    t, nd = day_clustered_t([(e[0], (1.0 if e[2] else 0.0) - e[1]) for e in entries])
    pos = sum(1 for e in entries if e[2])
    fr = f"{entered / denom:5.1%}" if denom else "  n/a"
    print(
        f"  {name:<44} n={n:5d} fire={fr}  ask {ask:5.3f}  win {win:5.1%} ({pos:4d})  "
        f"gross {gross:+6.3f}/sh  net {net:+6.3f}/sh  day-t {t:+5.2f} ({nd}d)"
    )


def parse_len(slug):
    return "15m" if "-15m-" in slug else "5m"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict_vps.db")
    ap.add_argument("--look-s", type=float, default=LOOK_S)
    ap.add_argument("--origin-bps", type=float, default=6.0)
    ap.add_argument("--origin-s", type=float, default=45.0)
    ap.add_argument("--v4-px", type=float, default=0.70)
    ap.add_argument("--v4-depth-bps", type=float, default=4.0)
    ap.add_argument("--v3-min-bps", type=float, default=5.0)
    args = ap.parse_args()

    c = sqlite3.connect(args.db)
    rounds = {}
    for slug, start_ms, end_ms, strike, winner, fee_bps in c.execute(
        "select slug, start_ms, end_ms, strike, winner, fee_rate_bps from rounds "
        "where winner in ('Up','Down') and strike > 0"
    ):
        rounds[slug] = (start_ms, end_ms, strike, winner, fee_bps or 0)

    sql = (
        "select slug, ts_ms, tau_s, spot, up_ask, down_ask, fee_rate_bps "
        "from quotes where up_ask is not null and down_ask is not null and spot > 0 "
        "order by slug, ts_ms"
    )

    V = defaultdict(list)
    entered = defaultdict(set)
    universe = {"5m": 0, "15m": 0}
    cross_seen = 0
    late = {}          # slug -> (side, strength_bps, ts)  last strong late cross
    first_q = {}       # slug -> first tick of the round

    for slug, grp in itertools.groupby(c.execute(sql), key=lambda r: r[0]):
        if slug not in rounds:
            continue
        start_ms, end_ms, strike, winner, fee_bps = rounds[slug]
        universe[parse_len(slug)] += 1

        ticks = []
        for _slug, ts, tau, spot, ua, da, fb in grp:
            if ticks and ticks[-1][0] == ts:
                continue
            ticks.append((ts, tau, spot, ua, da, fb or fee_bps))
        if len(ticks) < 5 or strike <= 0:
            continue
        first_q[slug] = ticks[0]
        spots = [(t[0], t[2]) for t in ticks]

        taken = set()

        def put(key, ts, side, tick):
            if key in taken:
                return
            _t, _tau, _spot, ua, da, fb = tick
            ask = ua if side == "Up" else da
            if not ask or ask <= 0.02 or ask >= 0.98:
                return
            taken.add(key)
            V[key].append((ts, ask, winner == side, (fb or 0) / 1e4))
            entered[key].add(slug)

        for i in range(1, len(ticks)):
            side = crossed_side(spots, strike, i, args.look_s, SLACK_S)
            if side is None:
                continue
            cross_seen += 1
            ts, tau, spot, ua, da, fb = ticks[i]
            if tau is None:
                continue

            # V0 -- the arm as configured
            if 5.0 <= tau <= 60.0:
                put("V0 baseline (tau 5-60, as configured)", ts, side, ticks[i])

            # V1 -- early tau
            if 220.0 <= tau <= 300.0:
                put("V1a early-tau 5m  tau 220-300", ts, side, ticks[i])
            if 620.0 <= tau <= 840.0:
                put("V1b early-tau 15m tau 620-840", ts, side, ticks[i])
            if 60.0 < tau <= 300.0:
                put("V1c tau 60-300 (any contract)", ts, side, ticks[i])

            # V2 -- depth of origin
            window = ts - int(args.origin_s * 1000)
            origin = min((p for (t, p) in spots[:i] if t >= window), default=None)
            if origin is not None:
                disp = 1e4 * (origin - strike) / strike
                ok = (disp <= -args.origin_bps) if side == "Up" else (disp >= args.origin_bps)
                if ok and 5.0 <= tau <= 60.0:
                    put(f"V2 thrust-origin +-{args.origin_bps:.0f}bps/{args.origin_s:.0f}s",
                        ts, side, ticks[i])

            # V4 -- probability cross
            depth_bps = abs(spot - strike) / strike * 1e4
            prev = ticks[i - 1]
            pa = prev[3] if side == "Up" else prev[4]
            ask_now = ua if side == "Up" else da
            if (ask_now and pa and ask_now >= args.v4_px > pa
                    and depth_bps >= args.v4_depth_bps):
                put(f"V4 prob-cross {args.v4_px:.2f} depth>={args.v4_depth_bps:.0f}bps",
                    ts, side, ticks[i])

            # remember the LAST strong late cross, for V3
            if tau <= 60.0:
                strength = (spot - strike) / strike * 1e4
                if abs(strength) >= args.v3_min_bps:
                    cur = late.get(slug)
                    if cur is None or ts >= cur[2]:
                        late[slug] = (side, strength, ts)

    # ---- V3 spillover: trigger on round N, TRADE round N+1 ------------------
    fam = defaultdict(list)
    for slug in rounds:
        fam[(slug.split("-")[0], parse_len(slug))].append((rounds[slug][0], slug))
    for _key, lst in fam.items():
        lst.sort()
        for idx in range(len(lst) - 1):
            prev_slug = lst[idx][1]
            nxt_ms, next_slug = lst[idx + 1]
            if nxt_ms - lst[idx][0] > 20 * 60 * 1000:
                continue
            info = late.get(prev_slug)
            if not info or next_slug not in first_q:
                continue
            side = info[0]
            if next_slug in entered["V3 spillover -> next round open"]:
                continue
            tick = first_q[next_slug]
            _t, _tau, _spot, ua, da, fb = tick
            ask = ua if side == "Up" else da
            if not ask or ask <= 0.02 or ask >= 0.98:
                continue
            entered["V3 spillover -> next round open"].add(next_slug)
            V["V3 spillover -> next round open"].append(
                (tick[0], ask, rounds[next_slug][3] == side,
                 (fb or rounds[next_slug][4]) / 1e4)
            )

    total = universe["5m"] + universe["15m"]
    print(f"universe: {universe['5m']} settled 5m + {universe['15m']} 15m = {total} rounds; "
          f"{cross_seen} raw cross triggers on the tape")
    print("\n--- per-share economics, priced at the DISPLAYED ask (zero slippage granted) ---")
    for key in sorted(V):
        if "5m" in key:
            d = universe["5m"]
        elif "15m" in key:
            d = universe["15m"]
        else:
            d = total
        report(key, V[key], d, len(entered[key]))

    print("\n--- sanity: the arm's own config comment claims this tau table ---")
    print("  45-60 ask .668 win .760 | 60-90 ask .621 win .625 | 150-300 ask .581")
    print("  win .576 EV -0.005/sh -> 'edge is gone' above tau 90")


if __name__ == "__main__":
    main()
