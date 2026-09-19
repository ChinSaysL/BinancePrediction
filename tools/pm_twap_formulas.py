"""TWAP FORECASTS, BUILT FROM SCRATCH, SCORED AGAINST THE MARKET.

Nothing here imports the arm's forecast code. The point is to re-derive the
object the venue pays on, check the derivation against the venue's own
settlements, and then ask the only question that matters: does the model beat
the PRICE, not whether it beats a naive forecast.

    settle Up  <=>  TWAP60(bell) >= TWAP60(open),  TWAP60(t) = mean spot [t-60,t]

At ts with tau seconds left the paid window is [ts+tau-60, ts+tau].

  tau <  60   printed [ts-(60-tau), ts] (duration 60-tau) + future [ts, ts+tau]
              TWAP = ((60-tau)*M_printed + tau*M_future)/60
              M_printed is the mean over the RECENT (60-tau) seconds ONLY.
  tau >= 60   the whole window is future; for a martingale the optimal point
              forecast is the spot itself.

⛔ THE FLAW THIS TOOL EXISTS TO TEST. The arm compares a POINT forecast to the
level and buys on the sign. The venue pays on a PROBABILITY. With arithmetic
BM at per-second vol sigma:

    Var(TWAP60(bell) | ts) =  (tau/60)^2 * sigma^2 * tau/3     tau <  60
                              sigma^2 * (tau - 40)             tau >= 60
    P(Up) = Phi((forecast - level) / sqrt(Var))

At tau=240 a 1 bps gap is a coin flip; at tau=10 it is near certain. A crossing
rule treats them identically, which is why a floor in BPS cannot fix it -- the
right threshold is in STANDARD DEVIATIONS.

    python3 tools/pm_twap_formulas.py
"""
from __future__ import annotations

import math, os, sqlite3, sys
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "btcpredict.db")
W = 60.0            # the venue's TWAP lookback, seconds
FEE = lambda p: 0.07 * p * (1 - p)


def load():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    r = pd.read_sql_query("""select slug,start_ms,end_ms,winner,feed_provider
        from rounds where winner in ('Up','Down') and start_ms is not null""", con)
    q = pd.read_sql_query("""select slug,ts_ms,tau_s,spot,up_ask,down_ask
        from quotes where spot is not null and spot>0 order by ts_ms""", con)
    con.close()
    r = r[r.feed_provider.fillna("").str.contains("twap", case=False)]
    q["fam"] = q.slug.str.replace(r"-\d+$", "", regex=True)
    r["fam"] = r.slug.str.replace(r"-\d+$", "", regex=True)
    return r.reset_index(drop=True), q


def mean_window(ts, px, lo_ms, hi_ms):
    """Mean of the tape over [lo, hi]. None if the window is not covered."""
    i = np.searchsorted(ts, lo_ms, "left")
    j = np.searchsorted(ts, hi_ms, "right")
    if j - i < 3:
        return None
    return float(px[i:j].mean())


