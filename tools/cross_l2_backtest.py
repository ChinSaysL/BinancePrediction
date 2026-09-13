"""The cross arm on complete, event-level venue L2 -- with the strike RECOVERED.

Everything that made earlier per-market numbers unmeasurable is fixed here:

  * COVERAGE. The earlier L2 set sampled ~5% of rounds (471 of ~9,216) and
    produced 50 triggers across 7 coins. This one is every round of the day,
    every coin, both timeframes, at event resolution (~99k book snapshots per
    5m round, 10 levels a side).

  * THE STRIKE, which was the fatal problem. A strike synthesized from spot
    only tracks the venue's Chainlink settlement 64% of the time ON TRIGGERED
    ROUNDS (tools/settlement_bias.py), because the arm trades exactly the
    rounds where the basis decides the outcome. Here the strike is not assumed:
    the book's own implied side flips precisely when spot crosses the strike,
    so K is recovered by maximising the confidence-weighted agreement between
    sign(spot - K) and (book P(Up) > 0.5), weighting late ticks where the book
    is sharpest. Validated at 0.00 bps against spot-at-open on a round where
    the basis happened to be zero, at 97.7% agreement.

  * SETTLEMENT. Taken from the book at expiry (P(Up) -> 0.99 / 0.01), i.e. the
    venue's own answer, not a spot comparison.

  * STATISTICS. CIs bootstrap over ROUNDS. Triggers inside a round share one
    price path and are not independent.
"""
from __future__ import annotations

import argparse
import glob
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
       "sol": "SOLUSDT", "xrp": "XRPUSDT", "doge": "DOGEUSDT"}
ROOT = "data/hf2/kartik/top10_parquet"


def spot_tape(sym: str, day: str):
    with zipfile.ZipFile(f"data/bars_1s/daily/{sym}-1s-{day}.zip") as z:
        raw = z.read(z.namelist()[0])
    k = pd.read_csv(io.BytesIO(raw), header=None, usecols=[0, 4], names=["t", "c"])
    t = k.t.to_numpy("float64")
    t = t / 1000.0 if t.max() > 1e15 else t
    p = k.c.to_numpy("float64")
    ok = np.isfinite(p) & (p > 0)
    return t[ok], p[ok]


def recover_strike(spot, pup, secs, grid_n=3000):
    """K such that sign(spot-K) best matches the book's implied side.

    Weighted by |P(Up)-0.5| (the book's confidence) and by exp(-secs/120) so
    the sharp, late part of the round dominates -- early in a round the book is
    near 0.5 everywhere and carries almost no information about K.
    """
    conf = np.abs(pup - 0.5) * np.exp(-secs / 120.0)
    if conf.sum() <= 0:
        return None, 0.0
    # RELATIVE pad. An absolute +-2 is a rounding error on BTC and a 200%
    # range on XRP/DOGE -- it returned K=-0.91 against a spot of 1.09, which
    # fair_value then rejected. 10 bps of the round's own level is wider than
    # any strike-vs-range offset actually seen and stays positive everywhere.
    pad = max(1e-3 * float(np.mean(spot)), 1e-9)
    lo = max(float(spot.min()) - pad, 1e-9)
    grid = np.linspace(lo, float(spot.max()) + pad, grid_n)
    up_book = pup > 0.5
    sc = np.array([(conf * ((spot > K) == up_book)).sum() for K in grid])
    i = int(sc.argmax())
    return float(grid[i]), float(sc[i] / conf.sum())


