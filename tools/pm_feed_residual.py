"""CAN WE TUNE THE BINANCE TAPE TOWARD CHAINLINK? No -- and we should not try.

The venue settles `chainlink-twap-60s-vs-range-start`; the arm reads BINANCE
spot. The operator's question after a round that turned on 0.02 bps: can the two
be brought closer, by modifying the feed or fitting a correction?

THREE ANSWERS, all measured on the CURRENT PM era (3,965 settled rounds with a
venue strike and a Binance tape covering both windows).

1. THERE IS NOTHING TO FIT. The venue's own published reference against our
   Binance twap60 reference:
       median +0.04 bps   mean +0.26   |gap| median 1.55   p10 -3.20  p90 +3.95
   Zero-mean with +-3.5 bps of dispersion. A constant correction moves nothing,
   and the dispersion is what decides marginal rounds. Chainlink is a COMPOSITE
   of many exchanges, not a lagged Binance, so one exchange's tape cannot
   reconstruct it -- the residual is cross-venue dispersion, not a lag.

2. USING THE VENUE'S OWN REFERENCE IS WORSE. Agreement with the venue's own
   winner, END = Binance twap60 in both columns:
       REF = ours (Binance twap60 at open)   3830/3965 = 96.60%
       REF = venue strike (Chainlink)        3688/3965 = 93.01%
   This replicates the corpus result in `pm_rule_source_decomp.py` (73.66% vs
   96.55%) that looked backwards -- it is not an era artifact.

   WHY, and it is the load-bearing insight: the venue computes BOTH its
   reference AND its settlement from Chainlink. Using Binance for both puts our
   source error on both sides, where it CANCELS. Mixing a Chainlink reference
   with a Binance end breaks the cancellation and the error shows in full.
   CONSISTENCY OF SOURCE BEATS MATCHING THE VENUE'S ABSOLUTE LEVEL. "Improving"
   the feed toward Chainlink would degrade exactly what earns the 96.6%.

3. THE RESIDUAL IS ENTIRELY A SMALL-MOVE PHENOMENON, by |final move| =
   |twap60(bell) - twap60(open)| on our own tape:
       [ 0.0, 0.5) bps  n= 152  agree 59.2%   <- a COIN FLIP
       [ 0.5, 1.0)      n= 123  agree 87.8%
       [ 1.0, 2.0)      n= 270  agree 90.7%
       [ 2.0, 3.0)      n= 244  agree 96.7%
       [ 3.0, 5.0)      n= 523  agree 97.9%
       [ 5.0,  10)      n=1074  agree 98.9%
       [10.0, inf)      n=1579  agree 99.9%
   Below half a basis point the feeds are a coin flip; above ten they are
   effectively identical. So the "feed problem" is not a feed problem at all --
   it is a SMALL-MOVE problem, and no amount of feed engineering touches it.

WHAT THIS MEANS FOR THE ARM. The quantity that decides exposure to this band is
the projected final move -- which is exactly `|fc - level|`, the margin the arm
ALREADY gates on (`cross_min_margin_bps`). So the instrument exists. It also
explains WHY that floor earns, and why it cannot simply be raised:
  * higher floor -> less feed-noise exposure -> better;
  * higher floor -> bigger, faster move -> worse fill (slippage rises with the
    margin: +0.0045 -> +0.0214 -> +0.0545/share) -> worse.
0.50 bps is where those two curves cross, which is where the tape sweep and the
live fills independently landed on 2026-09-18.

THE ONLY REAL FEED FIX, stated for completeness: `quotes.oracle` exists and is
NULL on all 1,669,984 PM-era rows. Polling the venue's own price series into it
would make the SIGNAL the quantity we are paid on rather than a correlated
proxy. That is the genuine solution and it is real work -- and it only pays if
the venue exposes a LIVE twap, not just the round's opening `priceToBeat`.

    /root/analysis-venv/bin/python tools/pm_feed_residual.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.main import twap_reference  # noqa: E402

DB = "/root/BinancePrediction/data/btcpredict.db"


def fam(s: str) -> str:
    return s.rsplit("-", 1)[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default=DB)
    ap.add_argument("--since", default="2026-09-16")
    args = ap.parse_args()
    cut = int(dt.datetime.fromisoformat(args.since)
              .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rounds = [dict(r) for r in con.execute("""
        select slug,start_ms,end_ms,strike,winner from rounds
        where end_ms>=? and winner in ('Up','Down') and strike is not null
          and feed_provider like 'chainlink%' order by end_ms""", (cut,))]
    tapes = {}
    for f in {fam(r["slug"]) for r in rounds}:
        tapes[f] = [(int(x["ts_ms"]), float(x["spot"])) for x in con.execute(
            """select ts_ms,spot from quotes where slug like ? and spot is not null
               order by ts_ms""", (f + "-%",))]
    gaps, rows, agree = [], [], {"ours": [0, 0], "venue": [0, 0]}
    for r in rounds:
        tp = tapes.get(fam(r["slug"]))
        if not tp:
            continue
        ref = twap_reference(tp, int(r["start_ms"]))
        if not ref or ref <= 0:
            continue
        w = [p for t, p in tp if int(r["end_ms"]) - 60_000 <= t <= int(r["end_ms"])]
        if len(w) < 30:
            continue
        end = sum(w) / len(w)
        gaps.append(1e4 * (float(r["strike"]) - ref) / ref)
        rows.append((abs(1e4 * (end - ref) / ref),
                     ("Up" if end > ref else "Down") == r["winner"]))
        for name, rr in (("ours", ref), ("venue", float(r["strike"]))):
            agree[name][0] += 1
            agree[name][1] += (("Up" if end > rr else "Down") == r["winner"])
    print(f"rounds scored: {len(rows)}")
    if gaps:
        a = sorted(gaps)
        print("\n=== venue strike vs OUR reference (bps) -- is there an offset to fit? ===")
        print("  median %+.2f  mean %+.2f  |gap| median %.2f  p10 %+.2f  p90 %+.2f"
              % (st.median(a), st.mean(a), st.median([abs(x) for x in a]),
                 a[int(.1 * len(a))], a[int(.9 * len(a)) - 1]))
    print("\n=== agreement with the venue's own winner (END = Binance twap60 both) ===")
    for k in ("ours", "venue"):
        n, h = agree[k]
        print("  REF = %-6s  %d/%d = %.2f%%" % (k, h, n, 100 * h / n if n else 0))
    print("\n=== the residual, by the size of the final move ===")
    for lo, hi in ((0, .5), (.5, 1), (1, 2), (2, 3), (3, 5), (5, 10), (10, 1e9)):
        sel = [ok for m, ok in rows if lo <= m < hi]
        if len(sel) < 5:
            continue
        print("  |move| [%4.1f,%5s) bps  n=%4d  agree %5.1f%%"
              % (lo, ("%g" % hi) if hi < 1e8 else "inf", len(sel),
                 100 * sum(sel) / len(sel)))


main()
