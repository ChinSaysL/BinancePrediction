"""Which ticket filter actually concentrates the edge? Measured on our own venue.

The ticket buys a 2-5c longshot near the strike. "Only fire at very certain
moves" cannot mean "the model says it is likely": at those entries the digital
fair for the cheap side is ~0.0005-0.02, i.e. it prices the venue as GENEROUS,
and the one that won was a 3-sigma move. What can be improved is the FILTER: the
venue's cheap-side price barely responds to how close the strike is, so the
question is which sub-cohort has the highest win rate RELATIVE TO ITS PRICE.

Axes tested here, per quote inside tau 3-30s with a cheap ask in 0.015-0.06:
  px            the price paid
  dist          |S-K| in bps
  z             dist / (sigma_1m * sqrt(tau/60))  -- "how close relative to the
                move still available", the honest form of "certainty"
  display gap   the cheap side's DISPLAYED price minus its ask: the venue's
                display is stale-high when its own book has already repriced
  sigma regime  realised 1m vol before the round

Every row is reported with the two tests that killed earlier "discoveries" here:
both halves of the sample, a DAY-clustered t (not a pooled one), and the result
after dropping the best day. Seven filters on one dataset WILL produce a big cell
by chance -- that is what these columns are for.

Usage:  python -m tools.ticket_filters [--db data/btcpredict.db]
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics as st
import time
from collections import defaultdict
from pathlib import Path

TAU_LO, TAU_HI = 3.0, 30.0
PX_LO, PX_HI = 0.015, 0.06
FEE = 0.98          # payout per share, i.e. 2% of shares charged on a win


def _sigma_by_round(con, rounds):
    bars = con.execute("SELECT open_ms, close FROM bars WHERE symbol='BTCUSDT' "
                       "ORDER BY open_ms").fetchall()
    out = {}
    for slug, start_ms, *_ in rounds:
        past = [b for b in bars if b[0] <= start_ms][-61:]
        rets = [math.log(past[i][1] / past[i - 1][1])
                for i in range(1, len(past)) if past[i - 1][1] > 0]
        if len(rets) >= 10:
            out[slug] = st.pstdev(rets)
    return out


def load(con):
    rounds = con.execute(
        "SELECT slug, start_ms, strike, winner FROM rounds "
        "WHERE symbol='BTCUSDT' AND winner IN ('Up','Down') AND strike > 0"
    ).fetchall()
    sig = _sigma_by_round(con, rounds)
    fallback = st.median(sig.values()) if sig else 2e-4
    obs = []
    for slug, start_ms, strike, winner in rounds:
        s = sig.get(slug, fallback)
        for ts_ms, tau, spot, up_px, dn_px, up_ask, dn_ask in con.execute(
                "SELECT ts_ms, tau_s, spot, up_price, down_price, up_ask, down_ask "
                "FROM quotes WHERE slug=? AND spot IS NOT NULL "
                "AND tau_s BETWEEN ? AND ? ORDER BY ts_ms", (slug, TAU_LO, TAU_HI)):
            if not (up_ask and dn_ask and up_px and dn_px):
                continue
            cheap_up = up_ask <= dn_ask
            ask = min(up_ask, dn_ask)
            disp = up_px if cheap_up else dn_px
            side = "Up" if cheap_up else "Down"
            if not (PX_LO <= ask <= PX_HI):
                continue
            dist = 1e4 * abs(spot - strike) / strike
            z = (dist / (1e4 * s * math.sqrt(tau / 60.0))
                 if s > 0 and tau > 0 else 99.0)
            obs.append({"slug": slug, "day": start_ms // 86400000, "tau": tau,
                        "px": ask, "disp": disp, "dist": dist, "z": z,
                        "sigma": s, "won": side == winner,
                        # SIGNED distance, from the cheap side's point of view:
                        # > 0 means it is currently IN THE MONEY (S is on its
                        # side of the strike). The unsigned version above cannot
                        # tell "far behind and hopeless" from "ahead and still
                        # quoted like a longshot", and those are opposite trades.
                        "ahead_bp": ((spot - strike) / strike * 1e4
                                     if side == "Up"
                                     else (strike - spot) / strike * 1e4)})
    return obs, sig, fallback


def score(obs, keep, label):
    """One ticket per round: the FIRST quote that passes the filter."""
    seen, first = set(), []
    for o in sorted(obs, key=lambda x: -x["tau"]):
        if o["slug"] in seen or not keep(o):
            continue
        seen.add(o["slug"])
        first.append(o)
    if len(first) < 10:
        print(f"  {label:<26} n={len(first):<4} (too few)"); return
    n = len(first)
    vals = [(FEE / o["px"] - 1.0) if o["won"] else -1.0 for o in first]
    m = sum(vals) / n
    sd = (sum((v - m) ** 2 for v in vals) / n) ** 0.5
    t = m / (sd / n ** 0.5) if sd else 0.0
    wr = sum(1 for o in first if o["won"]) / n
    px = sum(o["px"] for o in first) / n
    half = n // 2
    h1 = sum(vals[:half]) / max(1, half)
    h2 = sum(vals[half:]) / max(1, n - half)
    byday = defaultdict(list)
    for o, v in zip(first, vals):
        byday[o["day"]].append(v)
    dm = [sum(v) / len(v) for v in byday.values()]
    mdd = sum(dm) / len(dm)
    sdd = (sum((x - mdd) ** 2 for x in dm) / len(dm)) ** 0.5
    td = mdd / (sdd / len(dm) ** 0.5) if sdd else 0.0
    drop1 = sorted(dm)[:-1]
    nrounds = len({o["slug"] for o in obs})
    print(f"  {label:<26} n={n:<4} WR {100*wr:5.2f}% /px {px:.4f} = "
          f"{wr/px:4.2f}x  EV{ m:+7.3f}  pooled-t{t:+5.2f}  daily-t{td:+5.2f}  "
          f"halves {h1:+.2f}/{h2:+.2f}  drop1 {sum(drop1)/len(drop1):+.2f}  "
          f"({1000*n/(nrounds or 1):.1f} per 1000 rounds)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    args = ap.parse_args()
    con = sqlite3.connect(Path(args.db))
    obs, sig, fb = load(con)
    rounds = len({o["slug"] for o in obs})
    print(f"quote-level candidates: {len(obs):,} over {rounds:,} rounds "
          f"(sigma_1m: {len(sig):,} measured, else median {fb:.2e})\n")

    print("=== filters, one ticket per round (first passing quote) ===")
    score(obs, lambda o: 0.02 <= o["px"] <= 0.05 and o["dist"] <= 2.0,
          "live now: px2-5,<2bp")
    score(obs, lambda o: o["px"] <= 0.03 and o["dist"] <= 2.0,
          "px<=3c, dist<=2bp")
    score(obs, lambda o: o["px"] <= 0.025 and o["dist"] <= 2.0,
          "px<=2.5c (floor), 2bp")
    score(obs, lambda o: o["px"] <= 0.025 and o["dist"] <= 1.0,
          "  + dist<=1bp")
    score(obs, lambda o: o["px"] <= 0.025 and o["dist"] <= 1.0 and o["tau"] >= 10,
          "  + tau>=10s")
    print()
    print("=== the 'certainty' axis: distance relative to the move left ===")
    for lo, hi in ((0.0, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 4.0), (4.0, 1e9)):
        score(obs, lambda o, lo=lo, hi=hi: o["px"] <= 0.03 and lo <= o["z"] < hi,
              f"px<=3c, z {lo}-{hi if hi < 1e8 else 'inf'}")
    print()
    print("=== the stale-display axis (venue has not repriced this side) ===")
    for lo in (-1.0, 0.0, 0.02, 0.05):
        score(obs, lambda o, lo=lo: o["px"] <= 0.03 and (o["disp"] - o["px"]) >= lo,
              f"px<=3c, display-ask>={lo}")
    print()
    print("=== THE SIGNED AXIS: is the cheap side already in the money? ===")
    print("    (>0 means the underlying is on the cheap side's side of the strike,")
    print("     i.e. the venue is quoting a winner as a longshot)")
    for lo, hi in ((0.0, 1.0), (1.0, 2.0), (2.0, 5.0), (5.0, 1e9),
                   (-1.0, 0.0), (-2.0, -1.0), (-5.0, -2.0), (-1e9, -5.0)):
        score(obs, lambda o, lo=lo, hi=hi: o["px"] <= 0.03
              and lo <= o["ahead_bp"] < hi,
              f"px<=3c, ahead {lo}..{hi if hi < 1e8 else 'inf'}bp")
    print()
    print("=== the same, at the floor price only ===")
    for lo, hi in ((0.0, 1.0), (1.0, 3.0), (3.0, 1e9), (-1e9, 0.0)):
        score(obs, lambda o, lo=lo, hi=hi: o["px"] <= 0.025
              and lo <= o["ahead_bp"] < hi,
              f"px<=2.5c, ahead {lo}..{hi if hi < 1e8 else 'inf'}bp")
    print()
    print("=== and in the money but still cosmetically cheap (the dislocation) ===")
    score(obs, lambda o: o["px"] <= 0.03 and o["ahead_bp"] > 0,
          "px<=3c AND ahead")
    score(obs, lambda o: o["px"] <= 0.05 and o["ahead_bp"] > 0,
          "px<=5c AND ahead")
    print()
    print("=== vol regime (is a flip likelier when vol is high?) ===")
    if sig:
        med = st.median(sig.values())
        score(obs, lambda o: o["px"] <= 0.03 and o["sigma"] >= med,
              "px<=3c, sigma>=median")
        score(obs, lambda o: o["px"] <= 0.03 and o["sigma"] < med,
              "px<=3c, sigma<median")


if __name__ == "__main__":
    main()
