"""How stale is the venue's book? Measured, not assumed.

The gap this fills
------------------
`tools/forecast_30d.py` can replay the model on 30 days of bars but cannot price
a trade, because bars carry no book. Pricing the book from a LAGGED spot makes
the lag equal to the edge -- EV came out monotone in whatever lag was chosen
(0s -> no fills, 5s +0.20, 60s +0.33), which measures the assumption and
nothing else.

The lag has to come from data that holds the venue's quote and the spot at the
SAME instant. `data/recorded/quotes_arm.csv.gz` does: one row per poll with
`spot`, `up_bid`, `up_ask`, `down_bid`, `down_ask`, `tau_s`.

Method
------
Per round, build a 1-second grid and measure

    x_t = ln(S_{t+dt}/S_t)                      the spot's move over dt seconds
    y_t = q_{t+dt} - q_t                        the venue mid's move over the same dt

then for each lag L correlate y_t with x_{t-L}. At L=0 the book tracks the spot
perfectly; the L that maximises the correlation is how far behind the book is.

Two things make this robust rather than decorative:
* dt is a WINDOW, not one second. The venue ticks in cents, so a 1-second spot
  move is usually smaller than one tick and the signal is quantised away. The
  default dt is 10s, and the tool prints dt so the choice is visible.
* every series is standardised WITHIN its round before pooling. Rounds differ in
  volatility and in tau, and pooling raw changes would let the loudest rounds
  set the answer.

Correlation is scale-free, so no sigma has to be assumed anywhere -- which is the
point. This is the one number the 30-day forecast was missing.

Usage:
    python3 -m tools.venue_lag --family btc-updown-5m
"""
from __future__ import annotations

import argparse
import math
import os

import numpy as np
import pandas as pd

PATH = "data/recorded/quotes_arm.csv.gz"
ROUNDS = "data/recorded/rounds.csv.gz"


def load(family: str | None) -> list[tuple]:
    """-> list of (t_s, spot, mid) per round, on a 1-second grid."""
    q = pd.read_csv(PATH)
    q = q.dropna(subset=["up_bid", "up_ask", "down_bid", "down_ask", "spot"])
    q = q[(q.up_ask > 0) & (q.up_bid > 0) & (q.spot > 0)]
    if family:
        q = q[q.slug.str.startswith(family)]
    q = q.sort_values(["slug", "ts_ms"])
    out = []
    for slug, g in q.groupby("slug", sort=False):
        t = (g.ts_ms.to_numpy() // 1000).astype(np.int64)
        # venue mid from BOTH sides; quotes are quantised so the mean of the two
        # implied Up prices is steadier than one side alone
        mid = ((g.up_bid.to_numpy() + g.up_ask.to_numpy()) / 2.0
               + (1.0 - (g.down_bid.to_numpy() + g.down_ask.to_numpy()) / 2.0)
               ) / 2.0
        s = g.spot.to_numpy(dtype=np.float64)
        keep = np.ones(len(t), bool)
        keep[1:] = t[1:] > t[:-1]          # one row per second, first wins
        t, mid, s = t[keep], mid[keep], s[keep]
        if len(t) < 30:
            continue
        grid = np.arange(t[0], t[-1] + 1)
        if len(grid) < 30:
            continue
        mid = np.interp(grid, t, mid)      # linearly between polls, then
        s = np.interp(grid, t, s)
        out.append((grid, s, mid))
    return out


def pair(series: list[tuple], dt: int, lag: int) -> tuple[np.ndarray, np.ndarray]:
    """(standardised spot move at t-lag, standardised mid move at t)."""
    xs, ys = [], []
    for grid, s, mid in series:
        n = len(grid)
        if n < dt + lag + 2:
            continue
        i = np.arange(0, n - dt - lag)                # t
        x = np.log(s[i + dt] / s[i])                  # spot move, dt window
        j = i + lag
        y = mid[j + dt] - mid[j]                      # venue's move, later
        ok = np.isfinite(x) & np.isfinite(y)
        x, y = x[ok], y[ok]
        if len(x) < 20:
            continue
        sx, sy = x.std(), y.std()
        if sx <= 0 or sy <= 0:
            continue
        xs.append((x - x.mean()) / sx)
        ys.append((y - y.mean()) / sy)
    if not xs:
        return np.zeros(0), np.zeros(0)
    return np.concatenate(xs), np.concatenate(ys)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", default="btc-updown-5m")
    ap.add_argument("--all-families", action="store_true",
                    help="pool every family instead of one")
    ap.add_argument("--dt", type=int, default=10,
                    help="window over which each move is measured, seconds")
    ap.add_argument("--max-lag", type=int, default=60)
    a = ap.parse_args()

    series = load(None if a.all_families else a.family)
    if not series:
        print(f"no usable rounds for family={a.family}")
        return 1
    taus = np.concatenate([np.arange(len(g)) for g, _, _ in series])
    total_s = sum(len(g) for g, _, _ in series)
    print(f"{'ALL families' if a.all_families else a.family}: "
          f"{len(series):,} rounds | {total_s:,} quoted seconds "
          f"({total_s / 86400:.1f} days worth)")
    print(f"window dt = {a.dt}s | lag swept 0..{a.max_lag}s | every series "
          f"standardised within its own round")

    print(f"\n  {'lag s':>6}{'corr':>9}{'n':>10}{'vs lag0':>10}")
    best = (None, -2.0)
    rows = []
    for lag in list(range(0, 11)) + list(range(12, a.max_lag + 1, 2)):
        x, y = pair(series, a.dt, lag)
        if len(x) < 500:
            continue
        c = float(np.corrcoef(x, y)[0, 1])
        rows.append((lag, c, len(x)))
        if c > best[1]:
            best = (lag, c)
        print(f"  {lag:6d}{c:>+9.3f}{len(x):>10,}"
              f"{c - rows[0][1]:>+10.3f}")

    if best[0] is None:
        print("not enough paired observations")
        return 1
    lag0 = rows[0][1]
    print(f"\n  lag 0 correlation   {lag0:+.3f}")
    print(f"  best lag            {best[0]}s  (corr {best[1]:+.3f}, "
          f"{best[1] - lag0:+.3f} vs lag 0)")
    if best[0] == 0:
        print("  -> the book tracks the spot at the same second. Its staleness "
              "is NOT the edge.")
    else:
        print(f"  -> the book follows the spot about {best[0]}s behind it.")
    print(f"  variance explained by the spot move alone at the best lag: "
          f"{best[1] ** 2:.1%}")

    # how much of the venue's move happens at all -- a book that barely moves
    # cannot be scored on correlation
    x0, y0 = pair(series, a.dt, best[0])
    print(f"  sd of the venue's {a.dt}s mid move, in cents: "
          f"{100 * float(np.std(y0)) * float(np.mean([np.std(m) for _, _, m in series])):.2f}"
          f"  (raw series sd {np.mean([np.std(m) for _, _, m in series]):.4f})")
    print(f"  share of {a.dt}s windows where the venue's mid did not move at "
          f"all: {100 * float(np.mean(np.isclose(np.diff(np.concatenate([m for _, _, m in series])), 0))):.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
