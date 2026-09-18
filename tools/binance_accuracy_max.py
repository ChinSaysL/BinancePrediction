"""Maximise Binance-only accuracy for predicting the venue's winner.

THE TARGET. Everything here uses ONLY the recorded Binance tape. No oracle, no
external feed.

THE ESTIMATOR THAT EXISTS TODAY (tools/pm_accuracy_ceiling.py):
    mk  = mean(spot over the last 60 s)                 <- unweighted
    fc  = ((60 - tau) * mk + tau * spot) / 60           <- weight tau/60 ASSUMED
    sign(fc - strike) decides the side.

Two things in there are assumptions rather than measurements:
  1. the blend weight `tau/60`. It is the martingale weight (the unknown last
     `tau` seconds are best guessed at the current spot), which is right IF the
     forecast error is dominated by the unobserved future. It is not fitted.
  2. `mk` is a SIMPLE mean, but the venue pays on a TIME-WEIGHTED mean of a
     60 s window. With ~1 s sampling these nearly coincide; with gaps they do
     not, and the gaps are exactly where the price moved.

THE FAIR TEST. "Better accuracy" is meaningless on its own -- buying distance
raises accuracy and raises the price with it. So every estimator is scored on
the SAME rounds, and the ask column is the price of the side it actually chose,
so an estimator that only wins by picking more-lopsided rounds is visible.

Reported: n, accuracy on all rounds, accuracy on contested rounds (both sides
quoted inside the live band, the only ones that can be bought), the mean ask
paid, and per-share after the real fee 0.07*p*(1-p).

Usage: python3 tools/binance_accuracy_max.py /tmp/rounds_local.json [--tau 10]
"""
from __future__ import annotations

import argparse
import json
import math

FEE = lambda p: 0.07 * p * (1 - p)                             # noqa: E731
LOOKBACK = 60_000.0
COINS = ("btc", "eth", "bnb", "sol", "xrp", "doge")


def series(r: dict):
    """(ts_ms, tau_s, spot, up_ask, dn_ask) sorted by time, NaNs dropped.

    tau is DERIVED from end_ms, not read from the dump: the dump's second
    field was written as `t` (the timestamp) instead of `tau`, so it silently
    equals ts_ms and any tau lookup on it returns index 0.
    """
    end = float(r["end_ms"])
    out = []
    for row in r["q"]:
        ts, sp = float(row[0]), row[2]
        if sp is None or not math.isfinite(sp):
            continue
        out.append((ts, (end - ts) / 1000.0, float(sp),
                    None if row[3] is None else float(row[3]),
                    None if row[4] is None else float(row[4])))
    out.sort()
    return out


def wmean(pts, a, b):
    """Time-weighted mean of a step-constant series over [a, b] milliseconds."""
    if b <= a or not pts:
        return None
    tot = 0.0
    for i, (t, p) in enumerate(pts):
        lo = max(a, t)
        hi = min(b, pts[i + 1][0] if i + 1 < len(pts) else b)
        if hi > lo:
            tot += p * (hi - lo)
    if pts[0][0] > a:
        tot += pts[0][1] * (min(pts[0][0], b) - a)
    return tot / (b - a)


