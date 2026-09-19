"""IS THERE A FORECAST WORTH HAVING ON A POINT VENUE?

Polymarket pays TWAP60(bell), part of which is PRINTED before the bell -- so a
projection carries information the spot does not (MAE 0.349 bps against the
spot's 1.456 at tau 5-30). Binance pays the BELL PRICE. For a driftless
martingale the optimal forecast of a future point IS the current spot, and no
formula beats it.

⛔ BUT "DRIFTLESS MARTINGALE" IS AN ASSUMPTION, NOT A MEASUREMENT. If the spot
carries momentum or mean-reversion over the 5-60s the arm trades, a forecast
beats it and the arm should use one. That is testable on the Binance-era tape:
5,924 rounds with 1-second bars.

Candidates, all computed at `ts` with `tau` seconds to the bell:
    spot    S_now                       the martingale baseline
    mom     S_now + k*(S_now - S_-k)    momentum: recent move continues
    rev     S_now - k*(S_now - S_-k)    reversion: recent move gives back
    ewma    exponentially-weighted mean of the recent tape
Scored by MAE against the REALISED bell price, by tau bucket. Anything that
beats `spot` is a forecast; nothing that does is.

    python3 tools/pm_binance_forecast.py
"""
from __future__ import annotations

import glob, io, os, sqlite3, zipfile
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BARS = os.path.join(ROOT, "data", "bars_1s", "daily")


def load_bars(sym, days):
    ts, px = [], []
    for d in sorted(days):
        for p in glob.glob(os.path.join(BARS, f"{sym}-1s-{d}.zip")):
            with zipfile.ZipFile(p) as z:
                for n in z.namelist():
                    df = pd.read_csv(io.BytesIO(z.read(n)), header=None,
                                     usecols=[0, 4], names=["t", "c"])
                    t = np.array(df.t.to_numpy(dtype="int64"), copy=True)
                    if t.max() > 10**14:
                        t = t // 1000
                    ts.append(t); px.append(df.c.to_numpy(dtype=float))
    if not ts:
        return np.array([]), np.array([])
    t = np.concatenate(ts); p = np.concatenate(px)
    o = np.argsort(t)
    return t[o], p[o]


def main():
    con = sqlite3.connect(f"file:{os.path.join(ROOT,'data','btcpredict.db')}?mode=ro", uri=True)
    r = pd.read_sql_query("""select slug,start_ms,end_ms,winner,feed_provider
        from rounds where winner in ('Up','Down')""", con)
    con.close()
    r = r[~r.feed_provider.fillna("").str.contains("twap", case=False)]   # Binance era
    r["fam"] = r.slug.str.replace(r"-\d+$", "", regex=True)
    r["sym"] = r.fam.str.split("-").str[0].str.upper() + "USDT"
    r["day"] = pd.to_datetime(r.start_ms, unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    TAUS = (10, 20, 30, 45, 60)
    LAG = 10          # seconds of recent move the momentum/reversion terms use
    err = {k: {t: [] for t in TAUS} for k in ("spot", "mom", "rev", "ewma")}
    n_used = 0
    for sym, g in r.groupby("sym"):
        ts, px = load_bars(sym, set(g.day))
        if len(ts) == 0:
            continue
        for row in g.itertuples():
            j = np.searchsorted(ts, row.end_ms, "right") - 1
            if j < 0 or j >= len(px):
                continue
            bell = float(px[j])
            ok = False
            for tau in TAUS:
                i = np.searchsorted(ts, row.end_ms - tau * 1000, "right") - 1
                k = np.searchsorted(ts, row.end_ms - (tau + LAG) * 1000, "right") - 1
                if i < 0 or k < 0 or i >= len(px) or ts[i] < row.start_ms:
                    continue
                s, s_lag = float(px[i]), float(px[k])
                seg = px[max(0, i - 30):i + 1]
                if len(seg) < 5:
                    continue
                w = np.exp(np.linspace(-2.0, 0.0, len(seg)))
                ew = float((seg * w).sum() / w.sum())
                d = s - s_lag
                for name, f in (("spot", s), ("mom", s + d), ("rev", s - d), ("ewma", ew)):
                    err[name][tau].append(abs(f - bell) / bell * 1e4)
                ok = True
            n_used += ok
    print("=== MAE vs the REALISED BELL PRICE, in bps  (Binance era, %d rounds) ===" % n_used)
    print("  %-8s %8s %9s %9s %9s %9s" % ("tau", "n", "spot", "mom", "rev", "ewma"))
    for tau in TAUS:
        n = len(err["spot"][tau])
        if n < 100: continue
        vals = {k: float(np.mean(err[k][tau])) for k in err}
        best = min(vals, key=vals.get)
        print("  %-8d %8d %9.3f %9.3f %9.3f %9.3f   best=%s"
              % (tau, n, vals["spot"], vals["mom"], vals["rev"], vals["ewma"], best))
    print()
    print("  If `spot` wins every row, there is no forecast to build: the bell")
    print("  price is a martingale over this horizon and the arm is already")
    print("  using the optimal estimate of it.")


if __name__ == "__main__":
    main()
