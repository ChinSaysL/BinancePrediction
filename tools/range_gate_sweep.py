"""Re-test the cross arm's ROUND-RANGE gate, on the instruction the gate shipped with.

config.py records the sweep that chose `cross_max_round_range_bps = 10.0`:

    gate      n/day  add-slippage   mean-revert
    off        64.4     0.1535        0.2651
    <=8        37.3     0.2436        0.3090
    <=10       46.0     0.2473        0.3463   <- best under BOTH models
    <=12       51.9     0.2347        0.3376
    CAVEAT: day-t at <=10 is 1.72, under this repo's t=2 bar.
            It ships on the growth result being consistent across two independent
            slippage models, not on the day-t. Revisit at ~30 more fills.

And the per-market overrides are set by MATCHED SELECTIVITY, not by a per-market
edge measurement:

    "each family gets the gate that admits the SAME SHARE of its own rounds that
     10.0 admits of BTC-5m's (59.5%) -- matched selectivity, not a copied
     constant"

So the thresholds are a share-matching heuristic on top of a marginally-significant
global sweep. This tool re-runs the sweep on the live tape.

The range is NOT the distance from the strike. It is how far the round has
TRAVELLED, `1e4 * (max(spot) - min(spot)) / strike` over the round so far
(`Bot._round_range_bps`), i.e. a round that went up and came back scores wide.
The poll buffer it reads is the same ~1s series the `quotes` table records, so it
replicates exactly.

Three controls the original sweep did not have:
  * one row per round (the arm's own discipline);
  * a CORRELATED-TRIO dedupe. btc/eth/bnb cross to the same side in the same
    minute off the same BTC move, so those are not independent observations.
    `trio` keeps one row per round-boundary (btc preferred, then eth, then bnb).
  * an executable-price HAIRCUT. The refusal `quote` is the displayed ask, and
    the measured realised slippage is +0.055/share. `net_haircut` subtracts it.

    python3 tools/range_gate_sweep.py --db data/btcpredict_vps.db
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import itertools
import math
import sqlite3
import statistics
import sys

sys.path.insert(0, ".")
from tools.cross_geometry import crossed_side  # noqa: E402

# the live per-market gates, for the normalised view
GATES = {"btc-updown-5m": 10.0, "eth-updown-5m": 12.9, "bnb-updown-5m": 7.8,
         "btc-updown-15m": 13.6, "eth-updown-15m": 21.0, "bnb-updown-15m": 21.1}


def day_t(rows):
    by = collections.defaultdict(list)
    for ts, ev in rows:
        d = dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).date()
        by[d].append(ev)
    means = [statistics.fmean(v) for v in by.values() if v]
    if len(means) < 2:
        return float("nan"), len(means)
    sd = statistics.stdev(means)
    if sd == 0:
        return float("nan"), len(means)
    return statistics.fmean(means) / (sd / math.sqrt(len(means))), len(means)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict_vps.db")
    ap.add_argument("--tau-lo", type=float, default=5.0)
    ap.add_argument("--tau-hi", type=float, default=60.0)
    ap.add_argument("--haircut", type=float, default=0.055,
                    help="realised slippage vs the displayed ask, per share")
    ap.add_argument("--fee", type=float, default=0.011, help="fee as a fraction of price")
    args = ap.parse_args()

    c = sqlite3.connect(args.db)
    rounds = {}
    for slug, start_ms, end_ms, strike, winner in c.execute(
        "select slug, start_ms, end_ms, strike, winner from rounds "
        "where winner in ('Up','Down') and strike > 0"
    ):
        rounds[slug] = (start_ms, end_ms, strike, winner)

    sql = ("select slug, ts_ms, tau_s, spot, up_ask, down_ask from quotes "
           "where up_ask is not null and down_ask is not null and spot > 0 "
           "order by slug, ts_ms")

    trig = []
    for slug, grp in itertools.groupby(c.execute(sql), key=lambda r: r[0]):
        if slug not in rounds:
            continue
        start_ms, end_ms, strike, winner = rounds[slug]
        T = []
        for _s, ts, tau, spot, ua, da in grp:
            if T and T[-1][0] == ts:
                continue
            T.append((ts, tau, spot, ua, da))
        if len(T) < 5:
            continue
        spots = [(t[0], t[2]) for t in T]

        lo = hi = None
        for i in range(1, len(T)):
            ts, tau, spot, ua, da = T[i]
            # maintain the round's travelled range, exactly as _round_range_bps does
            if lo is None or spot < lo:
                lo = spot
            if hi is None or spot > hi:
                hi = spot
            side = crossed_side(spots, strike, i)
            if side is None:
                continue
            if tau is None or not (args.tau_lo <= tau <= args.tau_hi):
                continue
            ask = ua if side == "Up" else da
            if not ask or ask <= 0.02 or ask >= 0.98:
                continue
            rng = 1e4 * (hi - lo) / strike
            sym = slug.split("-")[0]
            trig.append({
                "slug": slug, "sym": sym, "ts": ts, "end": end_ms, "rng": rng,
                "ask": ask, "won": 1.0 if winner == side else 0.0,
                "gate": GATES.get(f"{sym}-{'15m' if '-15m-' in slug else '5m'}", 10.0),
            })
            break                      # first trigger per round

    n = len(trig)
    print(f"tape: {n} first-crosses per round, tau {args.tau_lo:.0f}-{args.tau_hi:.0f}")
    rr = sorted(t["rng"] for t in trig)
    q = lambda p: rr[min(len(rr) - 1, int(len(rr) * p))]
    print(f"round-range bps: p10 {q(.1):.1f}  p25 {q(.25):.1f}  p50 {q(.5):.1f}  "
          f"p75 {q(.75):.1f}  p90 {q(.9):.1f}  max {rr[-1]:.0f}")
    print(f"already under 10.0 bps: {sum(1 for x in rr if x <= 10)/n:.1%}")
    print()

    def ev(rows, net=False):
        a = statistics.fmean(r["ask"] for r in rows)
        w = statistics.fmean(r["won"] for r in rows)
        cost = (args.haircut + args.fee * a) if net else 0.0
        return a, w, w - a - cost, len(rows)

    # correlated-trio dedupe: one row per round-boundary, btc > eth > bnb
    pref = {"btc": 0, "eth": 1, "bnb": 2}
    best = {}
    for t in trig:
        k = t["end"]
        if k not in best or pref[t["sym"]] < pref[best[k]["sym"]]:
            best[k] = t
    trio = list(best.values())

    hdr = (f"{'gate':>10s} {'n':>5s} {'kept':>6s} {'ask':>7s} {'win':>7s} "
           f"{'gross':>8s} {'haircut':>8s} {'day-t':>7s}")
    for label, rows in (("ALL ROWS", trig), ("TRIO (1 per boundary)", trio)):
        print(f"=== {label} ===")
        print(hdr)
        print("-" * len(hdr))
        for T in (1e9, 20, 16, 14, 12, 10, 8, 6):
            rs = [r for r in rows if r["rng"] <= T]
            if not rs:
                continue
            a, w, net, k = ev(rs, net=True)
            t, nd = day_t([(r["ts"], r["won"] - r["ask"]) for r in rs])
            lbl = "off" if T == 1e9 else f"<={T}"
            print(f"{lbl:>10s} {len(rows):5d} {k/len(rows):6.0%} {a:7.3f} {w:7.1%} "
                  f"{w-a:+8.3f} {net:+8.3f} {t:+7.2f}")
        print()

    # normalised: range / the market's own live gate
    print("=== TRIO, normalised to each market's LIVE gate (range / gate) ===")
    print(hdr)
    print("-" * len(hdr))
    for lo, hi, lbl in [(0, 0.8, "0.0-0.8"), (0.8, 1.0, "0.8-1.0"), (1.0, 1.5, "1.0-1.5"),
                        (1.5, 2.5, "1.5-2.5"), (2.5, 1e9, ">2.5")]:
        rs = [r for r in trio if lo <= r["rng"] / r["gate"] < hi]
        if not rs:
            continue
        a, w, net, k = ev(rs, net=True)
        t, nd = day_t([(r["ts"], r["won"] - r["ask"]) for r in rs])
        print(f"{lbl:>10s} {len(trio):5d} {k:6d} {a:7.3f} {w:7.1%} {w-a:+8.3f} "
              f"{net:+8.3f} {t:+7.2f}")
    print()
    print("If the gate's premise is right ('a round that has swung wide is one where")
    print("a late crossing reverses again'), the win rate must FALL as range rises.")
    print("Read the last table's win column downward.")


if __name__ == "__main__":
    main()
