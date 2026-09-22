"""Is the live sigma biased? Live's sigma_1m vs the realised vol, same instant.

The ledger says the model arm made +$75.66 on 09-09 at an average stake of $5.02
and has lost every day since. The gates did not change much; the sigma did. fair =
Phi(ln(S/K)/(sigma*sqrt(tau/60))), so a biased sigma moves every fair in the same
direction and there is no gate setting that can compensate for it.

This compares the sigma the arm actually RECORDED (`decisions.sigma_1m`) against
the realised 1-minute vol from Binance bars at the same instant, using the same
VolEstimator the arm uses, fed causally. No venue data, no arm decisions.

Usage:
    python3 -m tools.sigma_bias --csv data/recorded/decisions_model.csv.gz
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btcpredict.pricing.vol import VolEstimator                 # noqa: E402
from btcpredict.pricing.digital import fair_value               # noqa: E402

WARMUP = 240


def true_sigma(sym: str) -> tuple[np.ndarray, np.ndarray]:
    """(minute_epoch, sigma_1m) from the 30-day month corpus, fed causally."""
    d = pd.read_csv(f"data/bars_1m/{sym}_1m_30d.csv",
                    usecols=["open", "close", "ts"]).drop_duplicates("ts")
    d = d.sort_values("ts")
    ts = d.ts.to_numpy(dtype=np.int64)
    op = d.open.to_numpy(dtype=np.float64)
    cl = d.close.to_numpy(dtype=np.float64)
    vol = VolEstimator()
    for i in range(WARMUP):
        vol.add_bar(int(ts[i]) * 1000, math.log(cl[i] / op[i]),
                    hour=int((ts[i] // 3600) % 24))
    vol._recompute_seasonality()
    out_t, out_s = [], []
    for i in range(WARMUP, len(ts)):
        vol.add_bar(int(ts[i]) * 1000, math.log(cl[i] / op[i]),
                    hour=int((ts[i] // 3600) % 24))
        out_t.append(int(ts[i]))
        out_s.append(vol.sigma_1m(hour=int((ts[i] // 3600) % 24)))
    return np.array(out_t), np.array(out_s)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/recorded/decisions_model.csv.gz")
    ap.add_argument("--sym", default="BTCUSDT")
    ap.add_argument("--fair-tol", type=float, default=0.02)
    a = ap.parse_args()

    dd = pd.read_csv(a.csv,
                     usecols=["ts_ms", "tau_s", "sigma_1m", "spot", "strike",
                              "fair", "slug"])
    dd = dd[dd.slug.str.startswith("btc-updown-5m")]
    dd = dd.dropna(subset=["sigma_1m", "spot", "strike", "fair"])
    dd = dd[(dd.spot > 0) & (dd.strike > 0) & (dd.sigma_1m > 0)]
    print(f"{a.csv}: {len(dd):,} recorded decisions on {a.sym} "
          f"({pd.to_datetime(dd.ts_ms.min(), unit='ms').date()} .. "
          f"{pd.to_datetime(dd.ts_ms.max(), unit='ms').date()})")

    t, s = true_sigma(a.sym)
    lo, hi = int(t[0]), int(t[-1])
    d = dd[(dd.ts_ms // 1000 >= lo) & (dd.ts_ms // 1000 <= hi)].copy()
    if d.empty:
        print("no overlap between the recorded decisions and the bar corpus")
        return 1
    minute = (d.ts_ms // 1000 // 60 * 60).to_numpy()
    pos = np.searchsorted(t, minute, side="right") - 1
    ok = (pos >= 0) & (minute - t[np.clip(pos, 0, len(t) - 1)] <= 120)
    d = d[ok].copy()
    d["sigma_true"] = s[pos[ok]]
    d["ratio"] = d.sigma_1m / d.sigma_true
    d["day"] = d.ts_ms // 86_400_000

    # is the recorded fair even consistent with its own recorded sigma?
    # `fair` is logged for the SIDE BOUGHT, so a Down decision logs 1 - Phi(...).
    # Testing only the Up form marks every Down row as a defect -- which is what
    # an earlier version of this check did (22.5% "consistent"). Score both.
    fp = d.apply(lambda r: fair_value(float(r.spot), float(r.strike),
                                      float(r.tau_s), float(r.sigma_1m)), axis=1)
    up_ok = (fp - d.fair).abs() <= a.fair_tol
    dn_ok = ((1.0 - fp) - d.fair).abs() <= a.fair_tol
    both = up_ok | dn_ok
    print(f"\n  fair reproduced from its own recorded sigma: "
          f"{100 * both.mean():.1f}% (up {100 * up_ok.mean():.1f}%, "
          f"down {100 * dn_ok.mean():.1f}%)")
    if (~both).any():
        g = d[~both]
        print(f"    {int((~both).sum()):,} rows match NEITHER side -- the fair "
              f"there came from a sigma the arm did not log. Their median "
              f"|Phi(sigma_1m) - fair| is "
              f"{(fp[~both] - g.fair).abs().median():.3f}")
    else:
        print("    every row matches one of the two sides -- the logged sigma "
              "is the sigma behind the logged fair.")

    print(f"\n=== LIVE sigma_1m vs REALISED sigma_1m (same instant) ===")
    print(f"  recorded sigma  median {d.sigma_1m.median() * 1e4:.2f} bps/min")
    print(f"  realised sigma  median {d.sigma_true.median() * 1e4:.2f} bps/min")
    print(f"  RATIO live/true median {d.ratio.median():.3f}  "
          f"p10 {d.ratio.quantile(.1):.3f}  p90 {d.ratio.quantile(.9):.3f}  "
          f"n={len(d):,}")
    print(f"  1.00 = unbiased; >1 = live sigma too HIGH (fairs dragged to 0.50)")
    if abs(d.ratio.median() - 1.0) < 0.05:
        print("  -> the live sigma is on top of the realised vol.")
    else:
        print(f"  -> the live sigma is {d.ratio.median():.2f}x the realised vol, "
              f"so every fair is pulled {'toward' if d.ratio.median() > 1 else 'away from'} 0.50.")

    print(f"\n  per day:")
    for day, g in d.groupby("day"):
        import datetime as dt
        fu = g.apply(lambda r: fair_value(float(r.spot), float(r.strike),
                                          float(r.tau_s), float(r.sigma_1m)),
                     axis=1)
        # match rate must be dated: the arm's `up_model` was drift_digital until
        # 2026-09-10, and that model does NOT reduce to Phi(ln(S/K)/(s*sqrt(t/60)))
        m = ((fu - g.fair).abs() <= a.fair_tol) | \
            (((1 - fu) - g.fair).abs() <= a.fair_tol)
        print(f"    {dt.datetime.fromtimestamp(day * 86400, dt.timezone.utc).date()}"
              f"  n={len(g):>6}  live {g.sigma_1m.median() * 1e4:6.2f}  "
              f"true {g.sigma_true.median() * 1e4:6.2f}  "
              f"ratio {g.ratio.median():5.3f}  fair-matches {100 * m.mean():5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
