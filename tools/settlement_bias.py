"""How wrong is a spot-derived settlement, ON THE ROUNDS THE ARM ACTUALLY TRADES?

The cross arm's win rate can be measured over a full month from 1s klines, but
only against a settlement synthesized from spot -- the venue settles on
Chainlink. Overall that proxy agrees 88.5% (5m) / 95.7% (15m). That average is
the WRONG correction to apply, because the disagreements concentrate in
sub-2bps rounds and the cross arm trades exactly those.

So measure the agreement rate CONDITIONAL on the arm having triggered, then
invert it. With agreement `q`, a measured win rate relates to the true one by

    w_meas = q * w_true + (1 - q) * (1 - w_true)
    w_true = (w_meas - (1 - q)) / (2q - 1)

Validation: applied to the month replay this must reproduce the independently
known BTC 5m corpus number (792 triggers, win 0.6907) or the correction is wrong.
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

SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "bnb": "BNBUSDT", "sol": "SOLUSDT",
       "xrp": "XRPUSDT", "doge": "DOGEUSDT", "zec": "ZECUSDT"}
DUR = {"5m": 300, "15m": 900}


def tape(sym: str, month: str = "2026-08"):
    with zipfile.ZipFile(f"data/bars_1s/monthly/{sym}-1s-{month}.zip") as z:
        raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=None, usecols=[0, 4], names=["t", "c"])
    t = df.t.to_numpy("float64")
    t = t / 1000.0 if t.max() > 1e15 else t
    p = df.c.to_numpy("float64")
    ok = np.isfinite(p) & (p > 0)
    return t[ok], p[ok]


def invert(w_meas: float, q: float) -> float:
    if q <= 0.5:
        return float("nan")
    return (w_meas - (1.0 - q)) / (2.0 * q - 1.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="btc,eth,bnb,sol,xrp,doge,zec")
    ap.add_argument("--tf", default="5m,15m")
    a = ap.parse_args()
    coins = a.coins.split(",")
    tapes = {c: tape(SYM[c]) for c in coins}

    for tf in a.tf.split(","):
        dur = DUR[tf]
        d = pd.read_parquet(f"data/hf/updown_{tf}.parquet",
                            columns=["market_slug", "coin", "winner"])
        d = d[d.coin.isin(coins)].drop_duplicates("market_slug")
        allr, trig = [], []
        for r in d.itertuples():
            coin = r.coin
            end = int(r.market_slug.rsplit("-", 1)[1]) + dur
            ts, px = tapes[coin]
            i0 = np.searchsorted(ts, (end - dur) * 1000.0, side="right") - 1
            i1 = np.searchsorted(ts, end * 1000.0, side="right") - 1
            if i0 < 0 or i1 <= i0:
                continue
            strike = float(px[i0])
            won = "Up" if r.winner == "yes" else "Down"
            agree = ("Up" if float(px[i1]) > strike else "Down") == won
            allr.append(agree)
            hits = crosses_vec(ts, px, strike, (end - dur) * 1000.0, end * 1000.0,
                               lo=settings.cross_min_tau_s, hi=settings.cross_max_tau_s,
                               look_s=settings.cross_look_s, slack_s=2.0,
                               maxdep=settings.cross_max_depth_bps,
                               cap=settings.cross_max_per_round)
            if hits:
                trig.append(agree)
        A, T = np.array(allr), np.array(trig)
        qa, qt = A.mean(), (T.mean() if len(T) else float("nan"))
        se = (qt * (1 - qt) / len(T)) ** 0.5 if len(T) else float("nan")
        print(f"\n[{tf}] agreement of a spot settlement with the venue")
        print(f"   all rounds        q={qa:.4f}  n={len(A)}")
        print(f"   TRIGGERED rounds  q={qt:.4f}  n={len(T)}  "
              f"95% CI [{qt-1.96*se:.4f}-{qt+1.96*se:.4f}]")
        print(f"   -> a measured win of 0.655 inverts to "
              f"{invert(0.655, qt):.4f} (trigger-conditional) vs "
              f"{invert(0.655, qa):.4f} (all-round)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
