"""Cross-arm signal for every coin x timeframe over a full month.

The 31-day corpus (data/btc5m_prices.parquet) that produced the shipped cross
numbers is BTC 5m ONLY, so it cannot say anything about the other families the
multi-market experiment is about. The August 1s-kline dumps DO cover a whole
month for every symbol, so rounds are synthesized on UTC boundaries and the
arm's own trigger is replayed against them.

What this is and is not:
  * rounds and settlement are synthesized from Binance spot, not the venue's
    Chainlink oracle. Measured against 186 real venue rounds, a spot-derived
    settlement agrees 91.4% at 5m and 94.5% at 15m, the error concentrated in
    sub-2bps rounds -- so treat the LEVEL as indicative and the RANKING and the
    TRIGGER RATE as the output.
  * no book. Pricing per market comes from data/hf/updown_*.parquet
    (tools/cross_book_pricing.py); this file is the signal half only.

Day-clustered t is reported because the repo's bar is day-t >= 2, and triggers
inside one day are not independent.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import zipfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings          # noqa: E402
from tools.instrument_ab import crosses_vec     # noqa: E402

SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "bnb": "BNBUSDT",
       "sol": "SOLUSDT", "xrp": "XRPUSDT", "doge": "DOGEUSDT", "zec": "ZECUSDT"}
DUR = {"5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


def load_month(sym: str, month: str) -> tuple[np.ndarray, np.ndarray]:
    with zipfile.ZipFile(f"data/bars_1s/monthly/{sym}-1s-{month}.zip") as z:
        raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=None, usecols=[0, 4], names=["t", "c"])
    t = df.t.to_numpy("float64")
    t = t / 1000.0 if t.max() > 1e15 else t
    px = df.c.to_numpy("float64")
    ok = np.isfinite(px) & (px > 0)
    return t[ok], px[ok]


def run(ts, px, dur, lo, hi):
    """Every trigger in the month. Returns (day, tau, won) rows."""
    step = dur * 1000
    first = int(np.ceil(ts[0] / step) * step)
    last = int(np.floor(ts[-1] / step) * step)
    rows = []
    for start in range(first, last, step):
        end = start + step
        # slice by index: masking the whole month per round is O(n) per round
        i0 = np.searchsorted(ts, start - 1000, side="left")
        i1 = np.searchsorted(ts, end, side="right")
        if i1 - i0 < 10:
            continue
        t, p = ts[i0:i1], px[i0:i1]
        strike = float(p[0])
        hits = crosses_vec(t, p, strike, float(start), float(end),
                           lo=lo, hi=hi, look_s=settings.cross_look_s, slack_s=2.0,
                           maxdep=settings.cross_max_depth_bps,
                           cap=settings.cross_max_per_round)
        if not hits:
            continue
        settled = "Up" if float(p[-1]) > strike else "Down"
        day = pd.Timestamp(start, unit="ms", tz="UTC").strftime("%Y-%m-%d")
        for tau, side in hits:
            rows.append((day, tau, int(side == settled)))
    return pd.DataFrame(rows, columns=["day", "tau", "won"])


def day_t(df: pd.DataFrame, base: float) -> float:
    """t on the per-day mean win rate against `base` -- the repo's bar."""
    g = df.groupby("day").won.mean()
    if len(g) < 2:
        return float("nan")
    return float((g.mean() - base) / (g.std(ddof=1) / np.sqrt(len(g))))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-08")
    ap.add_argument("--coins", default="btc,eth,bnb")
    ap.add_argument("--tfs", default="5m,15m,1h,1d")
    ap.add_argument("--scale-tau", action="store_true",
                    help="scale the tau gate with the round length (default: absolute)")
    a = ap.parse_args()

    print(f"month {a.month}  depth<={settings.cross_max_depth_bps}bps  "
          f"look {settings.cross_look_s}s  cap {settings.cross_max_per_round}/round  "
          f"tau {'scaled' if a.scale_tau else 'absolute'} "
          f"{settings.cross_min_tau_s:.0f}-{settings.cross_max_tau_s:.0f}s")
    print(f"\n{'market':16s} {'rounds/d':>8} {'n':>6} {'trig/d':>7} {'win':>6} "
          f"{'95% CI':>15} {'day-t vs .5':>11}")

    out = {}
    for coin in a.coins.split(","):
        ts, px = load_month(SYM[coin], a.month)
        days = (ts[-1] - ts[0]) / 86400_000.0
        for tf in a.tfs.split(","):
            dur = DUR[tf]
            sc = dur / 300.0 if a.scale_tau else 1.0
            lo, hi = settings.cross_min_tau_s * sc, settings.cross_max_tau_s * sc
            df = run(ts, px, dur, lo, hi)
            name = f"{coin}-updown-{tf}"
            if df.empty:
                print(f"{name:16s} {86400/dur:8.0f} {0:6d}   (no triggers)")
                continue
            n, w = len(df), df.won.mean()
            se = (w * (1 - w) / n) ** 0.5
            print(f"{name:16s} {86400/dur:8.0f} {n:6d} {n/days:7.1f} {w:6.3f} "
                  f"  [{w-1.96*se:.3f}-{w+1.96*se:.3f}] {day_t(df,0.5):11.2f}")
            out[name] = df
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
