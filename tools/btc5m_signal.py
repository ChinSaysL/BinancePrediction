"""Does the CROSSING predict the outcome? No prices involved, at all.

Every previous test of the cross arm was entangled with price: entry price,
latency, fill model. This one strips that out. It asks the narrowest possible
question:

    when spot crosses the strike inside the tau window, how often does the
    side it crossed INTO actually win?

No entry price, no fills, no fees, no order book. Just the signal and the
on-chain resolution. If the answer is ~50%, the arm is a coin flip and NO
execution improvement can save it. If it is meaningfully above 50%, then the
question becomes price and latency.

IT RUNS ON ALL 53 DAYS, not the 11 with tapes. The corpus carries
ticks_dense.parquet + metadata.json on every day; only trades_raw.json is
missing on 42 of them (the scraper wrote tapes every 4th day). Since this test
never reads a trade tape, restricting it to the tape days threw away 42 days of
signal for no reason -- 573 rounds became ~2,700.

DAY-CLUSTERED: one observation per day, so a single lucky day cannot carry the
result. `metadata.outcomePrices` supplies the resolution -- taken from the
chain, not inferred from spot.
"""
from __future__ import annotations

import argparse
import math
import statistics as st

from tools import btc5m_tape as T
from tools.btc5m_cross import ROUND_S, fire_labels

# Does the signal decay with age? Read down this table: if the win rate is flat
# there is no "freshness" premium and the tau window is a sample-size dial, not
# a selection rule.
TAU_BANDS = ((5, 15), (15, 30), (30, 45), (45, 60), (60, 90), (90, 120))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--look-s", type=int, default=1)
    ap.add_argument("--min-tau", type=int, default=5)
    ap.add_argument("--max-tau", type=int, default=60)
    ap.add_argument("--days", default="all")
    ap.add_argument("--tape-days-only", action="store_true",
                    help="restrict to the 11 days that have trade tapes "
                         "(only needed when prices are involved)")
    a = ap.parse_args()

    if a.days == "all":
        days = T.days_with_trades() if a.tape_days_only else T.days()
    else:
        days = a.days.split(",")
    print(f"signal-only test: does the crossed side win?   "
          f"look {a.look_s}s  tau [{a.min_tau},{a.max_tau}]")
    print(f"  {len(days)} days (no price, no tape, no fees)\n")

    per_day: list[tuple[str, int, int]] = []
    bands: dict[str, list[int]] = {}
    for day in days:
        n = w = 0
        for rd in T.rounds(day):
            R = T.load_round(rd, day)
            if R is None:
                continue
            lab = fire_labels(R.spot_open, R.strike, a.look_s)
            for k in range(len(lab)):
                if lab[k] is None:
                    continue
                tau = ROUND_S - k
                if not (a.min_tau <= tau <= a.max_tau):
                    continue
                n += 1
                w += int(lab[k] == R.label)
                for lo, hi in TAU_BANDS:
                    if lo <= tau < hi:
                        bands.setdefault(f"{lo}-{hi}", [0, 0])
                        bands[f"{lo}-{hi}"][0] += 1
                        bands[f"{lo}-{hi}"][1] += int(lab[k] == R.label)
                        break
                break                      # first crossing in the window
        if n:
            per_day.append((day, n, w))
        else:
            per_day.append((day, 0, 0))

    keep = [x for x in per_day if x[1]]
    if not keep:
        print("  nothing to report")
        return 0
    tot = sum(x[1] for x in keep)
    wins = sum(x[2] for x in keep)
    rates = [x[2] / x[1] for x in keep]
    p = wins / tot
    se = math.sqrt(p * (1 - p) / tot)
    dse = st.stdev(rates) / math.sqrt(len(rates)) if len(rates) > 1 else 0.0
    print()
    print(f"  rounds with a crossing : {tot}")
    print(f"  the crossed side won   : {wins}  ({100*p:.2f}%)")
    print(f"  pooled 95% CI          : "
          f"{100*(p-1.96*se):.2f}% .. {100*(p+1.96*se):.2f}%")
    print(f"  z vs a coin flip       : {(p-0.5)/se:+.2f}")
    print(f"  DAY-CLUSTERED t vs 50% : "
          f"{(p-0.5)/dse if dse else float('nan'):+.2f}")
    print(f"  days above 50%         : "
          f"{sum(1 for r in rates if r > 0.5)}/{len(rates)}")
    print()
    print("  by tau at the crossing (does the signal decay with age?):")
    for lo, hi in TAU_BANDS:
        nb = bands.get(f"{lo}-{hi}")
        if not nb or not nb[0]:
            continue
        nn, ww = nb
        pb = ww / nn
        seb = math.sqrt(pb * (1 - pb) / nn)
        print(f"    tau {lo:3d}-{hi:3d}s  n={nn:5d}  win {100*pb:5.1f}%  "
              f"z {(pb-0.5)/seb:+6.2f}")
    print("  A win rate is NOT an edge: break-even is price + fee, and at a")
    print("  typical 0.55 entry that needs ~57%. This only says whether the")
    print("  signal contains ANY information.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