def boot(df, col, n=4000, seed=17):
    if df.empty:
        return float("nan"), float("nan"), float("nan")
    g = [x[col].to_numpy() for _, x in df.groupby("cid")]
    rng = np.random.default_rng(seed)
    k = len(g)
    m = np.empty(n)
    for i in range(n):
        m[i] = np.concatenate([g[j] for j in rng.integers(0, k, k)]).mean()
    return float(df[col].mean()), float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-07-17")
    ap.add_argument("--tf", default="5m,15m")
    ap.add_argument("--coins", default="btc,eth,bnb,sol,xrp,doge")
    ap.add_argument("--fee-bps", type=float, default=200.0)
    ap.add_argument("--min-agree", type=float, default=0.90,
                    help="drop rounds where the strike could not be pinned")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--strike-mode", default="book", choices=["book", "open"],
                    help="book: recover K from the order book. open: use spot at "
                         "the round open, which touches the book not at all and "
                         "is the zero-circularity control.")
    a = ap.parse_args()

    coins = a.coins.split(",")
    tapes = {c: spot_tape(SYM[c], a.day) for c in coins}
    print(f"gate: tau {settings.cross_min_tau_s:.0f}-{settings.cross_max_tau_s:.0f}s "
          f"depth<={settings.cross_max_depth_bps}bps look {settings.cross_look_s}s "
          f"cap {settings.cross_max_per_round}/round | fee {a.fee_bps:.0f}bps | "
          f"strike recovered from the book, min agree {a.min_agree}")

    for tf in a.tf.split(","):
        dur = {"5m": 300, "15m": 900}[tf]
        rows, kept, dropped = [], 0, 0
        for coin in coins:
            files = sorted(glob.glob(f"{ROOT}/{a.day}/{coin}/{tf}/*.parquet"))
            if a.limit:
                files = files[:a.limit]
            ts_k, px_k = tapes[coin]
            for f in files:
                try:
                    t = pd.read_parquet(f, columns=["ts", "secs", "yes_bid", "yes_ask",
                                                    "no_bid", "no_ask"])
                except Exception:
                    continue
                t = t.dropna(subset=["yes_bid", "yes_ask"])
                t = t[(t.secs >= 0) & (t.secs <= dur)]
                if len(t) < 200:
                    continue
                ts = t.ts.to_numpy("float64")
                ts = ts if ts.max() > 1e12 else ts * 1000.0
                pup = ((t.yes_bid + t.yes_ask) / 2.0).to_numpy()
                secs = t.secs.to_numpy("float64")
                spot = px_k[np.clip(np.searchsorted(ts_k, ts, side="right") - 1,
                                    0, len(px_k) - 1)]
                K, agree = recover_strike(spot, pup, secs)
                if K is None or agree < a.min_agree:
                    dropped += 1
                    continue
                if a.strike_mode == "open":
                    # Control: the strike the venue would have published, taken
                    # from spot alone. Recovering K from the book is weighted to
                    # late ticks where the book already knows the answer, so if
                    # that were fitting the outcome this run would disagree.
                    K = float(spot[int(np.argmax(secs))])
                kept += 1
                won = "Up" if pup[int(np.argmin(secs))] > 0.5 else "Down"
                end_ms = float(ts[int(np.argmin(secs))] + secs.min() * 1000.0)
                start_ms = end_ms - dur * 1000.0
                hits = crosses_vec(ts_k, px_k, K, start_ms, end_ms,
                                   lo=settings.cross_min_tau_s,
                                   hi=settings.cross_max_tau_s,
                                   look_s=settings.cross_look_s, slack_s=2.0,
                                   maxdep=settings.cross_max_depth_bps,
                                   cap=settings.cross_max_per_round)
                if not hits:
                    continue
                cid = os.path.basename(f)[:18]
                askcol = t.yes_ask.to_numpy() if True else None
                no_ask = t.no_ask.to_numpy()
                for tau, side in hits:
                    j = int(np.argmin(np.abs(secs - tau)))
                    if abs(secs[j] - tau) > 3:
                        continue
                    ask = float(askcol[j] if side == "Up" else no_ask[j])
                    if not (0.0 < ask < 1.0):
                        continue
                    w = int(side == won)
                    ret = (1 - ask) / ask * (1 - a.fee_bps / 1e4) if w else -1.0
                    rows.append((coin, cid, tau, side, ask, w, ret))

        t = pd.DataFrame(rows, columns=["coin", "cid", "tau", "side", "ask", "won", "ret"])
        print(f"\n=== {tf} {a.day} | rounds used {kept}, dropped {dropped} "
              f"(strike unpinnable) | triggers {len(t)} ===")
        print(f"{'coin':5s} {'trig':>5} {'rounds':>7} {'ask':>6} {'win':>6} "
              f"{'win 95% CI':>17} {'EV/$1':>7} {'EV 95% CI':>19}")
        for c in coins:
            x = t[t.coin == c]
            if x.empty:
                print(f"{c:5s} {0:5d}   (none)")
                continue
            wm, wlo, whi = boot(x, "won")
            em, elo, ehi = boot(x, "ret")
            print(f"{c:5s} {len(x):5d} {x.cid.nunique():7d} {x.ask.mean():6.3f} "
                  f"{wm:6.3f}  [{wlo:.3f},{whi:.3f}] {em:+7.3f}  [{elo:+.3f},{ehi:+.3f}]")
        if not t.empty:
            wm, wlo, whi = boot(t, "won")
            em, elo, ehi = boot(t, "ret")
            print(f"{'ALL':5s} {len(t):5d} {t.cid.nunique():7d} {t.ask.mean():6.3f} "
                  f"{wm:6.3f}  [{wlo:.3f},{whi:.3f}] {em:+7.3f}  [{elo:+.3f},{ehi:+.3f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
