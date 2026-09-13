"""WHO KNOWS BETTER: the venue's price, or our own spot?

The ticket buys the side our Binance spot implies is cheap while the venue still
quotes it at 2-5c. On the user's screenshot that looked like a mispricing: with
14s left the chart sat ON the strike (77349.44) yet the venue priced Up 0.99 /
Down 0.03. But this venue SETTLES ON CHAINLINK, not on the chart's Binance price,
so the venue's own reference can be several bps away while our spot sits on the
strike -- and the settlement follows the venue's reference.

So the question that decides every one of these trades is a calibration question,
answerable on 5 days of our own recording:
    at a fixed tau, does the venue's implied probability predict the outcome
    better than our spot-implied probability does?
If the venue wins, then "our spot disagrees with the venue" is not an edge -- it
is our own error, and any arm that trades against the venue (the ticket) is
betting on that error.

Also reports the exact contingency from the screenshot: our spot within 1bp of
the strike while the venue prices that side at <=5c -- what actually happened.

Usage:  python -m tools.venue_vs_spot [--db data/btcpredict.db] [--tau 10]
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path

from btcpredict.pricing.digital import norm_cdf


def sigma_by_round(con, rounds):
    bars = con.execute("SELECT open_ms, close FROM bars WHERE symbol='BTCUSDT' "
                       "ORDER BY open_ms").fetchall()
    out = {}
    for slug, start_ms in rounds:
        past = [b for b in bars if b[0] <= start_ms][-61:]
        rets = [math.log(past[i][1] / past[i - 1][1])
                for i in range(1, len(past)) if past[i - 1][1] > 0]
        if len(rets) >= 10:
            out[slug] = st.pstdev(rets)
    return out


def score(preds, label):
    """preds: list of (p, realised_bool)."""
    if not preds:
        print(f"  {label:<22} no rows"); return
    n = len(preds)
    brier = sum((p - (1.0 if y else 0.0)) ** 2 for p, y in preds) / n
    ll = -sum(math.log(max(1e-9, p if y else 1 - p)) for p, y in preds) / n
    acc = sum(1 for p, y in preds if (p >= 0.5) == y) / n
    print(f"  {label:<22} n={n:<5} brier {brier:.4f}  logloss {ll:.4f}  "
          f"acc {100*acc:5.1f}%")
    return brier, ll


def calibration(preds, label):
    buckets = [(0.0, 0.05), (0.05, 0.20), (0.20, 0.40), (0.40, 0.60),
               (0.60, 0.80), (0.80, 0.95), (0.95, 1.01)]
    print(f"  {label}")
    for lo, hi in buckets:
        sub = [(p, y) for p, y in preds if lo <= p < hi]
        if not sub:
            continue
        mp = sum(p for p, _ in sub) / len(sub)
        mr = sum(1 for _, y in sub if y) / len(sub)
        print(f"    says {lo:.2f}-{hi:.2f}: n={len(sub):<4} predicted "
              f"{mp:.3f}  realised {mr:.3f}  ({'ok' if abs(mp-mr) < 0.07 else 'off'})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--tau", type=float, default=10.0)
    args = ap.parse_args()
    con = sqlite3.connect(Path(args.db))
    rounds = con.execute(
        "SELECT slug, start_ms, strike, winner FROM rounds "
        "WHERE symbol='BTCUSDT' AND winner IN ('Up','Down') AND strike > 0"
    ).fetchall()
    sig = sigma_by_round(con, [(r[0], r[1]) for r in rounds])
    fb = st.median(sig.values()) if sig else 2e-4
    print(f"rounds {len(rounds):,}   sigma from {len(sig):,} measured "
          f"(median {fb:.2e}), else that median   tau={args.tau:.0f}s\n")

    venue, spot, cheap_case = [], [], []
    for slug, start_ms, strike, winner in rounds:
        s = sig.get(slug, fb)
        row = con.execute(
            "SELECT tau_s, spot, up_price, up_bid, up_ask, down_ask FROM quotes "
            "WHERE slug=? AND spot IS NOT NULL AND tau_s IS NOT NULL "
            "ORDER BY ABS(tau_s - ?) LIMIT 1", (slug, args.tau)).fetchone()
        if not row or not row[3]:
            continue
        tau, sp, up_px, up_bid, up_ask, down_ask = row
        if not (0.0 < up_px < 1.0) or not sp or not s:
            continue
        y = winner == "Up"
        venue.append((up_px, y))
        z = math.log(sp / strike) / (s * math.sqrt(max(1.0, tau) / 60.0))
        spot.append((norm_cdf(z), y))
        # the screenshot's contingency: our spot ON the strike (<=1bp) while the
        # venue still prices the cheap side at <=5c
        dist = abs(sp - strike) / strike * 1e4
        cheap = min(up_ask or 1, down_ask or 1)
        if dist <= 1.0 and cheap <= 0.05:
            cheap_won = (not y) if (down_ask or 1) < (up_ask or 1) else y
            cheap_case.append((cheap, cheap_won))

    print("=== which probability predicts the settlement? (lower is better) ===")
    score(venue, "the venue's price")
    score(spot, "our spot (digital)")
    print()
    print("=== calibration: does the venue's number mean what it says? ===")
    calibration(venue, "the venue's price")
    print()
    calibration(spot, "our spot (digital)")
    print()
    print("=== the screenshot's setup: our spot within 1bp of the strike while")
    print("    the venue prices that side at <=5c ===")
    if cheap_case:
        n = len(cheap_case)
        wins = sum(1 for _, w in cheap_case if w)
        px = sum(p for p, _ in cheap_case) / n
        # what the venue's price implies, and the binomial spread on that
        exp = px
        print(f"  n={n}  the cheap side won {wins}  ({100*wins/n:.1f}%)   "
              f"mean venue price {px:.4f} -> implied {100*exp:.1f}%")
        print(f"  break-even rate for a 50x ticket is ~2.0%, and the venue's own "
              f"price implies {100*exp:.1f}%")
        if wins:
            print(f"  a {wins}/{n} result against an implied {100*exp:.1f}% is "
                  f"what calibration looks like, not a mispricing")
        else:
            print(f"  0/{n} wins is consistent with a true rate of "
                  f"{100*exp:.1f}% (P(0 wins) = {(1-exp)**n:.2f})")
    else:
        print("  no instances in this sample")


if __name__ == "__main__":
    main()
