"""DATA INTEGRITY FIRST: can the DB's strike + spot reproduce the venue's winners?

WHY THIS EXISTS. `tools/pm_forecast_edge_check.py` scored the PM terminal
distribution at Brier 0.262 -- worse than a coin flip (0.250) -- against the
venue's 0.112, and in the most printed bucket it claimed p=1.000 where the
realised rate was 39%. A model that is confidently WRONG in the tail is usually
not a bad model; it is a good model fed a bad number. There are exactly two
candidates, and both are checkable against the venue's own published winners:

    1. the STRIKE (rounds.strike, backfilled from the live spot tape), and
    2. the SPOT SERIES (quotes.spot), used both for the displacement and for J.

The venue's own rule is deployed (`pricing.settles_up`), so this compares like
with like: `settles_up(my_strike, my_series)` vs `rounds.winner`. Every round
where they disagree is one of: a wrong strike, a broken spot series, or the
known Binance-vs-Chainlink source residue (~9% of rounds concentrate within
~2 bps of the strike). The FIRST two show up as large, structured disagreement;
the third shows up as a thin band of near-strike rounds.

Usage: PYTHONPATH=/root/BinancePrediction /root/analysis-venv/bin/python \
           tools/pm_strike_integrity.py
"""
from __future__ import annotations

import sqlite3
import sys

import numpy as np
import pandas as pd

from btcpredict.venues.polymarket.pricing import settles_up

DB = sys.argv[1] if len(sys.argv) > 1 else "/root/BinancePrediction/data/btcpredict.db"


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    q = pd.read_sql_query("""
      select slug, ts_ms, tau_s, spot from quotes
      where spot is not null and ts_ms is not null order by slug, ts_ms
    """, con)
    r = pd.read_sql_query("""
      select slug, start_ms, end_ms, strike, winner, feed_provider
      from rounds where strike is not null and winner is not null
    """, con)
    con.close()
    r = r[r.feed_provider.fillna("").str.contains("twap", case=False)]
    r = r[r.slug.str.contains("-5m-")]
    return q, r


def main() -> None:
    q, r = load()
    print(f"PM-era 5m rounds with strike+winner: {len(r)}")
    rows = []
    for rr in r.itertuples():
        g = q[q.slug == rr.slug]
        if len(g) < 30:
            continue
        ts = g.ts_ms.to_numpy(dtype=float)
        sp = g.spot.to_numpy(dtype=float)
        end_ms = float(rr.end_ms)
        samples = list(zip(ts, sp))
        got = settles_up(samples, float(rr.strike), end_ms=end_ms)
        # the spot AT the round's start, from this same series
        start_near = ts[np.argsort(np.abs(ts - float(rr.start_ms)))][:5]
        first = float(sp[np.argmin(np.abs(ts - float(rr.start_ms)))])
        # the window's realised average vs the strike, in bps
        m = (ts >= end_ms - 60_000) & (ts <= end_ms)
        win_avg = float(np.mean(sp[m])) if m.sum() else np.nan
        disp = (1e4 * (win_avg - rr.strike) / rr.strike
                if np.isfinite(win_avg) else np.nan)
        strike_vs_first = 1e4 * (rr.strike - first) / rr.strike
        cover = float((ts.max() - ts.min()) / 1000.0)
        rows.append(dict(slug=rr.slug, winner=rr.winner, mine=got, disp=disp,
                         strike_vs_first=strike_vs_first, cover=cover,
                         n=len(g),
                         end_gap=(float(ts.max()) - end_ms) / 1000.0))
    d = pd.DataFrame(rows)
    if d.empty:
        print("no rounds with a usable series"); return
    ok = d[d.mine.notna()]
    agree = (ok.mine == (ok.winner == "Up")).mean()
    print(f"\n--- A. DOES THE DEPLOYED RULE ON MY SERIES REPRODUCE THE VENUE? ---")
    print(f"    rounds      {len(ok)}")
    print(f"    agreement   {agree:.1%}   (the rule was identified at 90.7% on Binance "
          f"1s klines; ~9% residue is Binance-vs-Chainlink source noise)")
    print(f"    my-label Up {ok.mine.mean():.1%} | venue Up {(ok.winner=='Up').mean():.1%}")

    print(f"\n--- B. IS THE STRIKE THE ROUND'S START PRICE? ---")
    s = d.strike_vs_first.dropna()
    print(f"    |strike - spot(first quote)| bps: median {s.abs().median():.2f}  "
          f"p90 {s.abs().quantile(.9):.2f}  p99 {s.abs().quantile(.99):.2f}  "
          f"max {s.abs().max():.1f}")
    print(f"    over 5 bps away: {(s.abs() > 5).mean():.1%} of rounds  "
          f"<- these cannot be the round's start price")

    print(f"\n--- C. IS THE SERIES USABLE AT THE BELL? ---")
    print(f"    series span: median {d.cover.median():.0f}s  min {d.cover.min():.0f}s")
    print(f"    last sample vs round end: median {d.end_gap.median():+.1f}s  "
          f"p10 {d.end_gap.quantile(.1):+.1f}s  (negative = series runs past the bell)")
    have_win = d.disp.notna()
    print(f"    rounds with a covered 60s window: {have_win.mean():.1%}")

    print(f"\n--- D. WHERE THE DISAGREEMENTS LIVE (|displacement| at the bell) ---")
    bad = ok[(ok.mine != (ok.winner == "Up"))]
    good = ok[(ok.mine == (ok.winner == "Up"))]
    print(f"    agree   n={len(good):4d}  |disp| median {good.disp.abs().median():.2f} bps")
    print(f"    disagree n={len(bad):4d}  |disp| median {bad.disp.abs().median():.2f} bps")
    print("    (if the disagreements are NOT concentrated near 0 bps, the strike or "
          "the series is wrong,\n     not the source noise)")


if __name__ == "__main__":
    main()
