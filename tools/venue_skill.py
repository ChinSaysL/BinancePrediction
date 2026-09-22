"""Does OUR fair beat the VENUE'S mid? The estimator-free test.

The confound this settles
------------------------
`tools/forecast_paired.py` measures the venue's implied sigma at 1.36x ours and
finds +0.0234/$1 buying at the venue's ask. But "the venue's sigma is 1.36x ours"
is a statement about TWO estimators, and ours is built here from 1-second polls
rather than the kline feed, so it can easily be the one that is wrong. If our
sigma is simply 1.36x too LOW, then the venue is pricing at the true sigma and
the measured "edge" is our own error pointed at our own bankroll.

Sigma is the suspect, so do not test sigma -- test PREDICTION. On the same poll,
take our fair and the venue's mid and score both against the round's settlement:

    Brier(our fair) vs Brier(venue mid), paired on the round, day-clustered.

This needs no sigma at all. It cannot be confounded by an estimator because the
outcome is the only thing being predicted. If our fair does not beat the mid,
there is no edge to forecast, whatever the sigma ratio says.

Usage:
    python3 -m tools.venue_skill --family btc-updown-5m
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btcpredict.config import settings as S          # noqa: E402

from tools.forecast_paired import (build_sigma, load, phi_inv_vec,  # noqa: E402
                                   )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", default="btc-updown-5m")
    ap.add_argument("--min-tau", type=float, default=S.min_tau_s)
    ap.add_argument("--max-tau", type=float, default=S.max_tau_s)
    ap.add_argument("--tau-pick", type=float, default=120.0,
                    help="score at the first poll at or below this tau")
    a = ap.parse_args()

    q, rounds, slugs = load(a.family)
    sig = build_sigma(q, slugs)
    idx = rounds.set_index("slug")

    rows = []
    for x in q.itertuples(index=False):
        if x.slug not in idx.index or not (a.min_tau <= x.tau_s <= a.max_tau):
            continue
        rr = idx.loc[x.slug]
        if isinstance(rr, pd.DataFrame):
            rr = rr.iloc[0]
        K = float(rr.strike)
        minute = (int(x.ts_ms) // 1000) // 60 * 60
        sg = sig.get(minute, np.nan)
        if not np.isfinite(sg):
            continue
        mid = ((x.up_bid + x.up_ask) / 2 + (1 - (x.down_bid + x.down_ask) / 2)) / 2
        from btcpredict.pricing.digital import fair_value
        rows.append({"slug": x.slug, "tau": x.tau_s, "day": rr.day,
                     "mid": min(0.999, max(0.001, mid)),
                     "fair": fair_value(x.spot, K, x.tau_s, sg),
                     "y": int(rr.winner == "Up"), "sigma": sg})
    d = pd.DataFrame(rows)
    if d.empty:
        print("no polls")
        return 1
    d = (d.sort_values(["slug", "tau"], ascending=[True, False])
           .drop_duplicates("slug", keep="first"))
    n_rounds = len(d)
    print(f"{a.family}: {n_rounds:,} rounds scored at the first poll with tau "
          f"<= {a.tau_pick:.0f}s+ (tau {d.tau.min():.0f}-{d.tau.max():.0f}) "
          f"over {d.day.nunique()} days")

    fb = (d.fair - d.y) ** 2
    mb = (d.mid - d.y) ** 2
    diff = mb - fb
    print(f"\n  Brier  our fair {fb.mean():.4f} | venue mid {mb.mean():.4f} | "
          f"difference (mid - ours) {diff.mean():+.4f}")
    print(f"  log-loss our fair "
          f"{-np.mean(d.y * np.log(d.fair.clip(1e-6, 1 - 1e-6)) + (1 - d.y) * np.log((1 - d.fair).clip(1e-6, 1 - 1e-6))):.4f}"
          f" | venue mid "
          f"{-np.mean(d.y * np.log(d.mid.clip(1e-6, 1 - 1e-6)) + (1 - d.y) * np.log((1 - d.mid).clip(1e-6, 1 - 1e-6))):.4f}")
    t_pool = diff.mean() / (diff.std(ddof=1) / math.sqrt(len(diff)))
    byd = diff.groupby(d.day.to_numpy()).mean()
    t_day = byd.mean() / (byd.std(ddof=1) / math.sqrt(len(byd)))
    days = sorted(byd.index)
    half = np.median(days)
    print(f"  paired diff: pooled t {t_pool:+.2f} | day-clustered t "
          f"{t_day:+.2f} over {len(byd)} days")
    print(f"  halves {byd[byd.index <= half].mean():+.4f} / "
          f"{byd[byd.index > half].mean():+.4f} | "
          f"drop-best-day {byd.drop(byd.idxmax()).mean():+.4f}")
    print(f"  POSITIVE difference = the venue's mid is the worse forecast.")

    # and the same question in money, at the mid (no spread), one bet per round
    take = d[d.fair > d.mid]
    ev_mid = (take.y / take.mid - 1.0).mean() if len(take) else float("nan")
    print(f"\n  betting the side our fair favours, at the venue MID, no spread, "
          f"no fee:")
    print(f"    n={len(take):,} ({len(take) / max(n_rounds, 1):.0%} of rounds) "
          f"WR {take.y.mean():.1%} paid {take.mid.mean():.3f} "
          f"EV {ev_mid:+.4f}/$1")
    print(f"    if this is not clearly positive, no sigma argument rescues it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
