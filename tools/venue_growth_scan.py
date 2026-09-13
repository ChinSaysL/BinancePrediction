"""Where can we multiply the measured edge? Coverage scan across the venue.

The measured cell (+2.18% per $1 staked, tau~60s, |window delta| 0.05-0.10%) came
from 5-minute windows. Growing means more symbols and longer timeframes -- but
0.10% means something different over 5 minutes than over 1 hour, so the useful
delta band has to be re-measured per (symbol, timeframe), not assumed.

Also prints the money projection per group: signals/day x edge x stake, which is
what decides whether growing is worth anything.

Multiple comparisons warning: scanning bands per group will find flattering cells
by chance. Cells are shown WITH the battery and with the sample size, and nothing
here justifies a live change on its own.
"""
from __future__ import annotations

import argparse
import math
import sqlite3

import numpy as np
import pandas as pd

BANDS = [(0.005, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.20),
         (0.20, 0.50), (0.50, 99.0)]


def load(db: str):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rounds = pd.read_sql_query(
        "select slug, symbol, start_ms, end_ms, strike, winner, fee_rate_bps "
        "from rounds where strike is not null and winner is not null", con)
    q = pd.read_sql_query(
        "select slug, ts_ms, tau_s, spot, up_ask, down_ask from quotes "
        "where spot is not null and up_ask is not null and down_ask is not null",
        con)
    con.close()
    rounds["win_s"] = ((rounds["end_ms"] - rounds["start_ms"]) / 1000.0).round(0)
    return q.merge(rounds, on="slug", how="inner")


def battery(ev: np.ndarray, days: np.ndarray) -> dict:
    s = {"n": len(ev), "ev": float(ev.mean())}
    sd = ev.std(ddof=1) if len(ev) > 1 else 0.0
    s["t"] = float(ev.mean() / (sd / math.sqrt(len(ev)))) if sd > 0 else float("nan")
    d = pd.DataFrame({"ev": ev, "day": days})
    g = d.groupby("day")["ev"].mean()
    s["ndays"] = len(g)
    s["t_day"] = (float(g.mean() / (g.std(ddof=1) / math.sqrt(len(g))))
                  if len(g) > 1 and g.std(ddof=1) > 0 else float("nan"))
    h = len(ev) // 2
    s["h1"], s["h2"] = float(ev[:h].mean()), float(ev[h:].mean())
    s["drop1"] = float(d[d["day"] != g.idxmax()]["ev"].mean())
    s["ok"] = (s["ev"] > 0.01 and s["t"] > 1.5 and s["t_day"] > 1.5
               and s["h1"] > 0 and s["h2"] > 0 and s["drop1"] > 0)
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--tau", type=float, default=60.0)
    ap.add_argument("--stakes", type=float, nargs="+", default=[1.0, 5.0])
    ap.add_argument("--min-n", type=int, default=25)
    args = ap.parse_args()

    df = load(args.db)
    days_total = pd.to_datetime(df["end_ms"], unit="ms", utc=True).dt.date.nunique()
    print(f"tape: {len(df):,} quotes | {df['slug'].nunique()} rounds | {days_total} days\n")

    print("=== COVERAGE (what the venue gives us) ===")
    cov = (df.groupby(["symbol", "win_s"])
             .agg(rounds=("slug", "nunique"), quotes=("slug", "size"),
                  first=("end_ms", "min"), last=("end_ms", "max"))
             .reset_index())
    for _, c in cov.sort_values(["symbol", "win_s"]).iterrows():
        f = pd.to_datetime(c["first"], unit="ms", utc=True).strftime("%m-%d")
        l = pd.to_datetime(c["last"], unit="ms", utc=True).strftime("%m-%d")
        rpd = c["rounds"] / max((c["last"] - c["first"]) / 86_400_000, 1e-9)
        print(f"  {c['symbol']:9s} {int(c['win_s']):4d}s window  {int(c['rounds']):5d} rounds "
              f"({rpd:6.1f}/day)  {f}->{l}  {int(c['quotes']):7,d} quotes")

    print(f"\n=== EDGE by group, entry at tau>={args.tau:.0f}s (real asks, real fee) ===")
    print("  money/day assumes signals keep coming at the recorded rate\n")
    totals = {s: 0.0 for s in args.stakes}
    for (sym, win_s), g in df.groupby(["symbol", "win_s"]):
        rounds_at = []
        for slug, gg in g.groupby("slug"):
            at = gg[gg["tau_s"] >= args.tau]
            if at.empty:
                continue
            e = at.iloc[-1]
            strike = float(e["strike"])
            delta = (float(e["spot"]) - strike) / strike * 100.0
            up = delta > 0
            px = float(e["up_ask"] if up else e["down_ask"])
            if not 0 < px < 1:
                continue
            w = str(e["winner"]).strip().lower()
            win = (up and w.startswith("up")) or ((not up) and w.startswith("down"))
            rate = float(e["fee_rate_bps"]) / 1e4
            f = rate * min(1.0, (1 - px) / px)
            rounds_at.append({"day": int(e["end_ms"] // 86_400_000),
                              "absd": abs(delta), "px": px,
                              "ev": ((1 - px) / px if win else -1.0) - f})
        if len(rounds_at) < args.min_n:
            continue
        rr = pd.DataFrame(rounds_at)
        span = max((rr["day"].max() - rr["day"].min()) + 1, 1)
        print(f"-- {sym} {int(win_s)}s  ({len(rr)} rounds, {len(rr) / span:.1f}/day)")
        s_all = battery(rr["ev"].to_numpy(), rr["day"].to_numpy())
        print(f"     ALL deltas            n={s_all['n']:4d}  EV/$1 {s_all['ev']:+.4f}  "
              f"t {s_all['t']:+.2f}  day-t {s_all['t_day']:+.2f}"
              + ("  <-- ok" if s_all["ok"] else ""))
        for lo, hi in BANDS:
            c = rr[(rr["absd"] >= lo) & (rr["absd"] < hi)]
            if len(c) < args.min_n:
                continue
            s = battery(c["ev"].to_numpy(), c["day"].to_numpy())
            money = " ".join(
                f"${s['ev'] * st * len(c) / span:5.2f}/d@${st:.0f}" for st in args.stakes)
            print(f"     |delta| {lo:>5.2f}-{hi:<5.2f}%  n={s['n']:4d}  "
                  f"EV/$1 {s['ev']:+.4f}  t {s['t']:+.2f}  day-t {s['t_day']:+.2f}  "
                  f"halves {s['h1']:+.3f}/{s['h2']:+.3f}  drop1 {s['drop1']:+.3f}  "
                  f"{money}" + ("  <-- ok" if s["ok"] else ""))
            if s["ok"]:
                for st in args.stakes:
                    totals[st] += s["ev"] * st * len(c) / span
        print()

    print("=== if every 'ok' cell were traded as measured ===")
    for st in args.stakes:
        print(f"  stake ${st:.0f}: ~${totals[st]:.2f}/day  (~${totals[st] * 30:.0f}/month)")


if __name__ == "__main__":
    main()
