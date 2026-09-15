#!/usr/bin/env python3
"""Is the venue's IMPLIED VOLATILITY mispriced? (third-party idea #1)

The claim: a dealer prices P = Phi((S - K) / (sigma * sqrt(tau))). S, K, tau are
deterministic; sigma is the ONLY estimated input, and desks use a slow rolling
historical vol rather than tick-by-tick. If so, sigma is wrong right after vol
regimes change, and the mispricing is tradeable.

Why this is the interesting idea in the batch: it is the ONE class of trade that
the venue's hard `slippageBps <= 10000` cap cannot block. That cap authorises a
fill only up to 2x the reference quote, so it kills anything needing a fast
price move -- but a volatility trade is entered when the price is STATIONARY, so
the cap never bites. If there is an edge left on this venue, it should be here.

Method, per quote:
  sigma_implied = (S - K) / (Phi^-1(mid) * sqrt(tau))     [bps per sqrt(second)]
  sigma_real    = trailing 15-minute realised vol of the SAME underlying,
                  from the 1m bars table, per sqrt(second)
  ratio         = |sigma_implied| / sigma_real
Then buy the favourite (the side the spot is toward) at its ask, binned by ratio.
A dealer with sticky sigma should be biased in the tails: too-high sigma pushes
every price toward 0.50, underpricing the in-the-money side.

Reports per-share edge with day-clustered t, and the pure pricing error (realised
minus the MID, which strips the spread) as a separate column.

Read-only.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import math
import sqlite3
import statistics as st

import numpy as np


def Phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def Phinv(p: float) -> float:
    lo, hi = -8.0, 8.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if Phi(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict_vps.db")
    ap.add_argument("--tau-lo", type=float, default=30.0)
    ap.add_argument("--tau-hi", type=float, default=270.0)
    ap.add_argument("--vol-bars", type=int, default=15)
    args = ap.parse_args()

    c = sqlite3.connect(args.db)
    c.row_factory = sqlite3.Row

    # trailing realised vol per symbol: bps per sqrt(second)
    bars: dict[str, list[tuple[int, float]]] = collections.defaultdict(list)
    for r in c.execute("select symbol, open_ms, close from bars order by open_ms"):
        bars[r["symbol"]].append((int(r["open_ms"]), r["close"]))
    vol: dict[str, tuple[list[int], list[float]]] = {}
    for sym, arr in bars.items():
        arr.sort()
        ts = [a[0] for a in arr]
        px = [a[1] for a in arr]
        lr = [float("nan")] + [math.log(px[i] / px[i - 1]) * 1e4 for i in range(1, len(px))]
        vals = []
        for i in range(len(ts)):
            w = [x for x in lr[max(0, i - args.vol_bars):i] if x == x]
            vals.append(float(np.std(w, ddof=1)) / math.sqrt(60.0) if len(w) >= 8 else None)
        vol[sym] = (ts, vals)

    def sigma_real(sym: str, ts_ms: int) -> float | None:
        v = vol.get(sym)
        if not v:
            return None
        ts, vals = v
        i = bisect.bisect_right(ts, ts_ms) - 1
        if i < 0:
            return None
        return vals[i]

    def sym_of(slug: str) -> str:
        return ("BTCUSDT" if slug.startswith("btc")
                else "ETHUSDT" if slug.startswith("eth") else "BNBUSDT")

    recs = []
    for r in c.execute("""
        select q.slug slug, q.ts_ms ts_ms, q.tau_s tau_s, q.up_ask up_ask,
               q.down_ask down_ask, q.up_bid up_bid, q.down_bid down_bid,
               q.spot spot, q.strike strike,
               r.winner winner, r.start_ms start_ms
        from quotes q join rounds r on r.slug = q.slug
        where r.winner is not null and r.strike is not null and q.spot is not null
          and q.up_ask is not null and q.down_ask is not null
          and q.up_bid is not null and q.down_bid is not null
          and q.tau_s between ? and ?
    """, (args.tau_lo, args.tau_hi)):
        ua, da = r["up_ask"], r["down_ask"]
        ub, db = r["up_bid"], r["down_bid"]
        up_mid = (ub + ua) / 2.0          # the UP side's own mid, NOT (ask+ask)/2
        dn_mid = (db + da) / 2.0
        if not (0.25 <= up_mid <= 0.75) or abs(up_mid - 0.5) < 0.005:
            continue
        sr = sigma_real(sym_of(r["slug"]), r["ts_ms"])
        if not sr or sr <= 0:
            continue
        dist = (r["spot"] - r["strike"]) / r["strike"] * 1e4
        tau = r["tau_s"]
        if tau <= 0 or dist == 0:
            continue
        up_is_fav = dist > 0
        fav_mid = up_mid if up_is_fav else dn_mid
        zi = Phinv(min(max(fav_mid, 1e-4), 1 - 1e-4))
        if zi < 0.02:                     # need a real distance from 0.5
            continue
        s_imp = abs(dist) / (zi * math.sqrt(tau))
        if not (0.02 <= s_imp <= 20):
            continue
        won = (r["winner"] == "Up") if up_is_fav else (r["winner"] == "Down")
        recs.append(dict(
            day=dt.datetime.fromtimestamp(int(r["start_ms"]) / 1000, dt.UTC).strftime("%m-%d"),
            ratio=s_imp / sr, s_imp=s_imp, s_real=sr, dist=abs(dist), tau=tau,
            won=1.0 if won else 0.0,
            ask=ua if up_is_fav else da,
            side_mid=fav_mid,
        ))
    if not recs:
        print("no rows")
        return 0
    rr = np.array([r["ratio"] for r in recs])
    si = np.array([r["s_imp"] for r in recs])
    sr_ = np.array([r["s_real"] for r in recs])
    print(f"{args.db}  rows={len(recs):,}  tau {args.tau_lo:.0f}-{args.tau_hi:.0f}")
    print(f"sigma_implied bps/sqrt(s): median {np.median(si):.3f} (p10 {np.percentile(si,10):.3f}, "
          f"p90 {np.percentile(si,90):.3f})")
    print(f"sigma_real    bps/sqrt(s): median {np.median(sr_):.3f} (p10 {np.percentile(sr_,10):.3f}, "
          f"p90 {np.percentile(sr_,90):.3f})")
    print(f"ratio sigma_implied/sigma_realised: p10 {np.percentile(rr,10):.2f}  "
          f"median {np.median(rr):.2f}  p90 {np.percentile(rr,90):.2f}")

    bins = [(0, 0.5), (0.5, 0.8), (0.8, 1.2), (1.2, 2.0), (2.0, 1e9)]
    print(f"\n  buy the FAVOURITE at its ASK, binned by the vol ratio")
    print(f"  {'ratio bin':>14} {'n':>6} {'ask':>7} {'realized':>9} {'edge':>8} {'day-t':>7} "
          f"| {'mid':>7} {'err vs mid':>10}")
    for lo, hi in bins:
        g = [r for r in recs if lo <= r["ratio"] < hi]
        if len(g) < 50:
            continue
        a = np.mean([r["ask"] for r in g])
        w = np.mean([r["won"] for r in g])
        m = np.mean([r["side_mid"] for r in g])
        bd = collections.defaultdict(list)
        for r in g:
            bd[r["day"]].append(r["won"] - r["ask"])
        dm = [np.mean(v) for v in bd.values()]
        t = (np.mean(dm) / (np.std(dm, ddof=1) / math.sqrt(len(dm)))
             if len(dm) > 1 and np.std(dm, ddof=1) > 0 else 0.0)
        print(f"  {lo:>6.2f}-{hi:<7.2f} {len(g):>6} {a:>7.4f} {w:>9.4f} {w-a:>+8.4f} {t:>+7.2f} "
              f"| {m:>7.4f} {w-m:>+10.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
