"""Ship / don't-ship per market, from historical data only.

ONE consistent backtest instead of numbers assembled from different sources:
every trade is a REAL venue round, priced at the REAL L2 ask that was resting,
scored against the REAL settled winner. Nothing synthesized except the strike.

  rounds + book + winner   data/hf/updown_{5m,15m}.parquet  (venue's own L2,
                           ~1 snapshot/s, full depth, settled winner)
  spot / trigger           Binance 1s klines
  strike                   spot at the round open. This is the ONLY synthesized
                           input, and its error is MEASURED here rather than
                           assumed: `--bias` reports how often a spot-derived
                           settlement agrees with the venue's Chainlink one.

Statistics: triggers inside one round are not independent (consecutive crosses
alternate sides on the same price path), so every CI is a bootstrap over ROUNDS,
not over triggers. A per-trigger CI is roughly sqrt(k) too narrow for k triggers
per round and is what made earlier per-market numbers look decisive.
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
DUR = {"5m": 300, "15m": 900, "4h": 14400}


def tape(sym: str, month: str = "2026-08"):
    with zipfile.ZipFile(f"data/bars_1s/monthly/{sym}-1s-{month}.zip") as z:
        raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=None, usecols=[0, 4], names=["t", "c"])
    t = df.t.to_numpy("float64")
    t = t / 1000.0 if t.max() > 1e15 else t
    p = df.c.to_numpy("float64")
    ok = np.isfinite(p) & (p > 0)
    return t[ok], p[ok]


def boot_rounds(df, col, n=4000, seed=11):
    """Bootstrap a mean over ROUNDS. df has one row per trigger."""
    if df.empty:
        return float("nan"), float("nan"), float("nan")
    g = [x[col].to_numpy() for _, x in df.groupby("slug")]
    rng = np.random.default_rng(seed)
    k = len(g)
    means = np.empty(n)
    for i in range(n):
        pick = rng.integers(0, k, k)
        means[i] = np.concatenate([g[j] for j in pick]).mean()
    return float(df[col].mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="5m,15m")
    ap.add_argument("--coins", default="btc,eth,bnb,sol,xrp,doge,zec")
    ap.add_argument("--fee-bps", type=float, default=200.0)
    ap.add_argument("--bias", action="store_true", help="report the strike/settlement bias")
    a = ap.parse_args()

    coins = a.coins.split(",")
    tapes = {c: tape(SYM[c]) for c in coins}
    print(f"gate: tau {settings.cross_min_tau_s:.0f}-{settings.cross_max_tau_s:.0f}s  "
          f"depth<={settings.cross_max_depth_bps}bps  look {settings.cross_look_s}s  "
          f"cap {settings.cross_max_per_round}/round  fee {a.fee_bps:.0f}bps")

    for tf in a.tf.split(","):
        dur = DUR[tf]
        d = pd.read_parquet(f"data/hf/updown_{tf}.parquet",
                            columns=["market_slug", "coin", "outcome", "winner",
                                     "seconds_to_close", "best_bid", "best_ask"])
        d = d[d.coin.isin(coins)]
        rows, bias = [], []
        for slug, g in d.groupby("market_slug"):
            coin = g.coin.iloc[0]
            end = int(slug.rsplit("-", 1)[1]) + dur      # slug epoch is the OPEN
            ts, px = tapes[coin]
            i0 = np.searchsorted(ts, (end - dur) * 1000.0, side="right") - 1
            i1 = np.searchsorted(ts, end * 1000.0, side="right") - 1
            if i0 < 0 or i1 <= i0:
                continue
            strike = float(px[i0])
            won = "Up" if g.winner.iloc[0] == "yes" else "Down"
            bias.append(("Up" if float(px[i1]) > strike else "Down") == won)
            hits = crosses_vec(ts, px, strike, (end - dur) * 1000.0, end * 1000.0,
                               lo=settings.cross_min_tau_s, hi=settings.cross_max_tau_s,
                               look_s=settings.cross_look_s, slack_s=2.0,
                               maxdep=settings.cross_max_depth_bps,
                               cap=settings.cross_max_per_round)
            for tau, side in hits:
                b = g[g.outcome == side]
                if b.empty:
                    continue
                k = (b.seconds_to_close - tau).abs().idxmin()
                if abs(b.loc[k, "seconds_to_close"] - tau) > 3:
                    continue
                ask = float(b.loc[k, "best_ask"])
                if not (0.0 < ask < 1.0):
                    continue
                w = int(side == won)
                # $1 staked: win -> (1-ask)/ask before fee on the profit
                ret = (1.0 - ask) / ask * (1.0 - a.fee_bps / 1e4) if w else -1.0
                rows.append((coin, slug, tau, ask, w, ret))

        t = pd.DataFrame(rows, columns=["coin", "slug", "tau", "ask", "won", "ret"])
        if a.bias:
            b = np.array(bias)
            print(f"\n[{tf}] spot-derived settlement agrees with the venue on "
                  f"{b.mean():.3f} of {len(b)} rounds "
                  f"(the strike's only effect; it biases win rates DOWN)")
        print(f"\n=== {tf}  REAL rounds / REAL book / REAL winner ===")
        print(f"{'coin':5s} {'trig':>5} {'rounds':>7} {'ask':>6} {'win':>6} "
              f"{'win 95% CI (by round)':>24} {'EV/$1':>7} {'EV 95% CI':>18}")
        for c in coins:
            x = t[t.coin == c]
            if x.empty:
                print(f"{c:5s} {0:5d}   (no triggers)")
                continue
            wm, wlo, whi = boot_rounds(x, "won")
            em, elo, ehi = boot_rounds(x, "ret")
            print(f"{c:5s} {len(x):5d} {x.slug.nunique():7d} {x.ask.mean():6.3f} "
                  f"{wm:6.3f}   [{wlo:.3f}-{whi:.3f}]      {em:+7.3f} "
                  f" [{elo:+.3f},{ehi:+.3f}]")
        if not t.empty:
            wm, wlo, whi = boot_rounds(t, "won")
            em, elo, ehi = boot_rounds(t, "ret")
            print(f"{'ALL':5s} {len(t):5d} {t.slug.nunique():7d} {t.ask.mean():6.3f} "
                  f"{wm:6.3f}   [{wlo:.3f}-{whi:.3f}]      {em:+7.3f} "
                  f" [{elo:+.3f},{ehi:+.3f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
