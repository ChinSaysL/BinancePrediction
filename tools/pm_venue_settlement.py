"""WHICH RULE DOES EACH VENUE SETTLE ON? Point, or 60s TWAP?

⛔ WHY THIS DECIDES WHETHER THE CROSS ARM CAN MOVE TO BINANCE. The arm's signal
is `crossed_side(fc, level)` where `level = twap_reference(...)` (the 60s mean
at the OPEN) and `fc = twap_forecast(...)`. Neither branches on venue, and both
knobs (`cross_ref_twap`, `cross_fc_trigger`) default True. That is correct for
Polymarket, which pays `TWAP60(bell) >= TWAP60(open)`.

If Binance pays a POINT comparison instead, then on Binance the arm compares
the wrong statistic against the wrong level -- a signal error, not plumbing,
and no amount of venue-agnostic order routing fixes it.

The `rounds` table labels the two eras:
    CHAINLINK                          6703 rounds   Binance era  (<= 09-16)
    chainlink-twap-60s-vs-range-start  4304 rounds   Polymarket   (>= 09-16)

Scored against the venue's OWN recorded winner, using Binance 1-second bars as
the tape -- dense enough for a real 60s window, unlike the sparse early quotes.

    python3 tools/pm_venue_settlement.py
"""
from __future__ import annotations

import glob, io, os, sqlite3, sys, zipfile
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BARS = os.path.join(ROOT, "data", "bars_1s", "daily")
W = 60.0


def load_bars(sym: str, days: set[str]) -> tuple[np.ndarray, np.ndarray]:
    ts, px = [], []
    for d in sorted(days):
        for p in glob.glob(os.path.join(BARS, f"{sym}-1s-{d}.zip")):
            with zipfile.ZipFile(p) as z:
                for n in z.namelist():
                    df = pd.read_csv(io.BytesIO(z.read(n)), header=None,
                                     usecols=[0, 4], names=["t", "c"])
                    t = np.array(df.t.to_numpy(dtype="int64"), copy=True)
                    if t.max() > 10**14:           # microseconds, not ms
                        t = t // 1000
                    ts.append(t); px.append(df.c.to_numpy(dtype=float))
    if not ts:
        return np.array([]), np.array([])
    t = np.concatenate(ts); p = np.concatenate(px)
    o = np.argsort(t)
    return t[o], p[o]


def mean_win(ts, px, lo, hi):
    i = np.searchsorted(ts, lo, "left"); j = np.searchsorted(ts, hi, "right")
    return float(px[i:j].mean()) if j - i >= 3 else None


def point_at(ts, px, at):
    i = np.searchsorted(ts, at, "right") - 1
    return float(px[i]) if 0 <= i < len(px) else None


def main():
    con = sqlite3.connect(f"file:{os.path.join(ROOT,'data','btcpredict.db')}?mode=ro", uri=True)
    r = pd.read_sql_query("""select slug,start_ms,end_ms,strike,winner,feed_provider
        from rounds where winner in ('Up','Down')""", con)
    con.close()
    r["fam"] = r.slug.str.replace(r"-\d+$", "", regex=True)
    r["sym"] = r.fam.str.split("-").str[0].str.upper() + "USDT"
    r["day"] = pd.to_datetime(r.start_ms, unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    r["era"] = np.where(r.feed_provider.fillna("").str.contains("twap", case=False),
                        "POLYMARKET (twap label)", "BINANCE (CHAINLINK label)")
    out = {}
    for sym, g in r.groupby("sym"):
        ts, px = load_bars(sym, set(g.day))
        if len(ts) == 0:
            continue
        for row in g.itertuples():
            lvl_t = mean_win(ts, px, row.start_ms - W * 1000, row.start_ms)
            fin_t = mean_win(ts, px, row.end_ms - W * 1000, row.end_ms)
            p0 = point_at(ts, px, row.start_ms)
            p1 = point_at(ts, px, row.end_ms)
            if None in (lvl_t, fin_t, p0, p1):
                continue
            rec = out.setdefault(row.era, [0, 0, 0, 0])
            rec[0] += 1
            rec[1] += (("Up" if fin_t >= lvl_t else "Down") == row.winner)
            rec[2] += (("Up" if p1 >= p0 else "Down") == row.winner)
            rec[3] += (("Up" if p1 >= (row.strike or p0) else "Down") == row.winner)
    print("=== WHICH RULE REPRODUCES THE VENUE'S OWN WINNER? ===")
    print("  tape: Binance 1-second bars\n")
    print("  %-28s %7s %12s %12s %12s" % ("era", "rounds", "TWAP60vTWAP60",
                                          "point vs p0", "point vs strike"))
    for era, (n, tw, pt, ps) in sorted(out.items()):
        if n < 30: continue
        print("  %-28s %7d %11.1f%% %11.1f%% %11.1f%%"
              % (era, n, 100*tw/n, 100*pt/n, 100*ps/n))
    print()
    print("  If the two eras prefer DIFFERENT rules, then `cross_ref_twap` and")
    print("  `cross_fc_trigger` must be venue-gated: on the point venue the arm")
    print("  would be comparing the wrong statistic to the wrong level.")


if __name__ == "__main__":
    main()
