"""IDEA 1: iceberg / hidden absorption -- heavy aggression, price refuses to move.

The claim: if >= 25 BTC of market SELLS execute in <= 500ms while the best bid
does not budge, an iceberg is absorbing; when the sellers exhaust the price snaps
UP, on a directional drift lasting 3-8s -- long enough for a 550ms pipeline.

This is testable exactly as stated on 2026-09-01, the one day holding BOTH:
  data/bookticker/BTCUSDT-2026-09-01.csv.gz   (bid/ask at ~114/s)
  data/aggTrades/BTCUSDT-2026-09-01.zip       (prints with taker side)

On Binance spot `is_buyer_maker == True` means the AGGRESSOR WAS THE SELLER.

Three controls, because the event description has two clauses and only one of
them can be doing any work:
  A. volume alone            -- no condition on the bid
  B. volume AND bid frozen   -- the proposal, verbatim
  C. volume AND bid FELL     -- the mirror: was the seller actually absorbed,
                                or did the price simply give way?
  D. unconditional baseline  -- what the mid does anyway

If C is as good as B, the clause is not absorption, it is just volatility.

    python3 tools/absorption_probe.py --symbol BTCUSDT --day 2026-09-01
"""

from __future__ import annotations

import argparse
import io
import zipfile

import numpy as np
import pandas as pd

GRID = 0.1                     # 100ms tape
WIN = 5                        # 500ms window = 5 bins
HORIZONS = (1, 3, 5, 10, 30, 50, 80)   # 100ms .. 8s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--day", default="2026-09-01")
    ap.add_argument("--btc", type=float, default=25.0)
    args = ap.parse_args()
    s = args.symbol

    bt = pd.read_csv(f"data/bookticker/{s}-{args.day}.csv.gz",
                     compression="gzip",
                     usecols=["timestamp", "ask_price", "bid_price"])
    t_bt = bt["timestamp"].to_numpy(dtype=np.int64) / 1e6
    bid = bt["bid_price"].to_numpy(dtype=np.float64)
    mid = (bt["ask_price"].to_numpy(dtype=np.float64) + bid) / 2.0

    with zipfile.ZipFile(f"data/aggTrades/{s}-aggTrades-{args.day}.zip") as z:
        raw = z.read(z.namelist()[0])
    tr = pd.read_csv(io.BytesIO(raw), header=None,
                     skiprows=1 if raw.startswith(b"agg_trade_id") else 0,
                     usecols=[2, 5, 6])
    tr.columns = ["qty", "ts", "maker"]
    ts = tr["ts"].to_numpy(dtype=np.int64)
    t_tr = ts / (1e6 if ts[0] > 1e15 else 1e3)
    qty = tr["qty"].to_numpy(dtype=np.float64)
    sell = tr["maker"].to_numpy().astype(bool)      # aggressor sold

    t0 = max(t_bt.min(), t_tr.min())
    n = int((min(t_bt.max(), t_tr.max()) - t0) / GRID)
    print(f"{s} {args.day}: {n:,} bins of {GRID*1000:.0f}ms; "
          f"{len(t_bt):,} book updates, {len(t_tr):,} prints")

    # aggressive SELL volume per 100ms bin
    vb = np.zeros(n)
    idx = ((t_tr - t0) / GRID).astype(np.int64)
    ok = (idx >= 0) & (idx < n) & sell
    np.add.at(vb, idx[ok], qty[ok])
    vol5 = np.convolve(vb, np.ones(WIN), "full")[:n]

    # book state per bin (last update)
    bi = ((t_bt - t0) / GRID).astype(np.int64)
    okb = (bi >= 0) & (bi < n)
    B = np.full(n, np.nan)
    M = np.full(n, np.nan)
    B[bi[okb]] = bid[okb]
    M[bi[okb]] = mid[okb]
    B = pd.Series(B).ffill().to_numpy()
    M = pd.Series(M).ffill().to_numpy()
    # the mid 500ms later, for the "did the bid give way" clause
    Bf = np.roll(B, -WIN)
    Bf[-WIN:] = np.nan

    print(f"aggressive-sell volume per 500ms: p50 {np.nanmedian(vol5):.3f} BTC, "
          f"p99 {np.nanpercentile(vol5, 99):.2f}, max {np.nanmax(vol5):.2f}")

    started = np.isfinite(vol5) & np.isfinite(M) & np.isfinite(Bf)
    for thr in (args.btc, 5.0, 1.0):
        ev = started & (vol5 >= thr)
        if ev.sum() < 20:
            print(f"  >= {thr:5.1f} BTC /500ms: only {int(ev.sum())} events "
                  f"-- too few to test")
            continue
        frozen = ev & (B == Bf)
        fell = ev & (Bf < B)
        print(f"\n  >= {thr:.1f} BTC of market SELLS in 500ms: "
              f"{int(ev.sum())} events  ({int(frozen.sum())} with the bid "
              f"frozen, {int(fell.sum())} with it lower 500ms later)")
        for name, mask in (("A volume only", ev),
                           ("B volume + bid FROZEN (the claim)", frozen),
                           ("C volume + bid FELL (mirror)", fell)):
            if mask.sum() < 20:
                print(f"    {name:36s} n={int(mask.sum())} -- too few")
                continue
            j = np.nonzero(mask)[0]
            line = f"    {name:36s} n={len(j):5,d}"
            for h in HORIZONS:
                k = j[j + h < n]
                fwd = (M[k + h] - M[k]) / M[k] * 1e4
                line += f"  {h*GRID*1000:>5.0f}ms {np.nanmean(fwd):+5.2f}"
            print(line + "  bps")
        # baseline
        base = []
        for h in HORIZONS:
            fwd = (M[h:] - M[:-h]) / M[:-h] * 1e4
            base.append(f"{h*GRID*1000:.0f}ms {np.nanmean(fwd):+5.2f}")
        print(f"    {'D baseline (all bins)':36s}       " + "  ".join(base))

    print("\n  The claim is an UPWARD snap after absorbed selling. Read the")
    print("  B row against D: that difference, at 3-8s, is the whole idea.")
    print("  If C (bid gave way) is as good or better, the clause is detecting")
    print("  volatility, not absorption.")
    print(f"\n  For scale: dP/dS at the strike is ~0.4/(sigma*sqrt(tau)), about")
    print(f"  9-11c of contract value per bp of spot, and our execution leak is")
    print(f"  measured at +5.5c/share -- so a drift needs to exceed ~0.5bp to")
    print(f"  be visible at all after costs.")


if __name__ == "__main__":
    main()
