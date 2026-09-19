"""THE LOCKED-IN FAVOURITE: does the tail edge survive DELAY and HELD-OUT DAYS?

The scored set shows a monotone favourite-longshot bias -- favourites at
P>=0.90 realise 91.5% against a market-implied 88.3%, longshots the mirror --
and betting the favourite returns +$0.11 per $1 at tau 5-60 against +$0.004 at
tau 60-300. The mechanism: near the bell the paid TWAP window is partly
PRINTED, so the outcome is close to determined while the book is still walking
the price to 1.00. The venue's fee curve helps -- 0.07*p*(1-p) is 33bps at
0.95 against 364bps at 0.50.

⛔ TWO WAYS THIS DIES, both tested here rather than argued:

  1. DELAY. Every replay in this repo prices at the tick's ask. The crossing
     arm scored +6.2 points there and +0.9 one second later. If this is the
     same race it collapses the same way. It may not: "the outcome is locked"
     is not a print the venue reacts to, it is a slow drift toward 1.00.
  2. FITTING. The probability is scored on the tape it was computed on.
     Held-out days are the only honest check.
"""
from __future__ import annotations

import math, os, sqlite3
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEE = lambda p: 0.07 * p * (1 - p)
PF, TLO, THI = 0.95, 5.0, 60.0


def main():
    db = os.path.join(ROOT, "data", "btcpredict.db")
    d = pd.read_pickle(os.path.join(ROOT, "data", ".twapfx.pkl"))
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    r = pd.read_sql_query("select slug,end_ms from rounds", con)
    q = pd.read_sql_query("""select slug,ts_ms,up_ask,down_ask from quotes
        where up_ask is not null and down_ask is not null order by ts_ms""", con)
    con.close()
    fwd = {s: (g.ts_ms.to_numpy(),
               g.up_ask.to_numpy(dtype=float), g.down_ask.to_numpy(dtype=float))
           for s, g in q.groupby("slug")}
    end = dict(zip(r.slug, r.end_ms))

    d = d[np.isfinite(d.ua) & np.isfinite(d.da) & (d.ua > .005) & (d.ua < .995)
          & (d.da > .005) & (d.da < .995)].copy()
    # the frame carries no timestamp; tau and the bell give it back exactly
    d["ts"] = d.slug.map(end) - (d.tau * 1000.0)
    d = d[np.isfinite(d.ts)]
    d["day"] = pd.to_datetime(d.ts, unit="ms", utc=True).dt.strftime("%m-%d")

    p = d.p_up.to_numpy()
    side = np.where(p >= PF, "Up", np.where((1 - p) >= PF, "Down", ""))
    keep = (side != "") & (d.tau.to_numpy() >= TLO) & (d.tau.to_numpy() < THI)
    sub, side = d[keep], side[keep]
    won = np.where(side == "Up", sub.up.to_numpy(), 1 - sub.up.to_numpy())
    print("scored ticks %d | tail bets at P>=%.2f, tau %d-%d: %d | days %s\n"
          % (len(d), PF, TLO, THI, len(sub), sorted(d.day.unique())))

    def ask_at(slug, ts, sd, delay):
        t = fwd.get(slug)
        if t is None:
            return np.nan
        tt, uu, dd = t
        k = int(np.searchsorted(tt, ts + delay, "left"))
        arr = uu if sd == "Up" else dd
        while k < len(arr):
            v = float(arr[k])
            if np.isfinite(v) and 0.005 < v < 0.995:
                return v
            k += 1
        return np.nan

    print("=" * 74)
    print("1. DOES IT SURVIVE DELAY?")
    print("=" * 74)
    print("  %-10s %8s %8s %10s %11s %9s" % ("delay", "n", "win%", "mean ask", "$/bet", "t"))
    asks = {}
    for delay in (0, 500, 1000, 2000, 5000):
        a = np.array([ask_at(s, int(t), sd, delay)
                      for s, t, sd in zip(sub.slug, sub.ts, side)])
        asks[delay] = a
        m = np.isfinite(a)
        if m.sum() < 50:
            print("  %-10s %8d  (too few)" % ("%dms" % delay, m.sum())); continue
        pnl = (1.0 / a[m]) * (won[m] - a[m] - FEE(a[m]))
        sd_ = pnl.std(ddof=1)
        t_ = pnl.mean() / (sd_ / math.sqrt(m.sum())) if sd_ else 0.0
        print("  %-10s %8d %7.1f%% %10.4f %+11.4f %+9.2f"
              % ("%dms" % delay, m.sum(), 100 * won[m].mean(), a[m].mean(), pnl.mean(), t_))

    print()
    print("=" * 74)
    print("2. HELD-OUT DAYS  (priced at 1000ms, the measured live decision->fill)")
    print("=" * 74)
    a = asks[1000]
    m = np.isfinite(a)
    s2, w2, a2 = sub[m], won[m], a[m]
    days = sorted(s2.day.unique())
    cut = len(days) // 2
    tr, te = set(days[:cut]), set(days[cut:])
    print("  train %s\n  test  %s\n" % (sorted(tr), sorted(te)))
    print("  %-8s %8s %8s %10s %11s %9s" % ("split", "n", "win%", "mean ask", "$/bet", "t"))
    for lab, ds in (("train", tr), ("test", te)):
        k = s2.day.isin(ds).to_numpy()
        if k.sum() < 50:
            print("  %-8s %8d (too few)" % (lab, k.sum())); continue
        pnl = (1.0 / a2[k]) * (w2[k] - a2[k] - FEE(a2[k]))
        sd_ = pnl.std(ddof=1)
        print("  %-8s %8d %7.1f%% %10.4f %+11.4f %+9.2f"
              % (lab, k.sum(), 100 * w2[k].mean(), a2[k].mean(), pnl.mean(),
                 pnl.mean() / (sd_ / math.sqrt(k.sum())) if sd_ else 0.0))
    print()
    print("  per day (test half is what counts):")
    print("  %-8s %7s %8s %11s" % ("day", "n", "win%", "$/bet"))
    for dd in days:
        k = (s2.day == dd).to_numpy()
        if k.sum() < 20: continue
        pnl = (1.0 / a2[k]) * (w2[k] - a2[k] - FEE(a2[k]))
        print("  %-8s %7d %7.1f%% %+11.4f%s"
              % (dd, k.sum(), 100 * w2[k].mean(), pnl.mean(), "  <- test" if dd in te else ""))


if __name__ == "__main__":
    main()
