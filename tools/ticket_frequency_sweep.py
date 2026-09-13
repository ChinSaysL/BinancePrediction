"""How frequent can the cheap-side ticket be before it stops paying?

The ticket arm is the only mechanism in this repo with measured evidence that
does not rest on predicting direction: it buys the side the venue still quotes
cheaply while the underlying is genuinely near the strike. Its two conditioners
were measured, but the arm it feeds is capped at 10/day and the gates admit
~2.5% of rounds, so frequency was never the thing being optimised.

This sweeps the rule on the recorded quotes to find the most FREQUENT cell that
still clears the battery. Axes:
  band   - the ask range we are willing to pay
  tau    - how close to the bell we enter (the venue's display lags its own book
           by ~7s, so staleness and time-to-bell are the same axis)
  div    - ask MINUS the venue's own displayed price for that side. Positive
           means its display sits under its own book: the venue is stale, which
           is the mispricing being harvested.
  z      - |ln(spot/strike)| / (sigma_1m*sqrt(tau/60)): how many sigma of the
           REMAINING move separate spot from strike. Under ~0.8 the historical
           cells were 0.42x / 0.00x / 0.75x -- all negative.

One ticket per round, the FIRST qualifying quote, priced at the ask we would
actually pay, fee = 200bps * min(1,(1-p)/p) of stake = 0.02*min(p,1-p) per
share. Frequency is reported as tickets/day so a cell can be judged on both
axes at once.
"""
from __future__ import annotations

import collections
import math
import sqlite3
import sys
import time

FEE_OF = lambda p: 0.02 * min(p, 1.0 - p)  # noqa: E731


def battery(v, days):
    if len(v) < 30:
        return "n<30", False
    m = sum(v) / len(v)
    sd = (sum((x - m) ** 2 for x in v) / (len(v) - 1)) ** 0.5
    t = m / (sd / math.sqrt(len(v))) if sd else float("nan")
    g = collections.defaultdict(list)
    for x, d in zip(v, days):
        g[d].append(x)
    gm = {k: sum(y) / len(y) for k, y in g.items()}
    mm = sum(gm.values()) / len(gm)
    sg = (sum((x - mm) ** 2 for x in gm.values()) / (len(gm) - 1)) ** 0.5 \
        if len(gm) > 1 else 0.0
    td = mm / (sg / math.sqrt(len(gm))) if sg else float("nan")
    h = len(v) // 2
    h1 = sum(v[:h]) / len(v[:h])
    h2 = sum(v[h:]) / len(v[h:])
    drop = sum(x for k, x in gm.items() if k != max(gm, key=gm.get)) / (len(gm) - 1)
    ok = (td > 2) and h1 > 0 and h2 > 0 and drop > 0
    return f"t {t:+5.2f} day-t {td:+5.2f} h {h1:+.3f}/{h2:+.3f} drop1 {drop:+.3f}", ok


