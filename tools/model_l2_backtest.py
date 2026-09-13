"""The MODEL arm on complete, event-level venue L2, with the recovered strike.

Same data and same strike recovery as tools/cross_l2_backtest.py, but through
the real `evaluate` gate at tau 60-210 with one position per round. Because the
arm does not need a rare trigger, n is the ROUND count -- 1,728 rounds at 5m
across six coins for a single day, against 418 in the older sampled set.

sigma is realised per-minute vol from a trailing window of the same 1s tape the
live estimator reads. Settlement is the book's own at expiry.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings as S                     # noqa: E402
from btcpredict.edge.signal import evaluate                     # noqa: E402
from btcpredict.pricing.digital import fair_value               # noqa: E402
from tools.cross_l2_backtest import SYM, ROOT, spot_tape, recover_strike, boot  # noqa: E402


def sigma_series(px: np.ndarray, win: int = 1800) -> np.ndarray:
    r = np.zeros_like(px)
    r[1:] = np.diff(np.log(px))
    s2 = pd.Series(r ** 2).rolling(win, min_periods=300).mean().to_numpy()
    return np.sqrt(np.maximum(s2, 0.0) * 60.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-07-17")
    ap.add_argument("--tf", default="5m,15m")
    ap.add_argument("--coins", default="btc,eth,bnb,sol,xrp,doge")
    ap.add_argument("--fee-bps", type=float, default=200.0)
    ap.add_argument("--min-agree", type=float, default=0.90)
    ap.add_argument("--strike-mode", default="book", choices=["book", "open"],
                    help="book: recover K from the order book. open: spot at the "
                         "round open, the zero-circularity control. The cross arm "
                         "swings from +0.231 to +0.053 between these two because "
                         "its 0.5bps gate is finer than the strike is known; this "
                         "arm trades far from ATM and should barely move.")
    a = ap.parse_args()

    coins = a.coins.split(",")
    tapes = {}
    for c in coins:
        ts, px = spot_tape(SYM[c], a.day)
        tapes[c] = (ts, px, sigma_series(px))

    print(f"model gate: tau {S.min_tau_s:.0f}-{S.max_tau_s:.0f}s min_edge {S.min_edge} "
          f"quote [{S.min_quote},{S.max_quote}] haircut {S.edge_haircut} "
          f"persistence {S.signal_persistence} | fee {a.fee_bps:.0f}bps")

    for tf in a.tf.split(","):
        dur = {"5m": 300, "15m": 900}[tf]
        rows, used = [], 0
        for coin in coins:
            ts_k, px_k, sg = tapes[coin]
            for f in sorted(glob.glob(f"{ROOT}/{a.day}/{coin}/{tf}/*.parquet")):
                try:
                    t = pd.read_parquet(f, columns=["ts", "secs", "yes_bid", "yes_ask",
                                                    "no_bid", "no_ask"])
                except Exception:
                    continue
                t = t.dropna(subset=["yes_bid", "yes_ask", "no_ask"])
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
                    continue
                if a.strike_mode == "open":
                    K = float(spot[int(np.argmax(secs))])
                used += 1
                won = "Up" if pup[int(np.argmin(secs))] > 0.5 else "Down"
                qu = t.yes_ask.to_numpy()
                qd = t.no_ask.to_numpy()
                idx = np.where((secs >= S.min_tau_s) & (secs <= S.max_tau_s))[0]
                idx = idx[np.argsort(-secs[idx])]        # time forward
                sig_i = np.clip(np.searchsorted(ts_k, ts, side="right") - 1,
                                0, len(px_k) - 1)
                run_side, run_n = None, 0
                cid = os.path.basename(f)[:18]
                # one book update per ~30ms is far finer than the live poll:
                # subsample to ~1/s so persistence means what it means live
                seen = set()
                for i in idx:
                    s_i = int(secs[i])
                    if s_i in seen:
                        continue
                    seen.add(s_i)
                    sgv = sg[sig_i[i]]
                    if not np.isfinite(sgv) or sgv <= 0:
                        continue
                    if not (0 < qu[i] < 1 and 0 < qd[i] < 1):
                        continue
                    fu = fair_value(float(spot[i]), K, float(secs[i]), float(sgv))
                    sg_ = evaluate(fu, float(qu[i]), float(qd[i]),
                                   half_spread=0.0, safety_margin=S.safety_margin,
                                   max_quote=S.max_quote, min_quote=S.min_quote,
                                   min_edge=S.min_edge, edge_haircut=S.edge_haircut)
                    if not sg_.act:
                        run_side, run_n = None, 0
                        continue
                    run_n = run_n + 1 if sg_.side == run_side else 1
                    run_side = sg_.side
                    if run_n < S.signal_persistence:
                        continue
                    ask = float(qu[i] if sg_.side == "Up" else qd[i])
                    w = int(sg_.side == won)
                    ret = (1 - ask) / ask * (1 - a.fee_bps / 1e4) if w else -1.0
                    rows.append((coin, cid, float(secs[i]), sg_.side, ask, w, ret))
                    break

        t = pd.DataFrame(rows, columns=["coin", "cid", "tau", "side", "ask", "won", "ret"])
        print(f"\n=== {tf} {a.day} MODEL | rounds used {used} | fills {len(t)} ===")
        print(f"{'coin':5s} {'fills':>6} {'ask':>6} {'win':>6} {'win 95% CI':>17} "
              f"{'EV/$1':>7} {'EV 95% CI':>19}")
        for c in coins:
            x = t[t.coin == c]
            if x.empty:
                print(f"{c:5s} {0:6d}   (none)")
                continue
            wm, wlo, whi = boot(x, "won")
            em, elo, ehi = boot(x, "ret")
            print(f"{c:5s} {len(x):6d} {x.ask.mean():6.3f} {wm:6.3f}  "
                  f"[{wlo:.3f},{whi:.3f}] {em:+7.3f}  [{elo:+.3f},{ehi:+.3f}]")
        if not t.empty:
            wm, wlo, whi = boot(t, "won")
            em, elo, ehi = boot(t, "ret")
            print(f"{'ALL':5s} {len(t):6d} {t.ask.mean():6.3f} {wm:6.3f}  "
                  f"[{wlo:.3f},{whi:.3f}] {em:+7.3f}  [{elo:+.3f},{ehi:+.3f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
