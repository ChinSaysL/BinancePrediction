"""CAN THE EXIT BE MADE TO TELL A REVERSAL FROM NOISE? A grid, honestly scored.

THE PROBLEM, stated as the live record states it. The exit that shipped cut 20
WINNERS for -24.051 while saving 23 LOSERS for +17.380. The corrected rule is
better but the shape is the same, and the reason is structural: "the signal
crossed back over the level" is SYMMETRIC information. It fires the same way on
a genuine reversal and on a position that merely sat near the level -- and by
construction every entry starts near the level, because `crossed_side` fires on
the first tick past it.

So the question is not "should we exit" but "is there a SIDE CONDITION that
separates the two cohorts". This sweeps the candidates:

  FLOOR    the flip must clear `k` bps of its own margin (the shipped fix uses
           the ENTRY's floor, 0.5; there is no reason the two must be equal --
           leaving costs a spread AND gives up the position's option value, so
           the exit's bar arguably belongs HIGHER than the entry's)
  GIVEBACK the signal has retraced `k` x the margin we ENTERED on. This is the
           scale-free version: it asks "has the move that justified this trade
           been undone", which is the actual thesis, rather than "where is the
           signal relative to an arbitrary level"
  TAU      only exit while `tau >= T` (a flip with 20 s left cannot recover; a
           flip with 150 s left is a different animal) -- variant F of
           pm_exit_flip was the best of the original battery
  DRAW     only exit when the position is already DOWN: the bid has fallen `d`
           below the entry. Cutting a winner is what costs; this refuses to.

Every cell reports the WINNER/LOSER SPLIT, because the aggregate hides the
failure: a rule can be flat overall while cutting winners and losers in equal
measure, and that is a coin-flip paying two spreads, not an edge.

⛔ THE GRID IS LARGE AND THE ARGMAX OVER IT IS NOT A RESULT. This file's own
record: "the argmax over ~290 estimates manufactures a winner". So every cell
carries the repo's PASS bar (pooled t>=2, day-t>=2, both halves positive,
drop-best-day positive) and the best cells are re-scored on the LIVE FILLS,
which is a different sample and the one that decides.

    /root/analysis-venv/bin/python tools/pm_exit_explore.py
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_cross_replay as base  # noqa: E402

FEE = base.FEE


def scan(pre, entry, look_s, hold_s, tau_min):
    """Every post-entry tick as (ts, tau, side, margin, own_bid, exit_px)."""
    src, src_ts, level = pre["fc"], pre["fc_ts"], pre["level"]
    if not src or not level:
        return
    rows = pre["rows"]
    tail = max(10, int((hold_s + look_s + 2.0) * 1.6) + 6)
    held = entry["side"]
    for ts, tau, up_ask, dn_ask in zip(rows.ts_ms, rows.tau_s,
                                       rows.up_ask, rows.down_ask):
        ts = int(ts)
        if ts <= entry["ts_ms"] or tau < tau_min:
            continue
        j = int(np.searchsorted(src_ts, ts, side="right"))
        if j < 4:
            continue
        side = base.crossed_side(src[max(0, j - tail):j], level, look_s,
                                 hold_s=hold_s)
        m = 1e4 * (src[j - 1][1] - level) / level
        opp = dn_ask if held == "Up" else up_ask
        px = (1.0 - float(opp)) if opp and 0.0 < float(opp) < 1.0 else None
        yield ts, float(tau), side, m, px


def walk(pre, entry, *, look_s, hold_s, tau_min, confirm_s,
         floor, giveback, tau_gate, draw):
    """Fire the exit under one rule spec. Returns the exit dict or None."""
    held = entry["side"]
    other = "Down" if held == "Up" else "Up"
    m0 = abs(entry["margin"])            # the margin we ENTERED on
    armed = None
    for ts, tau, side, m, px in scan(pre, entry, look_s, hold_s, tau_min):
        if tau_gate is not None and tau < tau_gate:
            break                        # too late to act, and it only gets later
        if side == held and abs(m) >= floor:
            armed = None                 # genuinely back on our side
            continue
        if side != other:
            continue
        if abs(m) < floor:
            continue
        # GIVEBACK: the move that justified the entry must be undone by k x it.
        if giveback is not None and m0 > 0 and abs(m) < giveback * m0:
            continue
        # DRAW: only cut a position that is already losing on the book.
        if draw is not None:
            if px is None or px > entry["ask"] - draw:
                continue
        if armed is None:
            armed = ts
        if confirm_s <= 0 or (ts - armed) / 1000.0 >= confirm_s:
            if px is None:
                continue
            return dict(px=px, ts_ms=ts, tau=tau,
                        lag_s=(ts - entry["ts_ms"]) / 1000.0)
    return None


def score(entries, walks, label, out):
    """Battery + the winner/loser split that the aggregate hides."""
    rows, cut_w, cut_l, sav_w, sav_l = [], 0, 0, 0.0, 0.0
    n_ex = 0
    for (pre, e), f in zip(entries, walks):
        hold = (1.0 if e["won"] else 0.0) - e["ask"] - FEE(e["ask"])
        if f is None:
            rows.append(dict(day=e["day"], per=hold, ts_ms=e["ts_ms"]))
            continue
        n_ex += 1
        per = f["px"] - FEE(f["px"]) - e["ask"] - FEE(e["ask"])
        rows.append(dict(day=e["day"], per=per, ts_ms=e["ts_ms"]))
        if e["won"]:
            cut_w += 1
            sav_w += per - hold
        else:
            cut_l += 1
            sav_l += per - hold
    d = pd.DataFrame(rows).sort_values("ts_ms")
    v = d.per.to_numpy()
    n = len(v)
    t = v.mean() / (v.std(ddof=1) / math.sqrt(n)) if n > 1 and v.std(ddof=1) else 0.0
    dm = d.groupby("day").per.mean().to_numpy()
    day_t = (dm.mean() / (dm.std(ddof=1) / math.sqrt(len(dm)))
             if len(dm) > 1 and dm.std(ddof=1) else 0.0)
    if len(dm) > 1:
        best = d.groupby("day").per.mean().idxmax()
        drop = d[d.day != best].per.mean()
    else:
        drop = v.mean()
    h = n // 2
    h1, h2 = v[:h].mean(), v[h:].mean()
    ok = (t >= 2 and day_t >= 2 and h1 > 0 and h2 > 0 and drop > 0)
    out.append(dict(label=label, n_ex=n_ex, per=v.mean(), tot=v.sum(), t=t,
                    day_t=day_t, h1=h1, h2=h2, drop=drop, ok=ok,
                    cut_w=cut_w, cut_l=cut_l, sav_w=sav_w, sav_l=sav_l))
    return out[-1]


def line(r, base_per, base_tot=None):
    """`be` is THE ROBUSTNESS NUMBER and it is why total is the wrong ranking.

    The replay prices every exit at `1 - other_ask`; the real fill is the bid,
    measured ~1c/share away. A cell's whole gain is (tot - base_tot) spread over
    `n_ex` exits, so it survives a per-share degradation of exactly
    (tot - base_tot) / n_ex. A cell that earns a big total from MANY exits is
    the fragile one -- it is paying two spreads on every one of them.
    """
    gain = (r["tot"] - base_tot) if base_tot is not None else 0.0
    be = (gain / r["n_ex"]) if r["n_ex"] else float("inf")
    ratio = (r["sav_l"] / abs(r["sav_w"])) if r["sav_w"] else float("inf")
    return (f"  {r['label']:<34} ex={r['n_ex']:4d} per={r['per']:+.4f} "
            f"({r['per']-base_per:+.4f}) tot={r['tot']:+7.2f} t={r['t']:+5.2f} "
            f"day-t={r['day_t']:+5.2f} "
            f"| cut {r['cut_l']:3d}L{r['sav_l']:+7.2f} {r['cut_w']:3d}W{r['sav_w']:+7.2f} "
            f"x{ratio:4.2f} | be={be*100:5.1f}c "
            f"[{'PASS' if r['ok'] else 'fail'}]")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default=base.DB)
    ap.add_argument("--tau-min", type=float, default=8.0)
    ap.add_argument("--px-lo", type=float, default=0.10)
    args = ap.parse_args()
    from btcpredict.config import settings
    look_s, hold_s = base.shipped_look_s(), base.shipped_hold_s()
    floor_entry = float(settings.cross_min_margin_bps)
    px_hi = float(settings.cross_max_fill_px)
    tau_lo, tau_hi = float(settings.cross_min_tau_s), float(settings.cross_max_tau_s)
    confirm = float(settings.cross_exit_confirm_s)

    r, q = base.load(args.db)
    q = q.copy(); q["fam"] = q.slug.map(base.family_of)
    entries = []
    for _, row in r.iterrows():
        pre = base.precompute(q, row, look_s, args.px_lo, px_hi, tau_lo, tau_hi)
        if pre is None:
            continue
        e = base.pick(pre, signal="fc", floor_bps=floor_entry, look_s=look_s,
                      px_lo=args.px_lo, px_hi=px_hi, tau_lo=tau_lo,
                      tau_hi=tau_hi, hold_s=hold_s)
        if e is not None:
            entries.append((pre, e))
    print(f"entries {len(entries)} | entry floor {floor_entry} tau "
          f"[{tau_lo:.0f},{tau_hi:.0f}] look {look_s} confirm {confirm}s")
    print("  'cut  nL+x  mW+y' = n LOSERS cut (saving x) and m WINNERS cut "
          "(costing y). A rule that is flat overall while cutting both is a")
    print("  coin flip paying two spreads. The split is the diagnostic.\n")

    out = []
    b = score(entries, [None] * len(entries), "A hold to the bell (base)", out)
    base_per, base_tot = b["per"], b["tot"]
    print(line(b, base_per, base_tot))

    def run(label, **kw):
        kw.setdefault("confirm_s", confirm)
        kw.setdefault("floor", floor_entry)
        kw.setdefault("giveback", None)
        kw.setdefault("tau_gate", None)
        kw.setdefault("draw", None)
        w = [walk(pre, e, look_s=look_s, hold_s=hold_s,
                  tau_min=args.tau_min, **kw) for pre, e in entries]
        print(line(score(entries, w, label, out), base_per, base_tot))

    print("\n--- the shipped fix, and the exit's own floor swept ---")
    run(f"B flip, floor={floor_entry} (SHIPPED)")
    for k in (1.0, 2.0, 3.0, 5.0):
        run(f"C flip, exit floor={k:.1f} bps", floor=k)
    print("\n--- GIVEBACK: the entry's own move must be undone k-fold ---")
    for k in (0.5, 1.0, 1.5, 2.0):
        run(f"D giveback>={k:.1f}x entry margin", giveback=k)
    print("\n--- TAU: only act while there is still time to be wrong ---")
    for T in (30.0, 60.0, 90.0, 120.0):
        run(f"E only while tau>={T:.0f}s", tau_gate=T)
    print("\n--- DRAW: never cut a position that is still up ---")
    for d in (0.0, 0.05, 0.10):
        run(f"F only if bid <= entry-{d:.2f}", draw=d)
    print("\n--- COMBINATIONS of the parts that helped on their own ---")
    run("H tau>=60 + draw>=0.00", tau_gate=60.0, draw=0.0)
    run("H tau>=60 + giveback>=1.0x", tau_gate=60.0, giveback=1.0)
    run("H tau>=60 + confirm=5s", tau_gate=60.0, confirm_s=5.0)
    run("H tau>=60 + draw + confirm=5s", tau_gate=60.0, draw=0.0, confirm_s=5.0)
    print("\n--- confirm_s on the shipped floor ---")
    for c in (0.0, 1.0, 5.0, 10.0):
        run(f"G confirm={c:.0f}s", confirm_s=c)

    print("\n=== the cells that PASS, ranked by total ===")
    ps = [x for x in out if x["ok"] and x["n_ex"] > 0]
    for x in sorted(ps, key=lambda z: -z["tot"])[:6]:
        print(line(x, base_per, base_tot))
    print("\n=== the same cells ranked by ROBUSTNESS (breakeven degradation) ===")
    print("  This is the ranking that matters: `be` is the per-share fill error")
    print("  the cell absorbs before its gain is gone. The measured model error")
    print("  is ~1c. A cell at be=2c is a coin flip; at be=15c it has room.")
    for x in sorted(ps, key=lambda z: -((z["tot"]-base_tot)/z["n_ex"] if z["n_ex"] else 0))[:6]:
        print(line(x, base_per, base_tot))
    if not ps:
        print("  NONE. No side condition in this grid separates the cohorts.")
    print(f"\n  cells tried: {len(out)-1}. Treat the best as a HYPOTHESIS, not a")
    print("  result -- it is an argmax over that many. The live fills decide.")


main()
