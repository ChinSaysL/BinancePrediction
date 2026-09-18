"""WHY eth-5m AND xrp-5m BLEED -- test the SIGNAL, not the money.

WHAT THIS FIXES. The knob sweep (`tools/pm_param_sweep.py`) left two families
negative at every floor -- eth-updown-5m (n=21/13/7/5 at floors 0.00/0.25/0.50/
1.00, -0.14/-0.16/-0.21/-0.06 per share) and xrp-updown-5m (-0.10/-0.11/-0.07).
The MONEY sample is 13 entries, which is not enough to act on and not enough to
dismiss: this repo already retracted one per-family carve-out made on n=21.

SO MEASURE THE PART THAT HAS POWER. The money sample is small because the price
band and the floor throw almost every round away; the TRIGGER fires on a few
hundred rounds per family. If the trigger's chosen side is right ~63% of the time
on btc and ~50% on eth, then the signal itself does not transfer to eth and that
is a mechanism. If it is 63% on eth too, then the losses are the PRICE (the ask
was high) or the selection (which of the many crossings the band let through),
and cutting the family would be fitting noise.

WHAT IT PRINTS, per family, over EVERY settled PM-era round:
  * n_trig      rounds where the trigger picks a side inside tau [5,60]
  * hit         how often that side IS the venue's winner -- the signal's power
  * be          the break-even: the mean ask the entry side was quoted at, plus
                the taker fee -- what the market thought
  * edge        hit - be, in pp. Positive = the trigger knows something the
                book does not; ~0 = the book already prices it (the repo's
                `pm_accuracy_ceiling` finding); negative = the trigger is WRONG
                on this family
  * halves      the same edge on each half of the period (the parameter test)
  * by day      so one bad session cannot carry it
  * margin      the |fc-level| bucket, since the floor is the knob in question

No price band and no floor here on purpose: those are the things being tested.
Usage (VPS, analysis venv):
    /root/analysis-venv/bin/python tools/pm_family_signal_check.py
    /root/analysis-venv/bin/python tools/pm_family_signal_check.py --signal spot
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/BinancePrediction")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from btcpredict.main import crossed_side  # noqa: E402
from pm_cross_replay import (FEE, family_of, load,  # noqa: E402
                             precompute, shipped_hold_s, shipped_look_s)


def trigger(pre: dict, signal: str, look_s: float, tau_lo: float,
            tau_hi: float, hold_s: float = 0.0) -> dict | None:
    """The FIRST crossing in the window, with the price the book quoted on that
    side. No band, no floor: those are what is being tested."""
    r, level = pre["r"], pre["level"]
    src, src_ts = ((pre["fc"], pre["fc_ts"]) if signal == "fc"
                   else (pre["tape"], pre["tape_ts"]))
    if signal == "fc" and not src:
        return None
    rows = pre["rows"]
    for i, (ts, tau, up_ask, dn_ask) in enumerate(
            zip(rows.ts_ms, rows.tau_s, rows.up_ask, rows.down_ask)):
        if not (tau_lo <= tau <= tau_hi):
            continue
        j = int(np.searchsorted(src_ts, int(ts), side="right"))
        if j < 4:
            continue
        side = crossed_side(src[max(0, j - max(10, int((hold_s + look_s + 2.0)
                                                      * 1.6) + 6)):j],
                            level, look_s, hold_s=hold_s)
        if side is None:
            continue
        sig_px = src[j - 1][1]
        ask = float(up_ask if side == "Up" else dn_ask)
        return dict(family=family_of(r.slug), slug=r.slug, side=side, ask=ask,
                    won=bool((r.winner == "Up") if side == "Up"
                             else (r.winner == "Down")),
                    margin=1e4 * (sig_px - level) / level, tau=float(tau),
                    day=dt.datetime.fromtimestamp(ts / 1000, dt.UTC)
                    .strftime("%m-%d"))
    return None


def report(label: str, d: pd.DataFrame) -> None:
    if d.empty:
        print(f"   {label:<18} n=  0")
        return
    n = len(d)
    hit = d.won.mean()
    # only rounds where the book quoted the taken side can be priced at all
    priced = d[d.ask > 0.0]
    be = (priced.ask + FEE(priced.ask)).mean() if len(priced) else float("nan")
    per = (priced.won.astype(float) - priced.ask - FEE(priced.ask)) if len(priced) \
        else pd.Series(dtype=float)
    h = n // 2
    edge = 100 * (hit - be) if be == be else float("nan")
    h1 = 100 * (d.won[:h].mean() - be) if be == be and h else float("nan")
    h2 = 100 * (d.won[h:].mean() - be) if be == be else float("nan")
    t = (per.mean() / (per.std(ddof=1) / math.sqrt(len(per)))
         if len(per) > 1 and per.std(ddof=1) else 0.0)
    print(f"   {label:<18} n={n:4d} hit={hit:5.1%} be-ask={be:5.3f} "
          f"edge={edge:+5.1f}pp halves={h1:+5.1f}/{h2:+5.1f} "
          f"per-share={per.mean() if len(per) else 0:+.4f} t={t:+5.2f} "
          f"med|m|={d.margin.abs().median():.2f}bps")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default="/root/BinancePrediction/data/"
                                             "btcpredict.db")
    ap.add_argument("--signal", default="fc", choices=["fc", "spot"])
    ap.add_argument("--look-s", type=float, default=None,
                    help="default: the SHIPPED cross_look_s")
    ap.add_argument("--hold-s", type=float, default=None,
                    help="default: the SHIPPED cross_hold_s (0 = off)")
    ap.add_argument("--tau-lo", type=float, default=5.0)
    ap.add_argument("--tau-hi", type=float, default=60.0)
    ap.add_argument("--control", nargs="*", default=["btc-updown-5m", "sol-updown-5m"],
                    help="families to print as controls")
    args = ap.parse_args()
    if args.look_s is None:
        args.look_s = shipped_look_s()
    if args.hold_s is None:
        args.hold_s = shipped_hold_s()

    r, q = load(args.db)
    if r.empty:
        print("no PM-era settled rounds")
        return
    q = q.copy()
    q["fam"] = q.slug.map(family_of)
    print(f"POLYMARKET ERA ONLY: {len(r)} rounds, signal={args.signal}, "
          f"tau [{args.tau_lo:.0f},{args.tau_hi:.0f}], NO band, NO floor")

    got: list[dict] = []
    for i, (_, rr) in enumerate(r.iterrows()):
        pre = precompute(q, rr, args.look_s, 0.01, 0.99, 1.0, 900.0)
        if pre is None:
            continue
        g = trigger(pre, args.signal, args.look_s, args.tau_lo, args.tau_hi)
        if g:
            got.append(g)
        if i % 700 == 0:
            print(f"    ... {i}/{len(r)}", flush=True)
    d = pd.DataFrame(got)
    if d.empty:
        print("no triggers")
        return

    print(f"\n=== EVERY TRIGGER, BY FAMILY ({len(d)} triggers) ===")
    print("   edge = hit - (ask + fee): what the trigger knows that the book")
    print("   does not. ~0 = the book already prices it; <0 = the trigger is")
    print("   wrong on this family; the control is whatever btc/sol show.")
    for fam in sorted(d.family.unique()):
        report(fam, d[d.family == fam])

    for fam in ("eth-updown-5m", "xrp-updown-5m", "eth-updown-15m"):
        sub = d[d.family == fam].reset_index(drop=True)
        if sub.empty:
            continue
        print(f"\n=== {fam}: where the money comes from ===")
        print("   by day")
        for day in sorted(sub.day.unique()):
            report(f"  {day}", sub[sub.day == day])
        print("   by side")
        for side in ("Up", "Down"):
            report(f"  {side}", sub[sub.side == side])
        print("   by the margin the floor cuts on")
        buckets = [(0, 1), (1, 2), (2, 3), (3, 1e9)]
        for lo, hi in buckets:
            m = sub.margin.abs()
            lab = f"  |m| {lo}-{hi if hi < 1e9 else '+'}bps"
            report(lab, sub[(m >= lo) & (m < hi)])
        print("   by tau at the entry")
        for lo, hi in [(5, 15), (15, 30), (30, 45), (45, 61)]:
            report(f"  tau {lo}-{hi}", sub[(sub.tau >= lo) & (sub.tau < hi)])

    print("\n=== WHERE THE ENTRY PRICE KILLS IT: per-share by ASK BUCKET ===")
    print("   The arm buys up to cross_max_price = 0.65. A family whose break-even")
    print("   (be-ask above) is UNDER 0.65 is buying above break-even by design in")
    print("   that range, so this table says whether a cap inside the band helps.")
    priced = d[d.ask > 0.0].copy()
    priced["per"] = (priced.won.astype(float) - priced.ask - FEE(priced.ask))
    buckets = [(0.0, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.66)]
    for lab, sub in (("ALL", priced),
                     *[(f, priced[priced.family == f])
                       for f in sorted(priced.family.unique())]):
        parts = []
        for lo, hi in buckets:
            b = sub[(sub.ask >= lo) & (sub.ask < hi)]
            parts.append(f"{lo:.2f}-{hi:.2f}: n={len(b):3d} "
                         f"win={b.won.mean():5.1%} per={b.per.mean():+.4f}"
                         if len(b) else f"{lo:.2f}-{hi:.2f}: n=  0")
        print(f"   {lab:<18} total={sub.per.sum():+6.2f}  " + " | ".join(parts))

    print("\n=== THE CAP TEST: drop every entry above X, per family ===")
    for cap in (0.65, 0.60, 0.55, 0.50, 0.45):
        keep = priced[priced.ask < cap]
        eth = keep[keep.family == "eth-updown-5m"]
        xrp = keep[keep.family == "xrp-updown-5m"]
        print(f"   cap {cap:.2f}  pooled n={len(keep):3d} per={keep.per.mean():+.4f} "
              f"total={keep.per.sum():+6.2f} | eth n={len(eth):2d} "
              f"per={eth.per.mean() if len(eth) else 0:+.4f} | xrp n={len(xrp):2d} "
              f"per={xrp.per.mean() if len(xrp) else 0:+.4f}")

    print("\n  THE DECISION RULE THIS FEEDS: cut a family only if its edge is")
    print("  negative with the SAME sign in BOTH halves AND the control families")
    print("  are positive. Anything else is the n=13 money sample talking.")


if __name__ == "__main__":
    main()
