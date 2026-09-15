"""Does the ask still gap away from us at HIGH tau?

This is the mechanism behind the "early-tau gate" proposal. The claim: near the
strike the contract's delta is at its peak, so a crossing makes the ask jump
before we can reach it -- but with plenty of time left (high tau), fair only
moves 0.50 -> 0.53 on a crossing, so 249ms of transit fits inside the spread and
we fill at fair.

Test: at every cross trigger on the live tape, take the ask we would be shown,
then the ask at +250ms / +500ms / +1s. Positive = the price ran away from us.

    python3 tools/cross_gap_by_tau.py --db data/btcpredict_vps.db
"""

from __future__ import annotations

import argparse
import collections
import itertools
import sqlite3
import statistics
import sys

sys.path.insert(0, ".")
from tools.cross_geometry import crossed_side  # noqa: E402

BANDS = [
    ("A  tau   5-60   (current window)", 5.0, 60.0),
    ("B  tau  60-120", 60.0, 120.0),
    ("C  tau 120-200", 120.0, 200.0),
    ("D  tau 200-300   <- idea #1", 200.0, 300.0),
    ("E  tau 400-700   (15m early)", 400.0, 700.0),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict_vps.db")
    args = ap.parse_args()
    c = sqlite3.connect(args.db)

    rounds = {
        slug: (strike, winner)
        for slug, strike, winner in c.execute(
            "select slug, strike, winner from rounds "
            "where winner in ('Up','Down') and strike > 0"
        )
    }
    sql = (
        "select slug, ts_ms, tau_s, spot, up_ask, down_ask from quotes "
        "where up_ask is not null and down_ask is not null and spot > 0 "
        "order by slug, ts_ms"
    )

    buck = collections.defaultdict(list)
    for slug, grp in itertools.groupby(c.execute(sql), key=lambda r: r[0]):
        if slug not in rounds:
            continue
        strike, winner = rounds[slug]
        T = []
        for _s, ts, tau, spot, ua, da in grp:
            if T and T[-1][0] == ts:
                continue
            T.append((ts, tau, spot, ua, da))
        if len(T) < 5:
            continue
        spots = [(t[0], t[2]) for t in T]
        for i in range(1, len(T)):
            side = crossed_side(spots, strike, i)
            if side is None:
                continue
            ts, tau, spot, ua, da = T[i]
            if tau is None:
                continue
            ask = ua if side == "Up" else da
            if not ask or ask <= 0.02 or ask >= 0.98:
                continue
            band = next((n for n, lo, hi in BANDS if lo <= tau <= hi), None)
            if band is None:
                continue

            def at(dl, _ts=ts, _i=i, _side=side):
                for j in range(_i + 1, len(T)):
                    if T[j][0] >= _ts + dl:
                        return T[j][3] if _side == "Up" else T[j][4]
                return None

            buck[band].append(
                (tau, ask, at(250), at(500), at(1000), winner == side, spot, strike)
            )

    print("signed ask move AFTER the trigger. Positive = price ran away from us.")
    print("(the arm's decision price is the ask on the trigger row, and the venue")
    print(" has 249ms of head start on us)")
    hdr = (f"{'bucket':34s} {'n':>5s} {'ask@trig':>8s} {'d+250ms':>8s} {'d+500ms':>8s} "
           f"{'d+1s':>8s} {'%up':>6s} {'p50':>7s} {'p90':>7s} {'win':>6s} {'edge':>7s}")
    print(hdr)
    print("-" * len(hdr))
    for name, _lo, _hi in BANDS:
        rs = buck.get(name) or []
        if not rs:
            print(f"{name:34s} {0:5d}")
            continue
        d250 = [r[2] - r[1] for r in rs if r[2] is not None]
        d500 = [r[3] - r[1] for r in rs if r[3] is not None]
        d1k = [r[4] - r[1] for r in rs if r[4] is not None]
        ask = statistics.fmean(r[1] for r in rs)
        win = statistics.fmean(1.0 if r[5] else 0.0 for r in rs)
        up = sum(1 for x in d250 if x > 0) / len(d250) if d250 else float("nan")
        p90 = sorted(d250)[min(len(d250) - 1, int(len(d250) * 0.9))] if d250 else float("nan")
        print(
            f"{name:34s} {len(rs):5d} {ask:8.3f} {statistics.fmean(d250):+8.3f} "
            f"{statistics.fmean(d500):+8.3f} {statistics.fmean(d1k):+8.3f} {up:6.0%} "
            f"{statistics.median(d250):+7.3f} {p90:+7.3f} {win:6.1%} {win - ask:+7.3f}"
        )
    print()
    print("'edge' = win rate minus the ask on the trigger row, per share, BEFORE")
    print("slippage and BEFORE fees. It is the ceiling a perfect fill could reach.")


if __name__ == "__main__":
    main()
