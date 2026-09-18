"""Run the pre-cross cell on whatever the live DB holds, split by feed_provider.

Read-only. Written to be scp'd to the VPS and run with the repo venv python:
    .venv/bin/python /root/BinancePrediction/tools/pm_precross_live_check.py
"""
from __future__ import annotations

import collections
import sqlite3
import sys

import numpy as np
import pandas as pd

DB = sys.argv[1] if len(sys.argv) > 1 else "/root/BinancePrediction/data/btcpredict.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
q = pd.read_sql_query("""
 select q.slug, q.ts_ms, q.tau_s, q.spot, q.up_ask, q.down_ask, q.up_bid, q.down_bid,
        r.strike, r.winner, r.feed_provider
 from quotes q join rounds r on q.slug = r.slug
 where r.strike is not null and r.winner is not null and q.spot is not null
   and q.tau_s between 5 and 60
 order by q.slug, q.ts_ms
""", con)
con.close()
print(f"rows {len(q)} rounds {q.slug.nunique()}")
q["d_bps"] = 1e4 * (q.spot - q.strike) / q.strike
q["m_bps"] = 1e4 * (q.spot - q.groupby("slug", sort=False)["spot"].shift(3)) / q.strike
q["side"] = np.where(q.d_bps > 0, "Down", "Up")
q["toward"] = np.where(q.d_bps > 0, q.m_bps < 0, q.m_bps > 0)
q["ask"] = np.where(q.side == "Down", q.down_ask, q.up_ask)
q["fav_ask"] = np.where(q.side == "Down", q.up_ask, q.down_ask)
q["won"] = ((q.side == "Down") & (q.winner == "Down")) | \
           ((q.side == "Up") & (q.winner == "Up"))
q["day"] = pd.to_datetime(q.ts_ms, unit="ms", utc=True).dt.strftime("%m-%d")
q["fam"] = q.slug.str.rsplit("-", n=1).str[0]
q["prov"] = q.feed_provider.fillna("none")


def show(s, label, pad=0.0):
    if len(s) < 8:
        print(f"  {label:<30} n={len(s)} (thin)")
        return
    a = s.ask + pad
    v = np.where(s.won, (1.0 - 0.07 * a * (1 - a)) / a - 1.0, -1.0)
    n = len(v); m = v.mean(); sd = v.std()
    t = m / (sd / np.sqrt(n)) if sd else 0.0
    dm = [v[s.day.values == d].mean() for d in s.day.unique()]
    td = np.mean(dm) / (np.std(dm) / np.sqrt(len(dm))) if np.std(dm) else 0.0
    h = n // 2
    per = (s.won.astype(float) - a - 0.07 * a * (1 - a)).mean()
    print(f"  {label:<30} n={n:4d} ask={s.ask.mean():.4f} win={s.won.mean():6.1%} "
          f"per-share={per:+.4f} $/1={m:+.3f} t={t:+.2f} day-t={td:+.2f} "
          f"halves={v[:h].mean():+.3f}/{v[h:].mean():+.3f} days+={sum(1 for x in dm if x > 0)}/{len(dm)}")


for prov in sorted(q.prov.unique()):
    s = q[(q.prov == prov) & q.toward & (q.d_bps.abs() <= 1.0)
          & q.slug.str.contains("-5m-")].dropna(subset=["ask", "won"])
    if not len(s):
        continue
    f = s.groupby("slug", sort=False).head(1).copy()
    print(f"\n=== feed_provider = {prov}  (5m only, |d|<=1bps toward, one entry/round) ===")
    show(f, "ALL")
    for lo, hi in [(0, 0.25), (0.25, 0.5), (0.5, 1.0)]:
        c = f[(f.d_bps.abs() >= lo) & (f.d_bps.abs() < hi)]
        show(c, f"  |d| {lo}-{hi}")
    print("  --- per family ---")
    for fam in sorted(f.fam.unique()):
        show(f[f.fam == fam], fam)
    print("  --- ask pad (realized fill slip, measured: median +0.006) ---")
    for pad in (0.0, 0.01, 0.02, 0.03):
        show(f, f"  pad +{pad:.2f}", pad)
    print("  --- mirror control (buy the favourite) ---")
    m = f.copy()
    m["ask"] = m.fav_ask
    m["won"] = ~m.won
    show(m, "  mirror")
    print("  --- by tau at entry ---")
    for lo, hi in [(5, 20), (20, 40), (40, 60)]:
        show(f[f.tau_s.between(lo, hi)], f"  tau {lo}-{hi}")
print("\nreference (Binance-wrapper era, 455 rounds): ask 0.4242 win 49.9% "
      "per-share +0.0587 t +3.70 day-t +4.85 halves +0.300/+0.435 7/8   "
      "mirror -0.1461 t -5.21 0/8   survives a +0.02 pad (+0.0385, t +3.22)")
