"""MOMENTUM CONTINUATION: is the contract UNDERPRICED after a real breakout?

The hypothesis, stated as it would have to be traded: a true institutional
breakout does not stop at the strike. Price thrusts 10-20bps past it over the
next 5-15s. At T+5s the favourite is quoted 0.68 while its true P(ITM) has
climbed to 0.85. You are not sniping a stale quote -- you are buying a running
train whose fair value is drifting faster than the spread.

WHY THIS IS THE RIGHT TEST, AND WHY IT HAS NOT BEEN DONE. Every previous test in
this campaign asked "can we FILL at a better price", and every one of them died
on the venue's ~1.4s working time. This asks a different question -- is the PRICE
WRONG -- and a slow, expensive, partial fill does not matter if the contract is
underpriced by more than the fee. It is a CALIBRATION test, so the queue is
irrelevant to it.

The nearest prior work is `DIST>=10bp` on the April corpus: px 0.7246, hit 0.7826,
edge +2.89pp/sh, day-t +1.15 (n=23). That is the hypothesis almost exactly. But
FINDINGS later found the April corpus's `*_sell_px` UNDER-REACTS against the live
venue ("Live governs"), which is precisely the artefact that manufactures an edge
of this shape. So it is re-run here on the LIVE venue's own asks, 5,556 rounds.

WHAT IS MEASURED
  STATIC   one row per round at the last quote with tau 10-60s: the leading side's
           executable ASK, its realised hit rate, and the gap hit - ask. If the
           market is calibrated the gap is ~0 and there is no edge at any price.
  DYNAMIC  THE HYPOTHESIS: find the first quote where |spot-K| >= X bps (the
           breakout), wait D seconds, buy the leading side at its ask then.
           Swept over X = 5/10/15/20 bps and D = 0/2/5/10s.
  CONTROL  the OPPOSITE side at the same entries. For a directional continuation
           bet this must be clearly negative; if both sides are positive the rule
           is selecting an entry MOMENT, not a direction.

    .venv/bin/python tools/momentum_continuation.py --db data/btcpredict.db
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import itertools
import math
import sqlite3
import statistics


def day_t(rows):
    by = collections.defaultdict(list)
    for ts, ev in rows:
        by[dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).date()].append(ev)
    means = [statistics.fmean(v) for v in by.values() if v]
    if len(means) < 2:
        return float("nan"), len(means)
    sd = statistics.stdev(means)
    return (float("nan") if sd == 0
            else statistics.fmean(means) / (sd / math.sqrt(len(means)))), len(means)


def report(name, rows, fee, tag=""):
    n = len(rows)
    if n < 15:
        print(f"  {name:34s} n={n:5d}  -- too few")
        return
    px = statistics.fmean(r[2] for r in rows)
    hit = statistics.fmean(1.0 if r[3] else 0.0 for r in rows)
    edge = statistics.fmean((1.0 if r[3] else 0.0) - r[2] for r in rows)
    net = statistics.fmean((1.0 if r[3] else 0.0) - r[2] - fee * r[2]
                           for r in rows)
    t, days = day_t([(r[1], (1.0 if r[3] else 0.0) - r[2]) for r in rows])
    print(f"  {name:34s} n={n:5d} px {px:6.3f}  hit {hit:6.1%}  "
          f"hit-px {edge:+7.3f}  net {net:+7.3f}  day-t {t:+5.2f}  {days}d  {tag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--tau-lo", type=float, default=10.0)
    ap.add_argument("--tau-hi", type=float, default=60.0)
    ap.add_argument("--min-tau", type=float, default=0.0,
                    help="tau at the BREAKOUT must be >= this (Train spec: 60)")
    ap.add_argument("--max-ask", type=float, default=0.98,
                    help="fire only if the coherent ask is <= this")
    ap.add_argument("--max-range-bps", type=float, default=0.0,
                    help="skip entries where the round has ALREADY travelled more "
                         "than this, open->entry. 0 = off. The cross arm ships "
                         "cross_max_round_range_bps=10.0, so this asks whether the "
                         "Train edge lives in the population that gate refuses.")
    ap.add_argument("--pay-up", type=float, default=0.0,
                    help="also report EV at ask+this, i.e. paying the LIMIT. "
                         "The arm pays about the limit: 69%% of live fills land "
                         "exactly on it, so this is the honest price.")
    ap.add_argument("--coherent", action="store_true", default=True,
                    help="require bid <= displayed <= ask (default ON)")
    ap.add_argument("--no-coherent", dest="coherent", action="store_false")
    args = ap.parse_args()

    c = sqlite3.connect(args.db)
    fee = 0.0117
    fr = [f / (s * p) for f, s, p in
          c.execute("select fee_paid, shares, filled_px from fills "
                    "where shares > 0 and filled_px > 0 and fee_paid > 0")]
    if fr:
        fee = statistics.median(fr)
    rounds = {s: (k, w) for s, k, w in c.execute(
        "select slug, strike, winner from rounds "
        "where winner in ('Up','Down') and strike > 0")}
    print(f"rounds with a winner: {len(rounds):,}   fee {fee:.4f} of price")

    sql = ("select slug, ts_ms, tau_s, spot, up_ask, down_ask, "
           "up_bid, down_bid, up_price, down_price from quotes "
           "where up_ask is not null and down_ask is not null and spot > 0 "
           "order by slug, ts_ms")
    series = {}
    for slug, grp in itertools.groupby(c.execute(sql), key=lambda r: r[0]):
        if slug not in rounds:
            continue
        T = []
        for _s, ts, tau, spot, ua, da, ub, db, upx, dpx in grp:
            if T and T[-1][0] == ts:
                continue
            T.append((ts, tau, spot, ua, da, ub, db, upx, dpx))
        if len(T) >= 5:
            series[slug] = T

    def pick(side, row):
        """The executable ask for `side` at this quote row, or None.

        COHERENCE IS THE WHOLE BALL GAME. FINDINGS records that only 47.2% of
        rows with a bid and an ask are coherent (bid <= displayed <= ask), and
        that scoring against the incoherent ones manufactured the +119%/$1
        table, VEL_INV's day-t +6.79 and 'every early rule's mirror going
        positive'. An ask recorded BELOW the displayed price is not a price you
        could have paid, and it is precisely the artefact that makes a deep
        favourite look underpriced.
        """
        _ts, _tau, _sp, ua, da, ub, db, upx, dpx = row
        ask = ua if side == "Up" else da
        if not ask or ask <= 0.02 or ask >= 0.98:
            return None
        if ask > args.max_ask:
            return None
        if args.coherent:
            bid = ub if side == "Up" else db
            px = upx if side == "Up" else dpx
            if not bid or not px or bid <= 0 or px <= 0:
                return None
            if not (bid - 1e-9 <= px <= ask + 1e-9):
                return None
        return ask

    def round_range_bps(slug, upto_ms):
        """How far the round has travelled, open -> upto_ms, in bps.

        The slug IS the round's start in unix seconds and the family names its
        length, so this needs no extra table. Cross arm ships this as
        cross_max_round_range_bps = 10.0.
        """
        parts = slug.split("-")
        dur = {"5m": 300, "15m": 900, "1h": 3600, "1d": 86400}.get(
            parts[2] if len(parts) > 2 else "", None)
        if dur is None:
            return None
        try:
            start = int(parts[3]) * 1000
        except (ValueError, IndexError):
            return None
        K = rounds[slug][0]
        seen = [row[2] for row in series[slug]
                if start <= row[0] <= upto_ms and row[2] > 0]
        if len(seen) < 2 or K <= 0:
            return None
        return 1e4 * (max(seen) - min(seen)) / K

    # ---------------- STATIC: calibration by distance --------------------
    # one entry per round: the LAST quote inside the tau window (the same
    # convention the prior DIST table used, so the numbers are comparable).
    stat = collections.defaultdict(list)
    for slug, T in series.items():
        K, winner = rounds[slug]
        chosen = None
        for row in T:
            ts, tau, spot = row[0], row[1], row[2]
            if tau is None or not (args.tau_lo <= tau <= args.tau_hi):
                continue
            side = "Up" if spot >= K else "Down"
            ask = pick(side, row)
            if ask is None:
                continue
            chosen = (ts, abs((spot - K) / K * 1e4), side, ask,
                      winner == side)
        if chosen:
            stat[min(7, int(chosen[1] // 2.5))].append(chosen)

    print("\nSTATIC -- calibration on the LIVE ask, one entry per round")
    print("  (if `hit-px` is ~0 the market is calibrated and there is no edge)")
    for b in sorted(stat):
        lo, hi = b * 2.5, (b + 1) * 2.5 if b < 7 else 99
        label = f"dist {lo:4.1f}-{hi:4.1f}bps"
        rows = [(c0[0], c0[0], c0[3], c0[4]) for c0 in stat[b]]
        report(label, rows, fee)

    # ---------------- DYNAMIC: the hypothesis ----------------------------
    print("\nDYNAMIC -- breakout >= X bps, wait D seconds, buy the runner")
    print("  `hit-px` is the whole claim: it should be LARGE positive.")
    for X in (5.0, 10.0, 15.0, 20.0):
        for D in (0, 2, 5, 10):
            rows, ctrl, paid = [], [], []
            for slug, T in series.items():
                K, winner = rounds[slug]
                bi = None
                for i, row in enumerate(T):
                    tau = row[1]
                    spot = row[2]
                    floor = max(3.0, args.min_tau)
                    if tau is None or tau < floor:
                        continue
                    if abs((spot - K) / K * 1e4) >= X:
                        bi = i
                        break
                if bi is None:
                    continue
                t0 = T[bi][0]
                j = next((i for i in range(bi, len(T))
                          if T[i][0] >= t0 + D * 1000), None)
                if j is None:
                    continue
                row = T[j]
                ts, spot = row[0], row[2]
                side = "Up" if spot >= K else "Down"
                if args.max_range_bps > 0:
                    rr = round_range_bps(slug, ts)
                    if rr is None or rr > args.max_range_bps:
                        continue
                ask = pick(side, row)
                oask = pick("Down" if side == "Up" else "Up", row)
                if ask is None:
                    continue
                rows.append((slug, ts, ask, winner == side))
                if args.pay_up:
                    paid.append((slug, ts, min(ask + args.pay_up, 0.995),
                                 winner == side))
                if oask is not None:
                    ctrl.append((slug, ts, oask, winner != side))
            if rows:
                report(f"X>={X:.0f}bp  D={D}s", rows, fee, "AT THE ASK")
                if paid:
                    report(f"   (paying ask+{args.pay_up*100:.1f}c = the limit)",
                           paid, fee, "PAYING THE LIMIT")
                if ctrl:
                    report(f"   (opposite side, same entries)", ctrl, fee)

    print("\n  Also findable in FINDINGS: DIST>=10bp on the APRIL corpus gave")
    print("  px 0.7246 hit 0.7826 (+2.89pp, day-t 1.15, n=23) -- but that corpus's")
    print("  ask UNDER-REACTS vs the live venue, which is exactly the artefact")
    print("  that manufactures this shape of edge. The live ask governs.")


if __name__ == "__main__":
    main()
