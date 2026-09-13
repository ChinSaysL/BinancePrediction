"""Would RESTING orders have filled, and how much would that have saved?

Why this is the money question: 138 of our 453 live fills paid ZERO fee while the
taker formula says they owed 1.7-2.2% of stake -- the venue exempts makers. Every
fill we cross for pays that fee, and on the cheap side it is a flat 2% of stake,
which is the same order as the whole measured edge we found today.

This replays our recorded book:
  * signal = window delta (spot vs strike), the gist's dominant term
  * taker path  : pay the recorded ask, pay the fee        (what we do now)
  * maker path  : post a bid at ask - k ticks, wait for the window to close,
                  filled only if a later recorded best-ask came down to our price
  * same settlement, same round, so the comparison is apples to apples

Outputs fill rate, EV per $1 staked for both paths, the battery on both, and the
net money difference per 100 trades.
"""
from __future__ import annotations

import argparse
import math
import sqlite3

import numpy as np
import pandas as pd

TICK = 0.01


def load(db: str) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    q = pd.read_sql_query(
        "select slug, ts_ms, tau_s, spot, up_ask, down_ask, up_bid, down_bid "
        "from quotes where spot is not null and up_ask is not null "
        "and down_ask is not null", con)
    r = pd.read_sql_query(
        "select slug, strike, winner, fee_rate_bps, end_ms from rounds "
        "where strike is not null and winner is not null", con)
    con.close()
    return q.merge(r, on="slug", how="inner").sort_values(["slug", "ts_ms"])


def fee(px: float, bps: float) -> float:
    return (bps / 1e4) * min(1.0, (1.0 - px) / px) if px > 0 else 1.0


def stats(ev: np.ndarray, days: np.ndarray) -> dict:
    s = {"n": len(ev), "ev": float(ev.mean())}
    sd = ev.std(ddof=1) if len(ev) > 1 else 0.0
    s["t"] = float(ev.mean() / (sd / math.sqrt(len(ev)))) if sd > 0 else float("nan")
    d = pd.DataFrame({"ev": ev, "day": days})
    g = d.groupby("day")["ev"].mean()
    s["t_day"] = (float(g.mean() / (g.std(ddof=1) / math.sqrt(len(g))))
                  if len(g) > 1 and g.std(ddof=1) > 0 else float("nan"))
    h = len(ev) // 2
    s["h1"], s["h2"] = float(ev[:h].mean()), float(ev[h:].mean())
    s["drop1"] = float(d[d["day"] != g.idxmax()]["ev"].mean())
    return s


def line(tag: str, s: dict) -> str:
    return (f"{tag:34s} n={s['n']:5d}  EV/$1 {s['ev']:+.4f}  t {s['t']:+.2f}  "
            f"day-t {s['t_day']:+.2f}  halves {s['h1']:+.3f}/{s['h2']:+.3f}  "
            f"drop1 {s['drop1']:+.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--tau", type=float, default=60.0)
    ap.add_argument("--ticks", type=int, nargs="+", default=[1, 2, 3],
                    help="how many ticks inside the ask to rest")
    ap.add_argument("--delta-lo", type=float, default=0.05)
    ap.add_argument("--delta-hi", type=float, default=0.10)
    ap.add_argument("--min-quotes", type=int, default=5)
    args = ap.parse_args()

    df = load(args.db)
    rows = []
    for slug, g in df.groupby("slug"):
        at = g[g["tau_s"] >= args.tau]
        if at.empty:
            continue
        e = at.iloc[-1]
        later = g[g["ts_ms"] > e["ts_ms"]]
        if len(later) < args.min_quotes:
            continue
        strike = float(e["strike"])
        delta = (float(e["spot"]) - strike) / strike * 100.0
        if not (args.delta_lo <= abs(delta) < args.delta_hi):
            continue
        up = delta > 0
        ask = float(e["up_ask"] if up else e["down_ask"])
        if not 0 < ask < 1:
            continue
        w = str(e["winner"]).strip().lower()
        win = (up and w.startswith("up")) or ((not up) and w.startswith("down"))
        rate = float(e["fee_rate_bps"])
        # taker: pay the ask now
        ev_taker = ((1 - ask) / ask if win else -1.0) - fee(ask, rate)
        rec = {"slug": slug, "day": int(e["end_ms"] // 86_400_000),
               "win": win, "ask": ask, "ev_taker": ev_taker}
        # maker: rest inside the ask; fill if a later ask reaches our price
        post = later[later["tau_s"] >= 0]
        for k in args.ticks:
            bid = round(ask - k * TICK, 4)
            if bid <= 0.01:
                continue
            askpath = (post["up_ask"] if up else post["down_ask"]).to_numpy(float)
            filled = bool(len(askpath) and np.nanmin(askpath) <= bid + 1e-9)
            rec[f"filled{k}"] = filled
            rec[f"ev_maker{k}"] = (((1 - bid) / bid if win else -1.0)
                                   if filled else 0.0)  # 0 = no trade, no cost
        rows.append(rec)

    r = pd.DataFrame(rows)
    if r.empty:
        print("no rounds qualified")
        return
    print(f"rounds: {len(r)} with >={args.min_quotes} later quotes, "
          f"|delta| in [{args.delta_lo},{args.delta_hi})% at tau>={args.tau:.0f}s "
          f"(real tau avg)")
    print(f"win rate {r['win'].mean():.1%}  avg ask {r['ask'].mean():.4f}\n")
    print(line("TAKER (cross, pay fee)", stats(r["ev_taker"].to_numpy(), r["day"].to_numpy())))
    for k in args.ticks:
        if f"ev_maker{k}" not in r:
            continue
        fr = float(r[f"filled{k}"].mean())
        ev = r[f"ev_maker{k}"].to_numpy()
        s = stats(ev, r["day"].to_numpy())
        s["n"] = len(ev)  # includes unfilled (0 EV) so n is comparable
        print(line(f"MAKER rest ask-{k}t (fill {fr:.0%})", s))
        filled_only = r[r[f"filled{k}"]][f"ev_maker{k}"].to_numpy()
        if len(filled_only) > 1:
            print(f"{'':34s}   filled-only EV/$1 {filled_only.mean():+.4f} "
                  f"on n={len(filled_only)}")
    print("\nper 100 signals, money on the table (stake $1):")
    base = r["ev_taker"].sum() / len(r) * 100
    print(f"  crossing            : {base:+.2f}")
    for k in args.ticks:
        if f"ev_maker{k}" not in r:
            continue
        v = r[f"ev_maker{k}"].sum() / len(r) * 100
        print(f"  resting ask-{k}t      : {v:+.2f}   ({v - base:+.2f} vs crossing)")


if __name__ == "__main__":
    main()
