"""IDEA 2, the mechanism half: does one tape actually LEAD the other?

The proposal: "large block market orders on Coinbase Pro or Bybit Perpetual
frequently lead Binance Spot by 50ms to 150ms -- an aggressive breakout there is
an early warning before the Binance spot feed even sees the move."

That claim is about a venue we hold no tick data for. But its MECHANISM -- one
tape carrying the information before the other -- is directly measurable on a
pair we DO hold: Binance USD-M PERP vs Binance SPOT, five overlapping days
(09-08..09-12), same schema, same 3 symbols.

Why it matters. The lead only has value if it survives the transport to the
venue we trade. Measured from the Tokyo box with a full HTTPS GET to a live
endpoint:

    binance spot BTCUSDT   p50 16.5ms      (same metro as the box)
    binance perp BTCUSDT   p50  9.2ms
    bybit    spot BTCUSDT  p50 82.7ms      <- 41ms ONE-WAY, before any lead
    coinbase spot BTC-USD  p50 30.0ms      (edge-cached; see the note below)

A 50-150ms claimed lead minus ~41ms of one-way propagation to a Tokyo-reachable
Bybit leaves 9-109ms, against a pipeline that needs ~300ms of lead to place the
order before the cross. So the size of the real lead decides whether the idea has
anything left in it.

WHAT THIS MEASURES

  1. Cross-correlation of perp and spot returns on a 50ms grid, lags +/-1s. The
     lag with the highest correlation is the lead.
  2. The tradeable version: after the perp moves >=1/2/5 bps inside 100ms, what
     does SPOT do over the next 50ms..2s? And the reverse (spot -> perp) as the
     control, because a symmetric result is just common movement, not a lead.

    python3 tools/perp_spot_lead.py --symbol BTCUSDT --day 2026-09-12
"""

from __future__ import annotations

import argparse
import io
import zipfile

import numpy as np
import pandas as pd

SPOT = "data/aggTrades/{s}-aggTrades-{d}.zip"
PERP = "data/perp/aggTrades/{s}-aggTrades-{d}.zip"
GRID = 0.05          # 50ms
LAG_BINS = 20        # +/-1s of lags
HORIZONS = (1, 2, 5, 10, 20, 40)   # in grid steps -> 50ms .. 2s


def load(path: str) -> pd.DataFrame:
    """Spot and perp aggTrades differ: the SPOT daily csv carries an extra
    trailing `is_best_match` field (8 columns) while the USD-M perp csv has 7,
    and only the perp file has a header row. Select by POSITION, never by name."""
    with zipfile.ZipFile(path) as z:
        name = z.namelist()[0]
        raw = z.read(name)
    df = pd.read_csv(io.BytesIO(raw), header=None,
                     skiprows=1 if raw.startswith(b"agg_trade_id") else 0,
                     usecols=[1, 2, 5, 6])
    df.columns = ["price", "qty", "ts", "maker"]
    # `transact_time` IS IN DIFFERENT UNITS ON THE TWO TAPES under the same
    # column name: spot is microseconds (1789171200412148), USD-M perp is
    # MILLISECONDS (1789171200002). Dividing both by 1e6 puts the perp in 1970
    # and the join silently produces an empty overlap.
    ts = df["ts"].to_numpy(dtype=np.int64)
    df["t"] = ts / (1e6 if ts[0] > 1e15 else 1e3)
    return df


def grid_series(df: pd.DataFrame, t0: float, n: int) -> np.ndarray:
    """Last trade price per 50ms bin, forward-filled."""
    idx = ((df["t"].to_numpy() - t0) / GRID).astype(np.int64)
    ok = (idx >= 0) & (idx < n)
    idx, px = idx[ok], df["price"].to_numpy()[ok]
    out = np.full(n, np.nan)
    out[idx] = px                       # last write per bin wins
    s = pd.Series(out).ffill()
    return s.to_numpy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--day", default="2026-09-12")
    args = ap.parse_args()

    sp = load(SPOT.format(s=args.symbol, d=args.day))
    pp = load(PERP.format(s=args.symbol, d=args.day))
    t0 = max(sp["t"].min(), pp["t"].min())
    t1 = min(sp["t"].max(), pp["t"].max())
    n = int((t1 - t0) / GRID) + 1
    S = grid_series(sp, t0, n)
    P = grid_series(pp, t0, n)
    good = np.isfinite(S) & np.isfinite(P)
    print(f"{args.symbol} {args.day}: {(t1-t0)/3600:.1f}h, {n:,} bins of "
          f"{GRID*1000:.0f}ms; spot {len(sp):,} prints, perp {len(pp):,}")

    rS = np.full(n, np.nan)
    rP = np.full(n, np.nan)
    rS[1:] = np.diff(np.log(S))
    rP[1:] = np.diff(np.log(P))

    print("\n1. CROSS-CORRELATION of 50ms returns, does either tape lead?")
    print(f"   {'lag':>7s} {'corr(perp_t, spot_t+lag)':>26s}")
    best = None
    for k in range(-LAG_BINS, LAG_BINS + 1):
        if k >= 0:
            a, b = rP[:n - k], rS[k:]
        else:
            a, b = rP[-k:], rS[:n + k]
        m = np.isfinite(a) & np.isfinite(b)
        if m.sum() < 1000:
            continue
        c = np.corrcoef(a[m], b[m])[0, 1]
        if k % 4 == 0 or k in (-1, 1, -2, 2, -3, 3):
            print(f"   {k*GRID*1000:+6.0f}ms {c:26.4f}")
        if best is None or c > best[1]:
            best = (k, c)
    print(f"   -> peak at lag {best[0]*GRID*1000:+.0f}ms "
          f"(positive = PERP leads SPOT)  corr {best[1]:.4f}")

    print("\n2. THE TRADEABLE VERSION -- forward return after a 100ms move")
    # The event is a 100ms log move, so it spans TWO 50ms bins. The forward
    # series below must be the PRICE series S/P -- feeding rS/rP (which are
    # returns) divides a return by a return and prints +/-inf and 98,000bps.
    mP = np.full(n, np.nan)
    mS = np.full(n, np.nan)
    mP[2:] = np.log(P[2:] / P[:-2])
    mS[2:] = np.log(S[2:] / S[:-2])
    for thr in (1.0, 2.0, 5.0):
        for name, r, f in (("perp -> spot", mP, S), ("spot -> perp", mS, P)):
            ev = np.isfinite(r) & (r >= thr / 1e4)
            if ev.sum() < 30:
                print(f"   {name} >= {thr:.0f}bps  events {int(ev.sum())} -- too few")
                continue
            idx = np.nonzero(ev)[0]
            line = f"   {name} >= {thr:.0f}bps  n={len(idx):7,d}"
            for h in HORIZONS:
                j = idx[idx + h < n]
                fwd = (f[j + h] - f[j]) / f[j] * 1e4
                line += f"  {h*GRID*1000:.0f}ms {np.nanmean(fwd):+6.2f}"
            print(line + "  bps")
    print("   (a real lead shows up as a LARGE positive number for perp->spot")
    print("    and a near-zero or negative one for spot->perp at the same lag)")

    base = []
    for h in HORIZONS:
        fwd = (S[h:] - S[:-h]) / S[:-h] * 1e4
        base.append(f"{h*GRID*1000:.0f}ms {np.nanmean(fwd):+6.2f}")
    print(f"\n   baseline spot drift: " + "  ".join(base) + "  bps")


if __name__ == "__main__":
    main()
