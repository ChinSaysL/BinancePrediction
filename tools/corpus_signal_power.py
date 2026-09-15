#!/usr/bin/env python3
"""Directional skill over the 31-day BTC-5m corpus.

The live-venue tests all died for the same reason: 5 days is ~5 independent
observations, and six unrelated rules correlate r=+0.73 day-to-day. This corpus
has 8,436 windows over 31 days with momentum at nine horizons, vol_surge, CVD,
z_tau and the spot-vs-strike distance precomputed, so "is there any directional
edge, at ANY horizon" can be asked with real power.

NOTE on `px` in this table: it is the BTC PRICE (72k-82k), NOT the odds. The ml
table carries no prices, so this is a HIT-RATE test, not a P&L test:

  hit    P(the side the rule chose actually won)
  base   the unconditional P(that side wins) over the same days. The strike is
         the round-open price, so the market is ~a coin flip and base ~ 0.5.
  EDGE   hit - base. Positive means the rule is picking winners more often
         than the coin flip it is betting into. A hit rate alone cannot be
         traded -- the price has to be below it -- but it is the necessary
         condition, and prices are checked separately on the 8-day table with
         real buy/sell quotes.

t is day-clustered (31 days), with per-day signs: a rule positive on 28 of 31
days is a different object from one carried by a single day.

Read-only.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import statistics as st

import pyarrow.parquet as pq

ML = "data/btc5m_ml.parquet"

# (name, feature, threshold, sign_from_feature)
RULES = [
    ("mom_5>=1bp", "mom_5_bps", 1.0, "self"),
    ("mom_5>=2bp", "mom_5_bps", 2.0, "self"),
    ("mom_5>=4bp", "mom_5_bps", 4.0, "self"),
    ("mom_10>=2bp", "mom_10_bps", 2.0, "self"),
    ("mom_30>=2bp", "mom_30_bps", 2.0, "self"),
    ("mom_30>=4bp", "mom_30_bps", 4.0, "self"),
    ("mom_60>=4bp", "mom_60_bps", 4.0, "self"),
    ("mom_120>=4bp", "mom_120_bps", 4.0, "self"),
    ("mom_300>=8bp", "mom_300_bps", 8.0, "self"),
    ("volsurge>=1.5", "vol_surge", 1.5, "mom_5_bps"),
    ("volsurge>=2.5", "vol_surge", 2.5, "mom_5_bps"),
    ("volsurge>=4.0", "vol_surge", 4.0, "mom_5_bps"),
    ("cvd_ratio>=0.3", "cvd_ratio", 0.3, "cvd"),
    ("cvd_ratio>=0.6", "cvd_ratio", 0.6, "cvd"),
    ("cvd10>=0.5", "cvd_10_ratio", 0.5, "cvd"),
    ("ztau>=0.5", "z_tau", 0.5, "z_tau"),
    ("ztau>=1.0", "z_tau", 1.0, "z_tau"),
    ("dist>=2bp", "delta_bps", 2.0, "delta_bps"),
    ("dist>=5bp", "delta_bps", 5.0, "delta_bps"),
    ("dist>=10bp", "delta_bps", 10.0, "delta_bps"),
]


def dayname(d) -> str:
    try:
        return dt.datetime.fromtimestamp(int(d) * 86400, dt.UTC).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return str(d)


def pnl(p: float, won: bool) -> float:
    return (1 - p) / p if won else -1.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tau-lo", type=float, default=10.0)
    ap.add_argument("--tau-hi", type=float, default=60.0)
    args = ap.parse_args()

    feats = sorted({r[1] for r in RULES} | {r[3] for r in RULES if r[3] != "self"})
    cols = ["window_id", "day", "tau_s", "px", "y_up"] + feats
    t = pq.read_table(ML, columns=cols)
    d = t.to_pydict()
    n = t.num_rows
    print(f"corpus: {n:,} rows, {len(set(d['window_id'])):,} windows, "
          f"{len({x for x in d['day'] if x is not None})} days")
    print(f"tau window [{args.tau_lo:.0f}, {args.tau_hi:.0f}]s\n")

    print(f"{'rule':16} {'n':>6} {'days':>5} {'hit':>7} {'base':>7} {'EDGE':>8} "
          f"{'se':>7} {'day-t':>7} {'pos/day':>8}  signs")
    for name, feat, thr, dirn in RULES:
        picks: list[dict] = []
        for i in range(n):
            tau = d["tau_s"][i]
            if tau is None or not (args.tau_lo <= tau <= args.tau_hi):
                continue
            y = d["y_up"][i]
            if y is None:
                continue
            v = d[feat][i]
            if v is None or abs(v) < thr:
                continue
            if dirn == "self":
                sd = 1 if v > 0 else -1
            else:
                sv = d[dirn][i]
                if sv is None or sv == 0:
                    continue
                sd = 1 if sv > 0 else -1
            picks.append({"day": dayname(d["day"][i]), "y": y, "sd": sd})
        if not picks:
            continue
        # EDGE = P(chosen side wins) - P(that side wins unconditionally).
        # The unconditional rate is measured on the SAME rows, so it absorbs
        # any drift in the corpus rather than assuming a 50/50 coin flip.
        up_rate = st.mean([r["y"] for r in picks])
        byday: dict[str, list[float]] = collections.defaultdict(list)
        hits = []
        for r in picks:
            won = 1.0 if ((r["y"] == 1) if r["sd"] > 0 else (r["y"] == 0)) else 0.0
            base = up_rate if r["sd"] > 0 else 1 - up_rate
            hits.append(won)
            byday[r["day"]].append(won - base)
        dm = [st.mean(v) for v in byday.values()]
        sd = st.stdev(dm) if len(dm) > 1 else 0.0
        tstat = st.mean(dm) / (sd / len(dm) ** 0.5) if sd else 0.0
        npos = sum(1 for x in dm if x > 0)
        signs = "".join("+" if x > 0 else "-" for x in dm)
        print(f"{name:16} {len(picks):>6} {len(byday):>5} {st.mean(hits):7.4f} "
              f"{st.mean([(up_rate if r['sd'] > 0 else 1 - up_rate) for r in picks]):7.4f} "
              f"{st.mean(dm):+8.4f} {sd/len(dm)**0.5:7.4f} {tstat:+7.2f} "
              f"{npos:>4}/{len(dm):<3}  {signs}")
    print("\n  EDGE > 0 with |day-t| > 2 and most days positive = directional skill.")
    print("  A hit rate is NECESSARY but not sufficient -- the price must be below")
    print("  it, which is checked on the 8-day table that has real quotes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
