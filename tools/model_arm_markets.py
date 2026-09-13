"""The MODEL arm on every market, against the venue's own book and settlement.

Why this arm is answerable from the L2 set when the cross arm is not: it takes
ONE position per round through the real `evaluate` gate at tau 60-210, so the
sample size is the ROUND COUNT, not a rare trigger count -- and the winner comes
from the venue, so nothing depends on a synthesized settlement. The cross arm
needs |spot-strike| <= 0.5bps near expiry, which this 4-day set produces ~50
times across 7 coins, and whose outcome a spot-derived settlement only tracks
64% of the time (tools/settlement_bias.py).

Everything the live arm uses is used here: fair_value from (spot, strike, tau,
sigma), the real `evaluate` with the deployed haircut/margins/bands, and
signal_persistence. sigma is realised per-minute vol from a trailing window of
the same 1s tape the bot's estimator reads.
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
from btcpredict.config import settings as S     # noqa: E402
from btcpredict.edge.signal import evaluate     # noqa: E402
from btcpredict.pricing.digital import fair_value   # noqa: E402

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


def sigma_series(px: np.ndarray, win: int = 1800) -> np.ndarray:
    """Realised per-MINUTE sigma from 1s closes, trailing `win` seconds."""
    r = np.zeros_like(px)
    r[1:] = np.diff(np.log(px))
    s2 = pd.Series(r ** 2).rolling(win, min_periods=300).mean().to_numpy()
    return np.sqrt(np.maximum(s2, 0.0) * 60.0)       # 1s -> 1m


def boot(df, col, n=4000, seed=13):
    if df.empty:
        return float("nan"), float("nan"), float("nan")
    g = [x[col].to_numpy() for _, x in df.groupby("slug")]
    rng = np.random.default_rng(seed)
    k = len(g)
    m = np.empty(n)
    for i in range(n):
        m[i] = np.concatenate([g[j] for j in rng.integers(0, k, k)]).mean()
    return float(df[col].mean()), float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="5m,15m")
    ap.add_argument("--coins", default="btc,eth,bnb,sol,xrp,doge,zec")
    ap.add_argument("--fee-bps", type=float, default=200.0)
    a = ap.parse_args()
    coins = a.coins.split(",")

    tapes = {}
    for c in coins:
        ts, px = tape(SYM[c])
        tapes[c] = (ts, px, sigma_series(px))

    print(f"model gate: tau {S.min_tau_s:.0f}-{S.max_tau_s:.0f}s  min_edge {S.min_edge} "
          f"quote [{S.min_quote},{S.max_quote}]  haircut {S.edge_haircut}  "
          f"persistence {S.signal_persistence}  fee {a.fee_bps:.0f}bps")

    for tf in a.tf.split(","):
        dur = DUR[tf]
        d = pd.read_parquet(f"data/hf/updown_{tf}.parquet",
                            columns=["market_slug", "coin", "outcome", "winner",
                                     "seconds_to_close", "best_ask"])
        d = d[d.coin.isin(coins)]
        rows = []
        for slug, g in d.groupby("market_slug"):
            coin = g.coin.iloc[0]
            end = int(slug.rsplit("-", 1)[1]) + dur
            ts, px, sg = tapes[coin]
            i0 = np.searchsorted(ts, (end - dur) * 1000.0, side="right") - 1
            if i0 < 0:
                continue
            strike = float(px[i0])
            won = "Up" if g.winner.iloc[0] == "yes" else "Down"
            w = g[(g.seconds_to_close >= S.min_tau_s) & (g.seconds_to_close <= S.max_tau_s)]
            if w.empty:
                continue
            # one book row per (tau, outcome) -> a quote pair per tau
            piv = w.pivot_table(index="seconds_to_close", columns="outcome",
                                values="best_ask", aggfunc="last")
            if "Up" not in piv or "Down" not in piv:
                continue
            piv = piv.sort_index(ascending=False)        # tau descending = time forward
            run_side, run_n = None, 0
            for tau, r in piv.iterrows():
                qu, qd = r.get("Up"), r.get("Down")
                if not (np.isfinite(qu) and np.isfinite(qd)):
                    continue
                j = np.searchsorted(ts, (end - tau) * 1000.0, side="right") - 1
                if j < 0 or not np.isfinite(sg[j]) or sg[j] <= 0:
                    continue
                fu = fair_value(float(px[j]), strike, float(tau), float(sg[j]))
                sig = evaluate(fu, float(qu), float(qd),
                               half_spread=0.0, safety_margin=S.safety_margin,
                               max_quote=S.max_quote, min_quote=S.min_quote,
                               min_edge=S.min_edge, edge_haircut=S.edge_haircut)
                if not sig.act:
                    run_side, run_n = None, 0
                    continue
                run_n = run_n + 1 if sig.side == run_side else 1
                run_side = sig.side
                if run_n < S.signal_persistence:
                    continue
                ask = qu if sig.side == "Up" else qd
                hit = int(sig.side == won)
                ret = (1 - ask) / ask * (1 - a.fee_bps / 1e4) if hit else -1.0
                rows.append((coin, slug, float(tau), sig.side, float(ask), hit, ret))
                break                                    # one position per round

        t = pd.DataFrame(rows, columns=["coin", "slug", "tau", "side", "ask", "won", "ret"])
        tot = d.drop_duplicates("market_slug").groupby("coin").size()
        print(f"\n=== {tf}  MODEL arm, real book + real settlement ===")
        print(f"{'coin':5s} {'rounds':>7} {'fills':>6} {'fill%':>6} {'ask':>6} "
              f"{'win':>6} {'EV/$1':>7} {'EV 95% CI (by round)':>22}")
        for c in coins:
            x = t[t.coin == c]
            n_r = int(tot.get(c, 0))
            if x.empty:
                print(f"{c:5s} {n_r:7d} {0:6d}   0.0%   --")
                continue
            em, elo, ehi = boot(x, "ret")
            print(f"{c:5s} {n_r:7d} {len(x):6d} {100*len(x)/max(n_r,1):5.1f}% "
                  f"{x.ask.mean():6.3f} {x.won.mean():6.3f} {em:+7.3f} "
                  f"  [{elo:+.3f},{ehi:+.3f}]")
        if not t.empty:
            em, elo, ehi = boot(t, "ret")
            print(f"{'ALL':5s} {int(tot.sum()):7d} {len(t):6d} "
                  f"{100*len(t)/max(int(tot.sum()),1):5.1f}% {t.ask.mean():6.3f} "
                  f"{t.won.mean():6.3f} {em:+7.3f}   [{elo:+.3f},{ehi:+.3f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
