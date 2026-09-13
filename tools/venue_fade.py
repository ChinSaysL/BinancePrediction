"""Is the venue's mispricing of FLAT windows exploitable in the other direction?

Measured (tools/venue_price_curve.py): when |spot-strike| is tiny and there are
20-120s left, the venue charges 0.55-0.81 for a side that only wins 48-73%.
An overpriced favourite means the opposite side is underpriced. Same trade,
same fee, other direction.

This is deliberately the OPPOSITE of the gist's rule (it fires late, on the
dominant side). Here we buy early, on the cheap side, when delta is noise.

Hard requirement: both real asks must have been recorded. The displayed price
lags the venue's own book by up to 0.40, so a "derived" opposite price can
invent an edge that does not exist. Rows without both asks are dropped.

EV per $1 staked:  win -> (1-p)/p, lose -> -1, fee -> rate*min(1,(1-p)/p)
Battery on every cell: pooled-t, day-clustered t, both halves, drop-best-day.
One row per round per cell.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

TAU_BUCKETS = [(3, 8), (8, 20), (20, 60), (60, 120)]
DELTA_BUCKETS = [(0.0, 0.005), (0.005, 0.01), (0.01, 0.02), (0.02, 0.05),
                 (0.05, 99.0)]

SQL = """
with x as (
  select q.slug, q.ts_ms, q.tau_s, q.spot, q.up_price, q.down_price,
         q.up_ask, q.down_ask, r.strike, r.winner, r.fee_rate_bps, r.end_ms,
         row_number() over (partition by q.slug order by q.ts_ms desc) as rn
  from quotes q join rounds r on r.slug = q.slug
  where q.spot is not null and r.strike is not null
    and q.up_ask is not null and q.down_ask is not null
    and q.tau_s >= ? and q.tau_s < ?
)
select slug, ts_ms, tau_s, spot, up_ask, down_ask, strike, winner,
       fee_rate_bps, end_ms
from x where rn = 1
"""


def fetch(db: str, lo: float, hi: float) -> pd.DataFrame:
    import sqlite3
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        df = pd.read_sql_query(SQL, con, params=(lo, hi))
    finally:
        con.close()
    if df.empty:
        return df
    df["delta_pct"] = (df["spot"] - df["strike"]) / df["strike"] * 100.0
    df["side"] = np.where(df["delta_pct"] > 0, "up", "down")
    df["px_in"] = np.where(df["side"] == "up", df["up_ask"], df["down_ask"])
    df["px_out"] = np.where(df["side"] == "up", df["down_ask"], df["up_ask"])
    w = df["winner"].astype(str).str.strip().str.lower()
    df["win"] = ((df["side"] == "up") & w.str.startswith("up")) | \
                ((df["side"] == "down") & w.str.startswith("down"))
    df["abs_delta"] = df["delta_pct"].abs()
    return df[(df["px_in"] > 0) & (df["px_in"] < 1) &
              (df["px_out"] > 0) & (df["px_out"] < 1)]


def ev_series(r: pd.DataFrame, px_col: str, win_mask) -> np.ndarray:
    p = r[px_col].to_numpy(dtype=float)
    rate = r["fee_rate_bps"].to_numpy(dtype=float) / 1e4
    f = rate * np.minimum(1.0, (1.0 - p) / np.maximum(p, 1e-9))
    return np.where(win_mask, (1.0 - p) / p, -1.0) - f


def stats(ev: np.ndarray, days: np.ndarray) -> dict:
    s = {"n": len(ev), "ev": float(ev.mean())}
    sd = ev.std(ddof=1) if len(ev) > 1 else 0.0
    s["t"] = float(ev.mean() / (sd / np.sqrt(len(ev)))) if sd > 0 else np.nan
    d = pd.DataFrame({"ev": ev, "day": days})
    g = d.groupby("day")["ev"].mean()
    s["ndays"] = len(g)
    s["t_day"] = (float(g.mean() / (g.std(ddof=1) / np.sqrt(len(g))))
                  if len(g) > 1 and g.std(ddof=1) > 0 else np.nan)
    h = len(ev) // 2
    s["h1"], s["h2"] = float(ev[:h].mean()), float(ev[h:].mean())
    s["drop1"] = float(d[d["day"] != g.idxmax()]["ev"].mean())
    s["survives"] = (abs(s["ev"]) > 0.02 and s["t"] > 1.5
                     and (np.isnan(s["t_day"]) or s["t_day"] > 1.5)
                     and s["h1"] > 0 and s["h2"] > 0 and s["drop1"] > 0)
    return s


def show(tag: str, s: dict) -> None:
    print(f"{tag:34s} n={s['n']:5d}  EV/$1 {s['ev']:+.4f}  t {s['t']:+.2f}  "
          f"day-t {s['t_day']:+.2f}  halves {s['h1']:+.3f}/{s['h2']:+.3f}  "
          f"drop1 {s['drop1']:+.3f}  days {s['ndays']:2d}"
          + ("   <-- SURVIVES" if s["survives"] else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--min-n", type=int, default=30)
    args = ap.parse_args()

    for lo, hi in TAU_BUCKETS:
        df = fetch(args.db, lo, hi)
        if df.empty:
            print(f"=== tau {lo}-{hi}s: no rounds with both asks ===\n")
            continue
        days = (df["end_ms"] // 86_400_000).to_numpy()
        print(f"=== tau {lo}-{hi}s  ({len(df)} rounds with both real asks) ===")
        show("ALL  buy-in-money", stats(ev_series(df, "px_in", df["win"].to_numpy()), days))
        show("ALL  FADE (buy cheap side)",
             stats(ev_series(df, "px_out", ~df["win"].to_numpy()), days))
        for dlo, dhi in DELTA_BUCKETS:
            cell = df[(df["abs_delta"] >= dlo) & (df["abs_delta"] < dhi)]
            if len(cell) < args.min_n:
                continue
            cd = (cell["end_ms"] // 86_400_000).to_numpy()
            s_in = stats(ev_series(cell, "px_in", cell["win"].to_numpy()), cd)
            s_out = stats(ev_series(cell, "px_out", ~cell["win"].to_numpy()), cd)
            print(f"  |delta| {dlo:>5.3f}-{dhi:<5.3f}%  in-money px {cell['px_in'].mean():.3f} "
                  f"WR {cell['win'].mean():5.1%}  px {cell['px_out'].mean():.3f} "
                  f"(fade)  ->  fade", end=" ")
            show("", s_out)
        print()


if __name__ == "__main__":
    main()
