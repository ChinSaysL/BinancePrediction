"""CAN A SWEEP MAKE THE LOCKED TAIL PROFITABLE? Yes. That is the problem.

This runs a genuine configuration sweep over the tail arm's knobs, reports the
best cell -- and then runs the IDENTICAL sweep against SHUFFLED OUTCOMES,
where by construction there is no edge at all. If the shuffled sweep finds
cells that look just as good, the real one has found nothing.

⛔ WHY THIS TOOL EXISTS. Every gate on the cross arm was chosen by a sweep of
this shape, on a few days, priced at the tick, scored with tick-level
t-statistics. That arm is measurably negative live: n=319, win 55.5% against a
61.1% break-even, -2.0 sd. The sweeps did not fail to find an optimum -- they
found one that was not there. `cross_max_margin_bps` was removed this morning
because a sweep preferred it off; the live record then said entries past 1.0
bps of margin win 38.5% against 57.6% implied.

    python3 tools/pm_tail_sweep.py
"""
from __future__ import annotations

import math, os, sqlite3, sys
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEE = lambda p: 0.07 * p * (1 - p)


def load():
    d = pd.read_pickle(os.path.join(ROOT, "data", ".twapfx.pkl"))
    con = sqlite3.connect(f"file:{os.path.join(ROOT,'data','btcpredict.db')}?mode=ro", uri=True)
    r = pd.read_sql_query("select slug,end_ms from rounds", con)
    q = pd.read_sql_query("""select slug,ts_ms,up_ask,down_ask from quotes
        where up_ask is not null and down_ask is not null order by ts_ms""", con)
    con.close()
    fwd = {s: (g.ts_ms.to_numpy(), g.up_ask.to_numpy(dtype=float),
               g.down_ask.to_numpy(dtype=float)) for s, g in q.groupby("slug")}
    end = dict(zip(r.slug, r.end_ms))
    d = d[np.isfinite(d.ua) & np.isfinite(d.da) & (d.ua > .005) & (d.ua < .995)
          & (d.da > .005) & (d.da < .995)].copy()
    d["ts"] = d.slug.map(end) - (d.tau * 1000.0)
    d = d[np.isfinite(d.ts)]
    d["day"] = pd.to_datetime(d.ts, unit="ms", utc=True).dt.strftime("%m-%d")
    # the +1000ms ask, once, for every tick -- the sweep only reslices it
    def ask_at(slug, ts, sd):
        f = fwd.get(slug)
        if f is None: return np.nan
        tt, uu, dd = f
        i = int(np.searchsorted(tt, ts + 1000, "left"))
        arr = uu if sd == "Up" else dd
        while i < len(arr):
            v = float(arr[i])
            if np.isfinite(v) and .005 < v < .995: return v
            i += 1
        return np.nan
    d["ask_up"] = [ask_at(s, int(t), "Up") for s, t in zip(d.slug, d.ts)]
    d["ask_dn"] = [ask_at(s, int(t), "Down") for s, t in zip(d.slug, d.ts)]
    return d


def cell(d, up_outcome, min_p, min_edge, tau_lo, tau_hi, max_px):
    p = d.p_up.to_numpy()
    side = np.where(p >= min_p, 1, np.where((1 - p) >= min_p, -1, 0))
    ask = np.where(side == 1, d.ask_up.to_numpy(), d.ask_dn.to_numpy())
    psd = np.where(side == 1, p, 1 - p)
    won = np.where(side == 1, up_outcome, 1 - up_outcome)
    tau = d.tau.to_numpy()
    ok = ((side != 0) & (tau >= tau_lo) & (tau < tau_hi) & np.isfinite(ask)
          & (ask > 0.01) & (ask <= max_px)
          & ((psd - ask - FEE(ask)) >= min_edge))
    if ok.sum() < 100:
        return None
    f = pd.DataFrame({"slug": d.slug.to_numpy()[ok], "day": d.day.to_numpy()[ok],
                      "ts": d.ts.to_numpy()[ok], "ask": ask[ok], "won": won[ok]})
    g = f.sort_values("ts").groupby("slug", as_index=False).first()
    pnl = (1.0 / g.ask.to_numpy()) * (g.won.to_numpy() - g.ask.to_numpy()
                                      - FEE(g.ask.to_numpy()))
    if len(pnl) < 50 or pnl.std(ddof=1) == 0:
        return None
    t = pnl.mean() / (pnl.std(ddof=1) / math.sqrt(len(pnl)))
    dm = g.assign(pnl=pnl).groupby("day").pnl.mean().to_numpy()
    dt = (dm.mean() / (dm.std(ddof=1) / math.sqrt(len(dm)))
          if len(dm) > 1 and dm.std(ddof=1) else 0.0)
    return dict(n=len(pnl), per=pnl.mean(), t=t, dayt=dt, tot=pnl.sum())