def main(db="data/btcpredict.db"):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    rows = c.execute("""
        SELECT q.slug, q.ts_ms, q.tau_s, q.spot, q.strike,
               q.up_price, q.down_price, q.up_ask, q.down_ask, r.winner
        FROM quotes q JOIN rounds r ON r.slug = q.slug
        WHERE r.winner IS NOT NULL AND q.spot IS NOT NULL AND q.strike > 0
          AND q.tau_s > 0
        ORDER BY q.slug, q.ts_ms
    """).fetchall()
    print(f"quotes joined: {len(rows):,}")
    # per-round sigma_1m from the round's own spot path
    spots = collections.defaultdict(list)
    for r in rows:
        spots[r["slug"]].append(r["spot"])
    sig = {}
    for slug, s in spots.items():
        if len(s) < 10:
            continue
        d = [math.log(s[i] / s[i - 1]) for i in range(1, len(s)) if s[i - 1] > 0]
        if len(d) < 5:
            continue
        mu = sum(d) / len(d)
        var = sum((x - mu) ** 2 for x in d) / (len(d) - 1)
        sig[slug] = max(math.sqrt(var) * math.sqrt(60.0), 1e-5)
    print(f"rounds with a measurable sigma: {len(sig):,}")

    # candidate entries: one per (round, side) is too many; the live rule takes
    # the FIRST qualifying quote per round, so collect candidates then pick.
    cand = collections.defaultdict(list)   # slug -> list of dicts
    for r in rows:
        s = sig.get(r["slug"])
        if s is None or r["tau_s"] <= 0:
            continue
        z = abs(math.log(r["spot"] / r["strike"])) / (s * math.sqrt(r["tau_s"] / 60.0))
        for side, ask, disp in (("Up", r["up_ask"], r["up_price"]),
                                ("Down", r["down_ask"], r["down_price"])):
            if ask is None or disp is None or not (0.0 < ask < 1.0):
                continue
            cand[r["slug"]].append(dict(
                tau=r["tau_s"], ask=ask, div=ask - disp, z=z, side=side,
                win=(r["winner"] == side), day=r["ts_ms"] // 86400000))
    for k in cand:
        cand[k].sort(key=lambda x: -x["tau"])     # earliest in the round first
    nd = len(set(q["day"] for v in cand.values() for q in v[:1]))
    print(f"rounds with candidates: {len(cand):,} over {nd} days "
          f"({len(cand) / max(nd,1):.0f} rounds/day)\n")

    def run(band, tau_max, div_min, z_min, tag):
        v, days, wins, asks = [], [], 0, []
        for slug, lst in cand.items():
            for q in lst:
                if q["tau"] > tau_max:
                    continue
                if not (band[0] <= q["ask"] <= band[1]) or q["div"] < div_min \
                        or q["z"] < z_min:
                    continue
                fee = FEE_OF(q["ask"])
                v.append((float(q["win"]) - q["ask"] - fee) / q["ask"])
                days.append(q["day"])
                wins += q["win"]
                asks.append(q["ask"])
                break
        if len(v) < 30:
            print(f"  {tag:46s} n={len(v):5d} (<30)")
            return None
        b, ok = battery(v, days)
        print(f"  {tag:46s} n={len(v):5d} ({len(v)/max(nd,1):5.1f}/day) "
              f"win {wins/len(v):5.3f} ask {sum(asks)/len(asks):.3f} "
              f"EV/$1 {sum(v)/len(v):+.3f} {'PASS' if ok else '    '}  {b}")
        return sum(v) / len(v), len(v) / max(nd, 1), ok

    print("=== A. the shipped cell, div swept (band 0.08-0.20, tau<=8s) ===")
    for dv in (0.0, 0.02, 0.03, 0.05, 0.08, 0.12):
        run((0.08, 0.20), 8, dv, 0.0, f"div>={dv:.2f}")

    print("\n=== B. what does the div gate cost in frequency? (no z, tau<=15s) ===")
    for dv in (0.0, 0.02, 0.05):
        run((0.08, 0.20), 15, dv, 0.0, f"band .08-.20 div>={dv:.2f}")

    print("\n=== C. widen the band for frequency (tau<=15s, div>=0.02) ===")
    for band in ((0.08, 0.20), (0.08, 0.30), (0.05, 0.35), (0.05, 0.50),
                 (0.10, 0.40), (0.20, 0.50), (0.30, 0.60)):
        run(band, 15, 0.02, 0.0, f"band {band[0]:.2f}-{band[1]:.2f}")

    print("\n=== D. longer windows for frequency (band 0.08-0.35, div>=0.02) ===")
    for t in (8, 15, 30, 60, 120, 210):
        run((0.08, 0.35), t, 0.02, 0.0, f"tau<={t}s")

    print("\n=== E. the z conditioner inside the widest paying band ===")
    for zz in (0.0, 0.3, 0.5, 0.8, 1.2):
        run((0.08, 0.35), 30, 0.02, zz, f"z>={zz:.1f}")

    print("\n=== F. both sides cheap / distance from strike ignored ===")
    for band in ((0.02, 0.08), (0.05, 0.15), (0.15, 0.30), (0.25, 0.45)):
        run(band, 15, 0.02, 0.0, f"band {band[0]:.2f}-{band[1]:.2f}")


if __name__ == "__main__":
    main(*sys.argv[1:])
