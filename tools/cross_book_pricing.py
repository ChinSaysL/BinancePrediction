"""Per-market cross-arm economics from the venue's own L2 book.

The recorded-only markets in MARKETS have no book (main.py gates the fetch on
`trade`), so the bot's corpus gives win rates but no entry price -- and win
rate alone cannot decide a market, because break-even is ~0.665 at live fills.

This prices the trigger instead, on data already on disk:
  signal  1s Binance klines (spot), strike synthesized at the round open
  price   data/hf/updown_{5m,15m}.parquet -- the venue's real L2, ~1 snap/s
  truth   the parquet's own `winner` (round-level: 'yes' == Up won)

The synthesized strike reproduces the venue's Chainlink settlement 91.4% (5m)
and 94.5% (15m); the error is concentrated in sub-2bps rounds, so it biases the
measured win rate DOWN. The ASK is not affected by it at all, which is the
number this tool exists for.
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
from btcpredict.config import settings              # noqa: E402
from tools.instrument_ab import crosses_vec         # noqa: E402

SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "bnb": "BNBUSDT",
       "sol": "SOLUSDT", "xrp": "XRPUSDT", "doge": "DOGEUSDT", "zec": "ZECUSDT"}


def load_tape(sym: str) -> tuple[np.ndarray, np.ndarray]:
    with zipfile.ZipFile(f"data/bars_1s/monthly/{sym}-1s-2026-08.zip") as z:
        raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=None, usecols=[0, 4], names=["t", "c"])
    t = df.t.to_numpy("float64")
    t = t / 1000.0 if t.max() > 1e15 else t
    return t, df.c.to_numpy("float64")


def px_at(ts, px, w):
    i = np.searchsorted(ts, w, side="right") - 1
    return float(px[i]) if i >= 0 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="5m", choices=["5m", "15m", "4h"])
    ap.add_argument("--coins", default="btc,eth,bnb")
    ap.add_argument("--lo", type=float, default=None)
    ap.add_argument("--hi", type=float, default=None)
    a = ap.parse_args()

    dur = {"5m": 300, "15m": 900, "4h": 14400}[a.tf]
    lo = a.lo if a.lo is not None else settings.cross_min_tau_s
    hi = a.hi if a.hi is not None else settings.cross_max_tau_s
    coins = a.coins.split(",")

    d = pd.read_parquet(f"data/hf/updown_{a.tf}.parquet",
                        columns=["market_slug", "coin", "outcome", "winner",
                                 "seconds_to_close", "best_bid", "best_ask"])
    d = d[d.coin.isin(coins)]
    tapes = {c: load_tape(SYM[c]) for c in coins}

    print(f"{a.tf}  tau {lo:.0f}-{hi:.0f}s  depth<={settings.cross_max_depth_bps}bps  "
          f"look {settings.cross_look_s}s  cap {settings.cross_max_per_round}/round")

    rows = []
    for slug, g in d.groupby("market_slug"):
        coin = g.coin.iloc[0]
        end = int(slug.rsplit("-", 1)[1]) + dur      # slug epoch is the OPEN
        start = end - dur
        ts, px = tapes[coin]
        strike = px_at(ts, px, start * 1000.0)
        if strike is None:
            continue
        won = "Up" if g.winner.iloc[0] == "yes" else "Down"
        hits = crosses_vec(ts, px, strike, start * 1000.0, end * 1000.0,
                           lo=lo, hi=hi, look_s=settings.cross_look_s, slack_s=2.0,
                           maxdep=settings.cross_max_depth_bps,
                           cap=settings.cross_max_per_round)
        if not hits:
            continue
        for tau, side in hits:
            b = g[g.outcome == side]
            if b.empty:
                continue
            k = (b.seconds_to_close - tau).abs().idxmin()
            if abs(b.loc[k, "seconds_to_close"] - tau) > 3:
                continue          # no book within 3s of the trigger
            rows.append((coin, slug, tau, side, float(b.loc[k, "best_ask"]),
                         float(b.loc[k, "best_bid"]), int(side == won)))

    t = pd.DataFrame(rows, columns=["coin", "slug", "tau", "side",
                                    "ask", "bid", "won"])
    if t.empty:
        print("  no triggers priced")
        return 0

    print(f"\n{'coin':5s} {'n':>4} {'rounds':>7} {'win':>6} {'ask':>6} {'spread':>7} "
          f"{'EV/$1':>7} {'EV%':>7}")
    for c in coins:
        x = t[t.coin == c]
        if x.empty:
            print(f"{c:5s}    0   (no triggers)")
            continue
        win, ask = x.won.mean(), x.ask.mean()
        ev = win - ask                      # $1 payoff, pay `ask`
        print(f"{c:5s} {len(x):4d} {x.slug.nunique():7d} {win:6.3f} {ask:6.3f} "
              f"{(x.ask-x.bid).mean():7.3f} {ev:+7.3f} {100*ev/ask:+6.1f}%")

    x = t
    win, ask = x.won.mean(), x.ask.mean()
    print(f"{'ALL':5s} {len(x):4d} {x.slug.nunique():7d} {win:6.3f} {ask:6.3f} "
          f"{(x.ask-x.bid).mean():7.3f} {win-ask:+7.3f} {100*(win-ask)/ask:+6.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
