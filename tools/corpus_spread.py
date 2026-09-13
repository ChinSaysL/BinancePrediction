"""The real transaction cost, measured instead of assumed.

Every earlier EV number needed a cost guess (I used 2 cents) and the result swung
on it. The price file lets us measure it. When both tokens trade inside the same
second, the Up print is the price a Up BUYER paid (an ask-side print) and the Down
print is the price a Down buyer paid. Since asking for Down at q is the same as
bidding for Up at 1-q, the Up spread is

    spread_up = up_px - (1 - dn_px)

which is exactly "what it costs to cross, in Up-token terms". Grouped by tau and by
price level, that is the number any taker EV has to clear.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", default="data/btc5m_prices.parquet")
    args = ap.parse_args()

    p = pd.read_parquet(args.prices)
    both = p[p["up_px"].notna() & p["dn_px"].notna()].copy()
    both["spread"] = both["up_px"] + both["dn_px"] - 1.0
    both["up_mid"] = (both["up_px"] + (1 - both["dn_px"])) / 2.0
    s = both["spread"]
    print(f"seconds with BOTH tokens printed: {len(both):,} of {len(p):,} "
          f"({len(both) / len(p):.1%})   windows {both['window_id'].nunique():,}")
    if both.empty:
        return
    print(f"\nspread (Up-token, = cost of crossing one round trip proxy):")
    print(f"  mean {s.mean():.4f}  median {s.median():.4f}  sd {s.std():.4f}")
    print(f"  p10 {s.quantile(.10):.4f}  p25 {s.quantile(.25):.4f}  "
          f"p75 {s.quantile(.75):.4f}  p90 {s.quantile(.90):.4f}")
    print(f"  share >= 0.01: {(s >= 0.01).mean():.1%}   "
          f">= 0.02: {(s >= 0.02).mean():.1%}   "
          f"<= 0: {(s <= 0).mean():.1%}")

    print(f"\nby tau bucket:")
    for lo, hi in ((0, 15), (15, 60), (60, 150), (150, 300)):
        c = both[(both["tau_s"] > lo) & (both["tau_s"] <= hi)]
        if len(c) < 50:
            continue
        print(f"  tau {lo:3d}-{hi:3d}  n={len(c):7,d}  close-to-mid mean "
              f"{c['spread'].mean():+.4f}  median {c['spread'].median():+.4f}  "
              f"(half-spread {c['spread'].median() / 2:.4f})")

    print(f"\nby price level (where the money is):")
    for lo, hi in ((0.0, 0.05), (0.05, 0.2), (0.2, 0.4), (0.4, 0.6),
                   (0.6, 0.8), (0.8, 0.95), (0.95, 1.01)):
        c = both[(both["up_mid"] >= lo) & (both["up_mid"] < hi)]
        if len(c) < 100:
            continue
        print(f"  Up {lo:.2f}-{hi:.2f}  n={len(c):7,d}  half-spread "
              f"{c['spread'].median() / 2:.4f}  "
              f"({c['spread'].median() / 2 / max(c['up_mid'].median(), 1e-9):.2%} "
              f"of price)")

    print(f"\nper-second trade counts are small, so this spread is estimated from "
          f"prints that may be seconds apart -- treat it as the cost floor, not "
          f"the exact book.")


if __name__ == "__main__":
    main()
