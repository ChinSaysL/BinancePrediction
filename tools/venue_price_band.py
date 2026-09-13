"""Which PRICE band actually pays, measured on our venue's own recorded asks?

Motivation: our real fills showed the 0.45-0.55 band with +0.19 per $1 staked,
daily-t +3.00 and drop1 +0.50 (n=60). Delta-based chasing (the gist, and the
fade) did not survive. Price is the simpler conditioning variable and it is what
we actually pay, so test it directly on every quote we have -- with real asks
required, so a stale displayed price cannot manufacture an edge.

Per round each tau bucket contributes BOTH sides: the cheap one fills the lower
bands, the expensive one the upper bands. Disjoint by construction, so no
pseudo-replication from using the same round twice in the same band.

EV per $1 staked: win -> (1-p)/p, lose -> -1, fee -> rate*min(1,(1-p)/p)
"""
from __future__ import annotations

import argparse
import sqlite3

import numpy as np
import pandas as pd

TAU_BUCKETS = [(3, 8), (8, 20), (20, 60), (60, 120)]
PRICE_BANDS = [(0.02, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.45),
               (0.45, 0.55), (0.55, 0.70), (0.70, 0.85), (0.85, 0.95),
               (0.95, 1.00)]

SQL = """
with x as (
  select q.slug, q.ts_ms, q.tau_s, q.up_ask, q.down_ask, r.winner,
         r.fee_rate_bps, r.end_ms,
         row_number() over (partition by q.slug order by q.ts_ms desc) as rn
  from quotes q join rounds r on r.slug = q.slug
  where q.up_ask is not null and q.down_ask is not null
    and q.tau_s >= ? and q.tau_s < ?
)
select slug, ts_ms, tau_s, up_ask, down_ask, winner, fee_rate_bps, end_ms
from x where rn = 1
"""


def fetch(db: str, lo: float, hi: float) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        df = pd.read_sql_query(SQL, con, params=(lo, hi))
    finally:
        con.close()
    if df.empty:
        return df
    w = df["winner"].astype(str).str.strip().str.lower()
    up_wins = w.str.startswith("up")
    # each row -> two observations: the cheap side and the expensive side
    cheap_up = df["up_ask"] <= df["down_ask"]
    obs = pd.DataFrame({
        "px": np.where(cheap_up, df["up_ask"], df["down_ask"]),
        "win": np.where(cheap_up, up_wins, ~up_wins),
        "side": np.where(cheap_up, "cheap", "expensive"),
        "fee_rate_bps": df["fee_rate_bps"],
        "end_ms": df["end_ms"],
    })
    obs = obs[(obs["px"] > 0) & (obs["px"] < 1)]
    return obs


def stats(r: pd.DataFrame) -> dict:
    p = r["px"].to_numpy(dtype=float)
    rate = r["fee_rate_bps"].to_numpy(dtype=float) / 1e4
    f = rate * np.minimum(1.0, (1.0 - p) / np.maximum(p, 1e-9))
    ev = np.where(r["win"].to_numpy(), (1.0 - p) / p, -1.0) - f
    s = {"n": len(r), "wr": float(r["win"].mean()), "px": float(p.mean()),
         "ev": float(ev.mean())}
    sd = ev.std(ddof=1) if len(ev) > 1 else 0.0
    s["t"] = float(ev.mean() / (sd / np.sqrt(len(ev)))) if sd > 0 else np.nan
    d = pd.DataFrame({"ev": ev, "day": (r["end_ms"] // 86_400_000).to_numpy()})
    g = d.groupby("day")["ev"].mean()
    s["ndays"] = len(g)
    s["t_day"] = (float(g.mean() / (g.std(ddof=1) / np.sqrt(len(g))))
                  if len(g) > 1 and g.std(ddof=1) > 0 else np.nan)
    h = len(ev) // 2
    s["h1"], s["h2"] = float(ev[:h].mean()), float(ev[h:].mean())
    s["drop1"] = float(d[d["day"] != g.idxmax()]["ev"].mean())
    s["survives"] = (s["ev"] > 0.02 and s["t"] > 1.5 and s["t_day"] > 1.5
                     and s["h1"] > 0 and s["h2"] > 0 and s["drop1"] > 0)
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--min-n", type=int, default=40)
    args = ap.parse_args()
    for lo, hi in TAU_BUCKETS:
        obs = fetch(args.db, lo, hi)
        if obs.empty:
            print(f"=== tau {lo}-{hi}s: no rounds with both asks ===\n")
            continue
        print(f"=== tau {lo}-{hi}s  ({obs['end_ms'].nunique()} rounds, "
              f"{len(obs)} band observations) ===")
        for blo, bhi in PRICE_BANDS:
            cell = obs[(obs["px"] >= blo) & (obs["px"] < bhi)]
            if len(cell) < args.min_n:
                continue
            s = stats(cell)
            print(f"  px {blo:.2f}-{bhi:.2f}  {str(cell['side'].iloc[0])[:9]:9s} "
                  f"n={s['n']:4d}  WR {s['wr']:6.1%}  avg px {s['px']:.3f}  "
                  f"EV/$1 {s['ev']:+.4f}  t {s['t']:+.2f}  day-t {s['t_day']:+.2f}  "
                  f"halves {s['h1']:+.3f}/{s['h2']:+.3f}  drop1 {s['drop1']:+.3f}"
                  + ("   <-- SURVIVES" if s["survives"] else ""))
        print()


if __name__ == "__main__":
    main()