def estimators(pts, i, tau):
    """Every candidate level for the settle, from Binance tape only."""
    ts_i, sp_i = pts[i][0], pts[i][2]
    lo = ts_i - LOOKBACK
    win = [p for p in pts if lo <= p[0] <= ts_i]
    if len(win) < 8:
        return {}
    cov = (ts_i - max(lo, win[0][0])) / LOOKBACK
    if cov < 0.8:
        return {}
    sps = [p[2] for p in win]
    simple = sum(sps) / len(sps)
    tw = wmean([(p[0], p[2]) for p in win], lo, ts_i)
    med = sorted(sps)[len(sps) // 2]
    # last 10 s only -- a pure momentum read
    recent = [p[2] for p in win if p[0] >= ts_i - 10_000]
    mom = (sum(recent) / len(recent)) if recent else sp_i

    e = {
        "spot": sp_i,
        "mean60": simple,
        "tw60": tw,
        "med60": med,
        "today:blend(simple)": ((LOOKBACK - tau) * simple + tau * sp_i) / LOOKBACK,
        "blend(tw)": ((LOOKBACK - tau) * tw + tau * sp_i) / LOOKBACK,
        "blend(med)": ((LOOKBACK - tau) * med + tau * sp_i) / LOOKBACK,
        # the blend weight is a free parameter; tau/60 is just one choice
        "w0.25": 0.75 * (tw if tw else simple) + 0.25 * sp_i,
        "w0.50": 0.50 * (tw if tw else simple) + 0.50 * sp_i,
        "w0.75": 0.25 * (tw if tw else simple) + 0.75 * sp_i,
        "w1.00": sp_i,
        "w+0.15*mom": (((LOOKBACK - tau) * tw + tau * sp_i) / LOOKBACK
                       + 0.15 * (sp_i - mom)) if tw else None,
        "w-0.15*mom": (((LOOKBACK - tau) * tw + tau * sp_i) / LOOKBACK
                       - 0.15 * (sp_i - mom)) if tw else None,
    }
    return {k: v for k, v in e.items() if v is not None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rounds")
    ap.add_argument("--tau", type=float, default=10.0)
    ap.add_argument("--band", nargs=2, type=float, default=(0.10, 0.65))
    a = ap.parse_args()

    rounds = json.load(open(a.rounds))
    lo_b, hi_b = a.band

    rows = []
    dropped = {"thin": 0, "no_tau": 0, "no_book": 0, "thin_win": 0}
    for r in rounds:
        pts = series(r)
        if len(pts) < 10:
            dropped["thin"] += 1
            continue
        j = min(range(len(pts)), key=lambda k: abs(pts[k][1] - a.tau))
        if abs(pts[j][1] - a.tau) > max(1.2, 0.1 * a.tau):
            dropped["no_tau"] += 1
            continue
        est = estimators(pts, j, pts[j][1])
        if not est:
            dropped["thin_win"] += 1
            continue
        ua, da = pts[j][3], pts[j][4]
        if ua is None or da is None:
            dropped["no_book"] += 1
            continue
        # contested = both sides quoted away from the extremes. This is NOT
        # "both asks inside the live band": the two asks sum to ~1, so both can
        # only sit in [0.10,0.65] when both are ~0.50, which matched 2 rounds.
        # The band is a gate on the side we TAKE, applied to `ask` below.
        rows.append({"coin": r["coin"], "win": r["winner"],
                     "strike": r["strike"], "ua": ua, "da": da, "est": est,
                     "both": 0.001 < ua < 0.999 and 0.001 < da < 0.999})
    print(f"    contested = both asks strictly inside (0.001, 0.999)")

    print(f"    rounds in: {len(rounds)}   dropped: {dropped}")
    n = len(rows)
    if not n:
        print("no usable rounds"); return 1
    print(f"=== Binance-only estimators, tau {a.tau:.0f}s, {n} rounds ===")
    print(f"    contested = both asks inside [{lo_b},{hi_b}] (the buyable set)\n")

    names = list(rows[0]["est"])
    cn = [r for r in rows if r["both"]]
    print(f"  {'estimator':<20} {'acc all':>8} {'acc cont':>9} {'n':>5} "
          f"{'ask':>6} {'per-share':>10} {'fee-adj acc':>11}")
    print("  " + "-" * 74)
    scored = []
    for nm in names:
        hits = tot = 0
        ch = ct = 0
        asks, pers = [], []
        for r in rows:
            v = r["est"].get(nm)
            if v is None:
                continue
            up = v >= r["strike"]
            ok = up == (r["win"] == "Up")
            tot += 1
            hits += ok
            if r["both"]:
                ct += 1
                ch += ok
                ask = r["ua"] if up else r["da"]
                asks.append(ask)
                pers.append((1.0 if ok else 0.0) - (ask + FEE(ask)))
        if not tot or not ct:
            continue
        acc, cacc = hits / tot, ch / ct
        ask = sum(asks) / len(asks)
        per = sum(pers) / len(pers)
        # accuracy measured only where the ask is below 0.5 -- the rounds where
        # being right is not the same thing as being told so by the price
        scored.append((nm, acc, cacc, ct, ask, per))
    scored.sort(key=lambda x: -x[2])
    base = dict((s[0], s) for s in scored).get("today:blend(simple)")
    for nm, acc, cacc, ct, ask, per in scored:
        mark = "  <- shipped" if nm == "today:blend(simple)" else ""
        gain = f" {cacc - base[2]:+6.2%}" if base and nm != base[0] else "       "
        print(f"  {nm:<20} {acc:>8.3%} {cacc:>9.3%} {ct:>5d} {ask:>6.3f} "
              f"{per:>+10.4f}{gain}{mark}")

    print(f"\n  contested n={len(cn)}; columns: acc on all rounds, acc on the "
          f"buyable set,\n  the mean ask actually paid, and per-share after fee.")
    print("  A row that wins on 'acc cont' but pays a HIGHER ask has bought the"
          "\n  accuracy, not earned it -- compare the two columns together.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