GRID = [(mp, me, tl, th, mx)
        for mp in (0.80, 0.85, 0.90, 0.95, 0.98)
        for me in (0.00, 0.01, 0.02, 0.05)
        for tl in (5.0, 10.0, 20.0)
        for th in (30.0, 45.0, 60.0)
        for mx in (0.90, 0.95, 0.97, 0.99)]


def sweep(d, outcome, label):
    best, rows = None, []
    for mp, me, tl, th, mx in GRID:
        if tl >= th: continue
        c = cell(d, outcome, mp, me, tl, th, mx)
        if c is None: continue
        c.update(mp=mp, me=me, tl=tl, th=th, mx=mx)
        rows.append(c)
        if best is None or c["per"] > best["per"]:
            best = c
    rows.sort(key=lambda x: -x["per"])
    print("  %s: %d live cells of %d" % (label, len(rows), len(GRID)))
    print("    %-34s %6s %9s %8s %8s" % ("best cells by $/bet", "n", "$/bet", "t", "day-t"))
    for c in rows[:5]:
        print("    P>=%.2f edge>=%.2f tau%2.0f-%2.0f px<=%.2f %6d %+9.4f %+8.2f %+8.2f"
              % (c["mp"], c["me"], c["tl"], c["th"], c["mx"], c["n"], c["per"], c["t"], c["dayt"]))
    sig = [c for c in rows if c["t"] >= 2.0]
    print("    cells with t >= 2.0 : %d  (%.1f%% of live cells)"
          % (len(sig), 100 * len(sig) / max(len(rows), 1)))
    return rows


def main():
    d = load()
    print("ticks %d | rounds %d | days %s\n"
          % (len(d), d.slug.nunique(), sorted(d.day.unique())))
    y = d.up.to_numpy()
    print("=" * 78)
    print("1. THE REAL SWEEP")
    print("=" * 78)
    real = sweep(d, y, "real outcomes")
    print()
    print("=" * 78)
    print("2. THE SAME SWEEP ON SHUFFLED OUTCOMES -- there is NO edge here by")
    print("   construction. Whatever it finds is what chance alone provides.")
    print("=" * 78)
    rng = np.random.default_rng(7)
    fakes = []
    for k in range(3):
        # shuffle the outcome BY ROUND so within-round structure is preserved
        per = d.groupby("slug").up.first()
        sh = pd.Series(rng.permutation(per.to_numpy()), index=per.index)
        fake = d.slug.map(sh).to_numpy().astype(float)
        fakes.append(sweep(d, fake, "shuffle #%d" % (k + 1)))
        print()
    print("=" * 78)
    print("3. THE VERDICT")
    print("=" * 78)
    rb = real[0]["per"] if real else 0
    fb = [f[0]["per"] for f in fakes if f]
    print("  best $/bet on REAL outcomes     : %+.4f" % rb)
    print("  best $/bet on SHUFFLED outcomes : %s" % ", ".join("%+.4f" % x for x in fb))
    if fb and rb <= max(fb):
        print("\n  ⛔ the sweep finds an equally good config in data with NO edge.")
        print("     A profitable cell here is evidence of nothing.")
    else:
        print("\n  the real best exceeds every shuffle -- weak evidence, not proof.")


if __name__ == "__main__":
    main()
