"""What does OUR venue actually charge for a given window delta?

This exists to kill an assumption. tools/gist_snipe.py scored the gist strategy
against an invented price curve (`0.5 + 0.5 * min(1, |delta|/0.10)`), and the
whole positive result hung on it. Here the price is MEASURED: for each round we
take the latest recorded quote inside a tau bucket, price the side that window
delta points at, and buy at the ask when the book was recorded.

EV is per $1 staked, net of the venue's own fee_rate_bps:
    win  -> (1-p)/p          lose -> -1        fee -> rate * min(1, (1-p)/p)

Every cell is reported with the repo's battery: pooled-t, day-clustered t, both
halves, and drop-the-best-day. One entry per round per cell (no pseudo-replication).
"""
from __future__ import annotations

import argparse
import sqlite3

import numpy as np
import pandas as pd

TAU_BUCKETS = [(3, 8), (8, 20), (20, 60), (60, 120)]
DELTA_BUCKETS = [(0.0, 0.005), (0.005, 0.02), (0.02, 0.05), (0.05, 0.10),
                 (0.10, 99.0)]


def load(db: str) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rounds = pd.read_sql_query(
        "select slug, symbol, start_ms, end_ms, strike, end_price, winner, "
        "fee_rate_bps from rounds where strike is not null and end_price is not null",
        con)
    q = pd.read_sql_query(
        "select slug, ts_ms, tau_s, up_price, down_price, up_ask, down_ask, "
        "spot, strike from quotes where tau_s <= 125 and spot is not null",
        con)
    con.close()
    df = q.merge(rounds, on="slug", suffixes=("", "_r"))
    df["delta_pct"] = (df["spot"] - df["strike"]) / df["strike"] * 100.0
    df["side"] = np.where(df["delta_pct"] > 0, "up", "down")
    ask = np.where(df["side"] == "up", df["up_ask"], df["down_ask"])
    disp = np.where(df["side"] == "up", df["up_price"], df["down_price"])
    df["px"] = np.where(np.isfinite(pd.to_numeric(ask, errors="coerce")) &
                        (pd.to_numeric(ask, errors="coerce") > 0), ask, disp)
    df["px_src"] = np.where(df["px"] == pd.to_numeric(ask, errors="coerce"),
                            "ask", "displayed")
    w = df["winner"].astype(str).str.strip().str.lower()
    df["win"] = ((df["side"] == "up") & w.str.startswith("up")) | \
                ((df["side"] == "down") & w.str.startswith("down"))
    df["abs_delta"] = df["delta_pct"].abs()
    return df.dropna(subset=["px"])


def latest_per_round(df: pd.DataFrame, lo: float, hi: float) -> pd.DataFrame:
    """One row per round: its last quote inside the tau window."""
    sub = df[(df["tau_s"] >= lo) & (df["tau_s"] < hi)]
    if sub.empty:
        return sub
    return sub.sort_values("ts_ms").groupby("slug").tail(1)


def battery(r: pd.DataFrame) -> dict:
    p = r["px"].to_numpy(dtype=float)
    rate = r["fee_rate_bps"].to_numpy(dtype=float) / 1e4
    f = rate * np.minimum(1.0, (1.0 - p) / np.maximum(p, 1e-9))
    ev = np.where(r["win"].to_numpy(), (1.0 - p) / p, -1.0) - f
    out = {"n": len(r), "wr": float(r["win"].mean()), "px": float(p.mean()),
           "ev": float(ev.mean()), "ask_share": float((r["px_src"] == "ask").mean())}
    sd = ev.std(ddof=1)
    out["t_pool"] = float(ev.mean() / (sd / np.sqrt(len(ev)))) if sd > 0 else np.nan
    day = pd.to_datetime(r["end_ms"], unit="ms", utc=True).dt.date
    g = pd.DataFrame({"ev": ev, "day": day}).groupby("day")["ev"].mean()
    out["ndays"] = len(g)
    out["t_day"] = (float(g.mean() / (g.std(ddof=1) / np.sqrt(len(g))))
                    if len(g) > 1 and g.std(ddof=1) > 0 else np.nan)
    h = len(ev) // 2
    out["h1"], out["h2"] = float(ev[:h].mean()), float(ev[h:].mean())
    keep = pd.DataFrame({"ev": ev, "day": day})
    out["drop1"] = float(keep[keep["day"] != g.idxmax()]["ev"].mean())
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--min-n", type=int, default=30)
    args = ap.parse_args()

    df = load(args.db)
    print(f"{len(df):,} quotes with spot, {df['slug'].nunique()} rounds, "
          f"{df['symbol'].nunique()} symbols, "
          f"{pd.to_datetime(df['end_ms'], unit='ms', utc=True).dt.date.nunique()} days")
    print(f"book available (ask) in {float((df['px_src'] == 'ask').mean()):.1%} of quotes")
    print(f"winner values seen: {dict(df['winner'].astype(str).value_counts().head(6))}\n")

    for lo, hi in TAU_BUCKETS:
        sub = latest_per_round(df, lo, hi)
        if sub.empty:
            print(f"tau {lo}-{hi}s: no data")
            continue
        b = battery(sub)
        print(f"tau {lo:>3.0f}-{hi:<3.0f}s  ALL   n={b['n']:5,d}  WR {b['wr']:6.1%}  "
              f"px {b['px']:.3f}  EV/$1 {b['ev']:+.4f}  t {b['t_pool']:+.2f}  "
              f"day-t {b['t_day']:+.2f}  halves {b['h1']:+.3f}/{b['h2']:+.3f}  "
              f"drop1 {b['drop1']:+.3f}  days {b['ndays']}  ask% {b['ask_share']:.0%}")
        for dlo, dhi in DELTA_BUCKETS:
            cell = sub[(sub["abs_delta"] >= dlo) & (sub["abs_delta"] < dhi)]
            if len(cell) < args.min_n:
                continue
            c = battery(cell)
            flag = ""
            if (abs(c["ev"]) > 0.02 and c["t_pool"] > 1.5 and
                    (np.isnan(c["t_day"]) or c["t_day"] > 1.5) and
                    c["h1"] > 0 and c["h2"] > 0 and c["drop1"] > 0):
                flag = "  <-- SURVIVES"
            print(f"   |delta| {dlo:>5.3f}-{dhi:<5.2f}%  n={c['n']:5,d}  "
                  f"WR {c['wr']:6.1%}  px {c['px']:.3f}  EV/$1 {c['ev']:+.4f}  "
                  f"t {c['t_pool']:+.2f}  day-t {c['t_day']:+.2f}  "
                  f"halves {c['h1']:+.3f}/{c['h2']:+.3f}  drop1 {c['drop1']:+.3f}{flag}")
        print()


if __name__ == "__main__":
    main()
