"""Does the blend weight actually matter, or did I just fit 52 comparisons?

THE CLAIM TO TEST. tools/binance_accuracy_max.py scored ~13 estimators at 4 taus
and at tau=35 the pure spot hit 68.7% on contested rounds against the shipped
blend's 61.5%. That is +7.3 pp, but n=179 gives a standard error near 3.7 pp, and
the winner was chosen from ~52 looks -- so a gap that size is roughly what noise
produces. Picking the best of N on one dataset is fitting, not a finding.

THE RULE (from hard experience on this project): never quote the exact optimum.
Re-run the sweep on the FIRST HALF and the SECOND HALF of the rounds separately
and require the same RANKING. Ship the direction the evidence repeats.

The shipped estimator is
    fc = ((60 - tau) * mean60 + tau * spot) / 60
i.e. a weight of tau/60 on the spot. Here the weight is swept freely:

    fc(w) = (1 - w) * mean60 + w * spot

Everything is Binance-only. Reported per w: accuracy on all rounds, accuracy on
CONTESTED rounds (both asks strictly inside (0.001, 0.999)), the mean ask of the
side taken, and per-share after the real 0.07*p*(1-p) fee -- then the same two
numbers for each half, because only a ranking that repeats is real.

Usage: python3 tools/binance_weight_sweep.py /tmp/rounds_local.json --tau 35
"""
from __future__ import annotations

import argparse
import json
import math

FEE = lambda p: 0.07 * p * (1 - p)                             # noqa: E731
LOOKBACK = 60_000.0


def components(r: dict):
    """spot, simple mean, and TIME-WEIGHTED mean over the trailing 60 s."""
    end = float(r["end_ms"])
    pts = [(float(x[0]), x[2], x[3], x[4]) for x in r["q"] if x[2] is not None]
    if len(pts) < 10:
        return None
    pts.sort()
    # tau reconstructed from end_ms; the dump's tau field is the ts repeated
    j = min(range(len(pts)), key=lambda k: abs((end - pts[k][0]) / 1000.0 - TAU))
    tau = (end - pts[j][0]) / 1000.0
    if abs(tau - TAU) > max(1.2, 0.1 * TAU):
        return None
    ts_i, sp_i, ua, da = pts[j][0], pts[j][1], pts[j][2], pts[j][3]
    lo = ts_i - LOOKBACK
    win = [p for p in pts if lo <= p[0] <= ts_i]
    if len(win) < 8 or (ts_i - max(lo, win[0][0])) / LOOKBACK < 0.8:
        return None
    sps = [p[1] for p in win]
    simple = sum(sps) / len(sps)
    tot = 0.0
    for i, p in enumerate(win):
        a = max(lo, p[0])
        b = min(ts_i, win[i + 1][0] if i + 1 < len(win) else ts_i)
        if b > a:
            tot += p[1] * (b - a)
    if win[0][0] > lo:
        tot += win[0][1] * (min(win[0][0], ts_i) - lo)
    tw = tot / (ts_i - lo)
    return dict(ts=ts_i, tau=tau, spot=sp_i, simple=simple, tw=tw,
                ua=ua, da=da, strike=float(r["strike"]), win=r["winner"],
                coin=r["coin"])


def contested(r: dict) -> bool:
    """Both sides materially quoted. Asks can legitimately be None, so this
    must guard before comparing or the whole sweep dies on a TypeError."""
    ua, da = r["ua"], r["da"]
    return (ua is not None and da is not None
            and 0.001 < ua < 0.999 and 0.001 < da < 0.999)


def acc_contested(rounds, w, key):
    hits = n = 0
    asks, pers = [], []
    for r in rounds:
        if not contested(r):
            continue
        v = (1 - w) * r[key] + w * r["spot"]
        up = v >= r["strike"]
        ok = up == (r["win"] == "Up")
        n += 1
        hits += ok
        ask = r["ua"] if up else r["da"]
        asks.append(ask)
        pers.append((1.0 if ok else 0.0) - (ask + FEE(ask)))
    if n < 4:
        return None
    return hits / n, n, sum(asks) / len(asks), sum(pers) / len(pers)


def acc_all(rounds, w, key):
    hits = 0
    for r in rounds:
        up = ((1 - w) * r[key] + w * r["spot"]) >= r["strike"]
        hits += up == (r["win"] == "Up")
    return hits / len(rounds)


def main() -> int:
    global TAU
    ap = argparse.ArgumentParser()
    ap.add_argument("rounds")
    ap.add_argument("--tau", type=float, default=35.0)
    ap.add_argument("--key", default="tw", choices=("tw", "simple"))
    ap.add_argument("--step", type=float, default=0.05)
    a = ap.parse_args()
    TAU = a.tau

    raw = json.load(open(a.rounds))
    rows = [c for c in (components(r) for r in raw) if c]
    rows.sort(key=lambda r: r["ts"])
    if len(rows) < 40:
        print(f"only {len(rows)} usable rounds"); return 1
    half = len(rows) // 2
    h1, h2 = rows[:half], rows[half:]
    cn = sum(1 for r in rows if contested(r))
    print(f"=== blend-weight sweep, tau {TAU:.0f}s, mean-of-window = {a.key} ===")
    print(f"    {len(rows)} rounds, {cn} contested; halves {len(h1)}/{len(h2)}")
    print(f"    shipped weight = tau/60 = {TAU / 60:.3f}\n")
    print(f"  {'w(spot)':>8} {'acc all':>8} {'acc cont':>9} {'ask':>6} "
          f"{'per-share':>10} | {'h1':>8} {'h2':>8} {'same dir?':>10}")
    print("  " + "-" * 76)

    w = 0.0
    res = []
    while w <= 1.0001:
        c = acc_contested(rows, w, a.key)
        aa = acc_all(rows, w, a.key)
        c1 = acc_contested(h1, w, a.key)
        c2 = acc_contested(h2, w, a.key)
        if c:
            tag = ""
            if c1 and c2:
                # both halves must beat their own w=0 baseline in the same way
                b1 = acc_contested(h1, 0.0, a.key)
                b2 = acc_contested(h2, 0.0, a.key)
                tag = ("yes" if (c1[0] - b1[0]) > 0 and (c2[0] - b2[0]) > 0 else
                       "no")
            res.append((w, aa, c, c1, c2, tag))
            print(f"  {w:>8.2f} {aa:>8.3%} {c[0]:>9.3%} {c[2]:>6.3f} "
                  f"{c[3]:>+10.4f} | {(c1[0] if c1 else float('nan')):>8.3%} "
                  f"{(c2[0] if c2 else float('nan')):>8.3%} {tag:>10}")
        w += a.step

    if not res:
        return 0
    best = max(res, key=lambda x: x[2][0])
    ship = min(res, key=lambda x: abs(x[0] - TAU / 60))
    print(f"\n  shipped  w={ship[0]:.2f}  contested {ship[2][0]:.3%}  "
          f"per-share {ship[2][3]:+.4f}")
    print(f"  best     w={best[0]:.2f}  contested {best[2][0]:.3%}  "
          f"per-share {best[2][3]:+.4f}   "
          f"({best[2][0] - ship[2][0]:+.2%} vs shipped)")
    ok = [x for x in res if x[5] == "yes"]
    print(f"  weights beating w=0 in BOTH halves: "
          f"{[round(x[0], 2) for x in ok] or 'NONE'}")
    print("  -> a weight only ships if its direction repeats in both halves;"
          "\n     if the list above is empty or scatters, the earlier 7 pp was"
          "\n     the best of N draws, not a signal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