def main():
    r, q = load()
    print("rounds %d   quote rows %d\n" % (len(r), len(q)))
    tapes = {f: (g.ts_ms.to_numpy(), g.spot.to_numpy(dtype=float))
             for f, g in q.groupby("fam")}

    # ---------------------------------------------------------------- 1. truth
    print("=" * 74)
    print("1. CAN I REPRODUCE THE VENUE'S OWN SETTLEMENT FROM THE TAPE?")
    print("   If not, every number below is meaningless.")
    print("=" * 74)
    ok = bad = skip = 0
    truth = {}
    for row in r.itertuples():
        t = tapes.get(row.fam)
        if t is None:
            skip += 1; continue
        ts, px = t
        lvl = mean_window(ts, px, row.start_ms - W * 1000, row.start_ms)
        fin = mean_window(ts, px, row.end_ms - W * 1000, row.end_ms)
        if lvl is None or fin is None:
            skip += 1; continue
        pred = "Up" if fin >= lvl else "Down"
        truth[row.slug] = (lvl, fin, row.winner)
        ok += (pred == row.winner); bad += (pred != row.winner)
    tot = ok + bad
    print("  reproduced %d of %d settlements = %.1f%%   (skipped %d, tape gap)"
          % (ok, tot, 100 * ok / max(tot, 1), skip))
    if tot and ok / tot < 0.80:
        print("\n  ⛔ GROUND TRUTH IS NOT RELIABLE -- stopping. The tape cannot")
        print("     reconstruct the venue's statistic, so no forecast built on")
        print("     it can be trusted either.")
        return
    print("  -> good enough to score against.\n")

    # ---------------------------------------------------------------- 2. score
    print("=" * 74)
    print("2. FORECASTS AT EVERY TICK, AND THE PROBABILITY THEY IMPLY")
    print("=" * 74)
    rec = []
    for row in r.itertuples():
        if row.slug not in truth:
            continue
        lvl, fin, winner = truth[row.slug]
        t = tapes.get(row.fam)
        ts, px = t
        sub = q[(q.slug == row.slug)]
        if sub.empty:
            continue
        # per-second vol from this round's own tape, arithmetic
        i0 = np.searchsorted(ts, row.start_ms - 300_000, "left")
        i1 = np.searchsorted(ts, row.start_ms, "right")
        seg = px[i0:i1]
        if len(seg) < 30:
            continue
        sig = float(np.diff(seg).std())          # per-sample (~1s) move
        if not np.isfinite(sig) or sig <= 0:
            continue
        for tsm, tau, spot, ua, da in zip(sub.ts_ms, sub.tau_s, sub.spot,
                                          sub.up_ask, sub.down_ask):
            if not (5.0 <= tau <= 260.0) or not spot or spot <= 0:
                continue
            if tau < W:
                mp = mean_window(ts, px, tsm - (W - tau) * 1000, tsm)
                if mp is None:
                    continue
                f_new = ((W - tau) * mp + tau * spot) / W
                var = (tau / W) ** 2 * sig ** 2 * tau / 3.0
            else:
                f_new = float(spot)
                var = sig ** 2 * (tau - W + W / 3.0)
            # the shipped shape: whole trailing minute, at every tau
            m60 = mean_window(ts, px, tsm - W * 1000, tsm)
            f_old = (((W - tau) * m60 + tau * spot) / W) if (m60 and tau < W) else float(spot)
            sd = math.sqrt(max(var, 1e-18))
            p_up = 0.5 * (1.0 + math.erf((f_new - lvl) / (sd * math.sqrt(2.0))))
            rec.append((row.slug, float(tau), float(spot), lvl, fin, winner,
                        f_new, f_old, p_up,
                        float(ua) if ua and np.isfinite(ua) else np.nan,
                        float(da) if da and np.isfinite(da) else np.nan))
    if not rec:
        print("  no usable ticks"); return
    d = pd.DataFrame(rec, columns=["slug","tau","spot","lvl","fin","winner",
                                   "f_new","f_old","p_up","ua","da"])
    d["up"] = (d.winner == "Up").astype(float)
    print("  ticks scored: %d over %d rounds\n" % (len(d), d.slug.nunique()))

    print("  --- POINT FORECAST ACCURACY: |forecast - realised TWAP60(bell)| in bps ---")
    print("  %-12s %7s %10s %10s %10s" % ("tau", "n", "spot", "shipped", "NEW"))
    for lo, hi in ((5,30),(30,60),(60,120),(120,180),(180,260)):
        m = d.tau.between(lo, hi)
        if m.sum() < 50: continue
        e_s = 1e4*(d.spot[m]-d.fin[m]).abs()/d.lvl[m]
        e_o = 1e4*(d.f_old[m]-d.fin[m]).abs()/d.lvl[m]
        e_n = 1e4*(d.f_new[m]-d.fin[m]).abs()/d.lvl[m]
        print("  %3d-%-8d %7d %9.3f %10.3f %10.3f%s"
              % (lo,hi,m.sum(),e_s.mean(),e_o.mean(),e_n.mean(),
                 "  <- best" if e_n.mean()<=min(e_s.mean(),e_o.mean()) else ""))
    d.to_pickle(os.path.join(ROOT, "data", ".twapfx.pkl"))
    print("\n  scored frame saved to data/.twapfx.pkl for the market comparison")


if __name__ == "__main__":
    main()
