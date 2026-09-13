#!/usr/bin/env python3
"""Robustness for the out-of-venue mid-band test.

1. one entry per round (first accepted cell) - kills the within-round cluster
2. the same trades under OUR venue's cost structure (180bps fee curve + the
   measured 1.32c/$1 slip): does the mid band die then?
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

WEEK = "/root/pmweek"
SLIP = 0.0132  # measured live, per $1 staked


def fee_our(q: float) -> float:
    if q <= 0:
        return 0.018
    return min(0.018, 0.018 * (1.0 - q) / q)


def ret_ourcost(won: bool, q: float) -> float:
    gross = (1.0 / (q * (1.0 + fee_our(q))) - 1.0) if won else -1.0
    return gross - SLIP


def walk(t: pd.DataFrame, ret: str, label: str) -> None:
    days = sorted(t["day"].unique())
    rules = {"keep everything": lambda x, lo, hi: np.ones(len(x), bool),
             "drop mid third": lambda x, lo, hi: ~((x >= lo) & (x < hi)),
             "edge >= 0.04": lambda x, lo, hi: x >= 0.04}
    totals = {k: 0.0 for k in rules}
    per = {k: [] for k in rules}
    for i in range(len(days) - 1):
        fit = t[t["day"] == days[i]]
        out = t[t["day"] == days[i + 1]]
        e = np.sort(fit["edge"].to_numpy())
        k = len(e) // 3
        lo, hi = e[k], e[2 * k]
        for name, rule in rules.items():
            o = out[rule(out["edge"].to_numpy(), lo, hi)]
            if len(o) == 0:
                continue
            totals[name] += o[ret].sum()
            per[name].append(o[ret].mean())
    print(f"\ntotals [{label}] over {len(days)-1} folds (same folds, comparable):")
    for name, tot in sorted(totals.items(), key=lambda kv: -kv[1]):
        means = " ".join(f"{m:+.3f}" for m in per[name])
        print(f"  {name:<16} total {tot:+9.2f}   per-fold EV/$1 {means}")


def day_buckets(t: pd.DataFrame, ret: str) -> pd.DataFrame:
    rows = []
    for day, g in t.groupby("day"):
        b = g.sort_values("edge")
        k = len(b) // 3
        if k < 5:
            continue
        for tag, part in (("small", b.iloc[:k]), ("mid", b.iloc[k:2 * k]),
                          ("big", b.iloc[2 * k:])):
            rows.append(dict(day=str(day), bucket=tag, n=len(part), ev=part[ret].mean()))
    return pd.DataFrame(rows)


def main() -> None:
    rows = pd.read_parquet(f"{WEEK}/pm_band_rows.parquet")
    acc = rows[rows["act"]].copy()
    print(f"all accepted cells: {len(acc)}  rounds: {acc['market_id'].nunique()}")

    # --- one entry per round: first accepted cell per market
    first = (acc.sort_values("ts_ms")
             .groupby("market_id", as_index=False).first())
    print(f"one per round:      {len(first)}")

    for name, t in (("ALL CELLS", acc), ("ONE PER ROUND", first)):
        print(f"\n================ {name} ================")
        bt = day_buckets(t, "ret_net")
        piv = bt.pivot(index="day", columns="bucket", values="ev")
        npiv = bt.pivot(index="day", columns="bucket", values="n")
        with pd.option_context("display.width", 200):
            print("EV/$1 by claim tercile, per day (Polymarket fees):")
            print(piv.to_string())
            print("n:")
            print(npiv.to_string())
        walk(t, "ret_net", f"{name} - Polymarket costs")

        under = t.copy()
        under["ret_our"] = [ret_ourcost(w, q)
                            for w, q in zip(under["won"], under["q"])]
        bt2 = day_buckets(under, "ret_our")
        piv2 = bt2.pivot(index="day", columns="bucket", values="ev")
        with pd.option_context("display.width", 200):
            print("\nEV/$1 by claim tercile under OUR venue's costs "
                  "(180bps fee + 1.32c slip):")
            print(piv2.to_string())
        walk(under, "ret_our", f"{name} - our venue's costs")


if __name__ == "__main__":
    main()
