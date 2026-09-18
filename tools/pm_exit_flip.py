"""EXIT-ON-FLIP: what would leaving when the entry's own forecast turns cost?

THE IDEA (operator, 2026-09-18): we entered because the forecast said Up. If the
forecast later says Down, the reason we bought is gone -- so sell and take the
money out rather than hold a position whose thesis died.

WHY THIS IS A COUNTERFACTUAL, NOT A RULE, ON THIS VENUE:
  * every order path in the bot passes `side="BUY"` (broker.py 579/1004/1617),
    and the earlier unwind path that DID send sells came back with 37
    `no SELL quote` errors before it was removed as dead code (FINDINGS, the
    2026-09-11 audit). `config.py` records "this venue has no sell, only buy".
  * the only buy-side substitute is to buy the OTHER side, which pays exactly
    1.000 at the bell whatever the oracle prints -- so at the prices this arm
    pays (both asks sum to >1 by the spread) it locks a loss by arithmetic.
So the numbers below price the DECISION, not an order the bot can place today.

THE EXIT PRICE. The two outcome tokens are complementary: holding Up and buying
Down pays 1.000, so the Up BID the venue would pay is `1 - down_ask`. Selling at
that price is what this models, less the same per-share taker fee the entry pays
(`0.07 p (1-p)`, the repo's convention).

THE TEST THAT DECIDES IT. Exiting always realises the spread: a 0.57 entry exits
near 0.43, i.e. -0.15/share, on EVERY exit. So the flip only pays if it is
informative -- if P(win | flipped) is far enough below P(win) to cover that. Both
numbers are printed, and the battery runs on the exit variant, not on the idea.

Usage (analysis venv, on the VPS):
    /root/analysis-venv/bin/python tools/pm_exit_flip.py
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_cross_replay as base  # noqa: E402

FEE = base.FEE


def walk_legacy(pre: dict, entry: dict, *, confirm_s: float, look_s: float,
                tau_min: float, hold_s: float = 0.0,
                floor_bps: float = 0.0) -> dict | None:
    """THE RULE AS IT WAS MEASURED ON 2026-09-18, kept only for the A/B.

    It has three defects, all of which flatter the exit, and it is retained so
    the corrected numbers can be shown against the ones that shipped:

    1. `cand` LATCHES ON THE FIRST TICK AFTER THE ENTRY, not on the first
       flipped reading -- the `if side != other: if cand is None` branch -- and
       is never advanced. So the exit is booked at the book roughly one second
       after the entry, i.e. at ~the entry price, no matter when it actually
       fires. That is the whole of the reported edge: the measured
       "entry -> booked-price lag" is median 1.0 s with p10 = p90 = 1.0 s, which
       is the tape's own spacing, not a property of the flip.
    2. It requires no `hold_s` and no margin floor, while `pm_cross_replay.pick`
       (the ENTRY) applies both -- so it scores an ungated exit against a gated
       entry.
    3. `confirm_s` therefore measures from the entry, not from the flip.
    """
    held = entry["side"]
    other = "Down" if held == "Up" else "Up"
    src_, src_ts = pre["fc"], pre["fc_ts"]
    if not src_:
        return None
    rows = pre["rows"]
    tail = max(10, int((look_s + 2.0) * 1.6) + 6)
    end_ms = int(pre["r"].end_ms)
    cand = None
    for ts, tau, up_ask, dn_ask in zip(rows.ts_ms, rows.tau_s,
                                       rows.up_ask, rows.down_ask):
        ts = int(ts)
        if ts <= entry["ts_ms"] or tau < tau_min:
            continue
        j = int(np.searchsorted(src_ts, ts, side="right"))
        if j < 4:
            continue
        side = base.crossed_side(src_[max(0, j - tail):j], pre["level"], look_s)
        if side != other:
            if cand is None:
                cand = (ts, float(tau), up_ask, dn_ask)
            continue
        if cand is None:
            cand = (ts, float(tau), up_ask, dn_ask)
        ts0, tau0, up0, dn0 = cand
        if confirm_s <= 0 or (ts - ts0) / 1000.0 >= confirm_s:
            return _px(held, ts0, tau0, up0, dn0, entry, end_ms)
    return None


def walk(pre: dict, entry: dict, *, confirm_s: float, look_s: float,
         tau_min: float, hold_s: float = 0.0,
         floor_bps: float = 0.0) -> dict | None:
    """The first tick after the entry where the SAME trigger, RUN WITH THE
    SAME GATES THE ENTRY USES, reads the other side -- priced where it fires.

    Three corrections against `walk_legacy`, which is what the 2026-09-18
    decision was taken on:

    * **PRICED AT THE FIRING TICK.** The sale happens when the rule fires, so
      that is the book it gets. The legacy version booked the tick right after
      the entry, which is why the exit appeared to bank ~the entry price on a
      cohort that only wins 42%.
    * **`hold_s` AND `floor_bps` ARE APPLIED**, exactly as `pm_cross_replay.pick`
      applies them to the entry. Without them the exit fires on a crossing the
      ENTRY would refuse -- there is no hysteresis band, and the live arm
      demonstrated the consequence: sol-updown-5m-1789743000 refused to open
      four times at 0.20 bps (floor 0.25), opened at 14:53:20, and sold 4.4 s
      later on an unmeasured reversal.
    * **`confirm_s` RUNS FROM THE FLIP**, so it means "the flip persisted",
      which is what the name and the whipsaw argument both claim.

    Intervening non-readings do NOT disarm: `crossed_side` reports a crossing
    EVENT, so the tick after a flip returns None with the signal still on that
    side. The latch is cleared only by a reading back on the held side.
    """
    held = entry["side"]
    other = "Down" if held == "Up" else "Up"
    src_, src_ts = pre["fc"], pre["fc_ts"]
    if not src_:
        return None
    rows = pre["rows"]
    tail = max(10, int((hold_s + look_s + 2.0) * 1.6) + 6)
    end_ms = int(pre["r"].end_ms)
    level = pre["level"]
    armed_ms = None
    for ts, tau, up_ask, dn_ask in zip(rows.ts_ms, rows.tau_s,
                                       rows.up_ask, rows.down_ask):
        ts = int(ts)
        if ts <= entry["ts_ms"] or tau < tau_min:
            continue
        j = int(np.searchsorted(src_ts, ts, side="right"))
        if j < 4:
            continue
        side = base.crossed_side(src_[max(0, j - tail):j], level, look_s,
                                 hold_s=hold_s)
        if side == held:
            armed_ms = None           # back on our side: the flip died
            continue
        if side != other:
            continue                  # no reading: expected, do not disarm
        # THE FLIP'S OWN MARGIN, the gate the entry applies to itself.
        if floor_bps > 0 and level:
            sig_px = src_[j - 1][1]
            if abs(1e4 * (sig_px - level) / level) < floor_bps:
                continue              # on the level, not past it
        if armed_ms is None:
            armed_ms = ts
        if confirm_s <= 0 or (ts - armed_ms) / 1000.0 >= confirm_s:
            return _px(held, ts, float(tau), up_ask, dn_ask, entry, end_ms)
    return None


def _px(held: str, ts0: int, tau0: float, up0, dn0, entry: dict,
        end_ms: int) -> dict | None:
    """Exit price from the complementary book: the bid on what we hold."""
    if held == "Up":
        opp = dn0
    else:
        opp = up0
    if not opp or not (0.0 < float(opp) < 1.0):
        return None
    px = 1.0 - float(opp)
    if not (0.0 < px < 1.0):
        return None
    return dict(px=px, ts_ms=int(ts0), tau=tau0,
                hold_s=(int(ts0) - entry["ts_ms"]) / 1000.0,
                tau_left=(end_ms - int(ts0)) / 1000.0)


def per_share(entry: dict, won: bool, exit_px: float | None) -> float:
    cost = entry["ask"] + FEE(entry["ask"])
    if exit_px is None:
        return (1.0 if won else 0.0) - cost
    return exit_px - FEE(exit_px) - cost


def battery(rows: list[dict], label: str) -> None:
    if not rows:
        print(f"    {label:<44} n=0")
        return
    d = pd.DataFrame(rows).sort_values("ts_ms")
    v = d.per.to_numpy()
    n = len(v)
    t = v.mean() / (v.std() / math.sqrt(n)) if v.std() else 0.0
    dm = np.array([v[d.day.values == x].mean() for x in d.day.unique()])
    day_t = (dm.mean() / (dm.std() / math.sqrt(len(dm)))) if dm.std() else 0.0
    h = n // 2
    drop1 = np.concatenate([v[d.day.values != x] for x in
                            [d.day.value_counts().idxmax()]]).mean()
    print(f"    {label:<44} per-share={v.mean():+.4f} tot={v.sum():+7.2f} "
          f"win={d.won.mean():5.1%} t={t:+5.2f} day-t={day_t:+5.2f} "
          f"halves={v[:h].mean():+.4f}/{v[h:].mean():+.4f} drop1={drop1:+.4f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default=base.DB)
    ap.add_argument("--tau-lo", type=float, default=5.0)
    ap.add_argument("--tau-hi", type=float, default=180.0)
    ap.add_argument("--px-lo", type=float, default=0.10)
    ap.add_argument("--px-hi", type=float, default=None)
    ap.add_argument("--floor-bps", type=float, default=None)
    ap.add_argument("--tau-min", type=float, default=8.0,
                    help="never exit inside this many seconds of the bell "
                         "(the tape is one-sided there; the bid is gone)")
    args = ap.parse_args()
    from btcpredict.config import settings
    if args.px_hi is None:
        args.px_hi = float(settings.cross_max_fill_px)
    if args.floor_bps is None:
        args.floor_bps = float(settings.cross_min_margin_bps)
    look_s = base.shipped_look_s()
    hold_s = base.shipped_hold_s()

    r, q = base.load(args.db)
    q = q.copy()
    q["fam"] = q.slug.map(base.family_of)
    entries: list[tuple[dict, dict]] = []
    for _, row in r.iterrows():
        pre = base.precompute(q, row, look_s, args.px_lo, args.px_hi,
                              args.tau_lo, args.tau_hi)
        if pre is None:
            continue
        e = base.pick(pre, signal="fc", floor_bps=args.floor_bps, look_s=look_s,
                      px_lo=args.px_lo, px_hi=args.px_hi,
                      tau_lo=args.tau_lo, tau_hi=args.tau_hi)
        if e is not None:
            entries.append((pre, e))
    print(f"POLYMARKET ERA, shipped chain: {len(entries)} entries | look_s "
          f"{look_s:.1f} floor {args.floor_bps} tau "
          f"[{args.tau_lo:.0f},{args.tau_hi:.0f}] | exit fee {FEE(0.5)*100:.2f}c "
          f"at 0.50")

    # -- hold as the baseline, and the raw flip statistics -----------------
    flipped, notflipped = [], []
    for pre, e in entries:
        f = walk(pre, e, confirm_s=0.0, look_s=look_s, tau_min=args.tau_min,
                 hold_s=hold_s, floor_bps=args.floor_bps)
        (flipped if f else notflipped).append((pre, e, f))
    print(f"\n=== does it flip? ===")
    print(f"    entries whose signal turned against them before the bell: "
          f"{len(flipped)}/{len(entries)} ({len(flipped)/len(entries):.1%})")
    if flipped:
        hs = [f["hold_s"] for _, _, f in flipped]
        tl = [f["tau_left"] for _, _, f in flipped]
        print(f"    seconds from entry to the flip: median {np.median(hs):.1f} "
              f"mean {np.mean(hs):.1f} p10 {np.percentile(hs,10):.1f} "
              f"p90 {np.percentile(hs,90):.1f}")
        print(f"    seconds of round LEFT at the flip: median "
              f"{np.median(tl):.1f} p10 {np.percentile(tl,10):.1f}")
        print(f"    exit price implied: median {np.median([f['px'] for _,_,f in flipped]):.4f}")
        wf = sum(1 for _, e, _ in flipped if e["won"]) / len(flipped)
        wn = sum(1 for _, e, _ in notflipped if e["won"]) / max(1, len(notflipped))
        print(f"    P(win | flipped)  {wf:5.1%}   vs  P(win | not flipped) {wn:5.1%}")
        print(f"    (holding pays 1.0 with p={wf:.3f} and {wn:.3f} respectively; "
              f"exiting banks ~{np.median([f['px'] for _,_,f in flipped]):.2f} now)")

    # -- variants ---------------------------------------------------------
    print("\n=== variants (per share; entry pays ask+fee, exit pays "
          "1-other_ask-fee) ===")
    base_rows = [dict(ts_ms=e["ts_ms"], day=e["day"], won=e["won"],
                      per=per_share(e, e["won"], None)) for _, e in entries]
    battery(base_rows, "A hold to settlement (base)")

    def run(label: str, keep_fn) -> None:
        rows, n_exit = [], 0
        for pre, e, f in flipped:
            px = f["px"] if (f and keep_fn(f, e)) else None
            if px is not None:
                n_exit += 1
            rows.append(dict(ts_ms=e["ts_ms"], day=e["day"], won=e["won"],
                             per=per_share(e, e["won"], px)))
        for _, e, _ in notflipped:
            rows.append(dict(ts_ms=e["ts_ms"], day=e["day"], won=e["won"],
                             per=per_share(e, e["won"], None)))
        battery(rows, f"{label} [{n_exit} exits]")

    run("B exit at the first flip", lambda f, e: True)
    for c in (3.0, 5.0, 10.0):
        rows_probe = []
        n = 0
        for pre, e in entries:
            f = walk(pre, e, confirm_s=c, look_s=look_s, tau_min=args.tau_min,
                     hold_s=hold_s, floor_bps=args.floor_bps)
            if f:
                n += 1
            rows_probe.append((e, f))
        rows = [dict(ts_ms=e["ts_ms"], day=e["day"], won=e["won"],
                     per=per_share(e, e["won"], (f["px"] if f else None)))
                for e, f in rows_probe]
        battery(rows, f"C exit if the flip holds {c:.0f}s [{n} exits]")
    # -- THE CORRECTION, SHOWN AGAINST WHAT SHIPPED ------------------------
    # Every number in `config.cross_exit_enabled` and in FINDINGS came from
    # `walk_legacy`. This block is the A/B that retires them.
    print("\n=== \u26d4 THE RULE THAT SHIPPED vs THE CORRECTED ONE ===")
    for c in (3.0,):
        for name, fn, kw in (
                ("legacy (booked at entry+1s, ungated)", walk_legacy,
                 dict(hold_s=0.0, floor_bps=0.0)),
                ("corrected (priced at the fire, gated)", walk,
                 dict(hold_s=hold_s, floor_bps=args.floor_bps))):
            rows, n, lags = [], 0, []
            for pre, e in entries:
                f = fn(pre, e, confirm_s=c, look_s=look_s,
                       tau_min=args.tau_min, **kw)
                if f:
                    n += 1
                    lags.append(f["hold_s"])
                rows.append(dict(ts_ms=e["ts_ms"], day=e["day"], won=e["won"],
                                 per=per_share(e, e["won"],
                                               f["px"] if f else None)))
            battery(rows, f"{name} confirm {c:.0f}s [{n} exits]")
            if lags:
                print(f"        entry -> exit lag: median {np.median(lags):6.1f}s "
                      f"p10 {np.percentile(lags,10):5.1f} "
                      f"p90 {np.percentile(lags,90):6.1f}")
    run("D exit only when the exit price is under the entry",
        lambda f, e: f["px"] < e["ask"])
    run("E exit only if the flip lands before tau 60",
        lambda f, e: f["tau"] <= 60.0)
    run("F exit only if the flip lands before tau 120",
        lambda f, e: f["tau"] <= 120.0)

    # -- IS THIS JUST THE MARGINAL-CROSSING COHORT? ------------------------
    # The flip arrives ONE SECOND after the entry at p10=p90=1.0, and the
    # shipped floor admits a crossing worth 0.25 bps -- which is noise. If the
    # flip is that cohort, then the SELL-FREE version of this idea is the floor,
    # which the execution path already has and which the venue cannot refuse.
    print("\n=== flip rate and value by the margin the entry was taken on ===")
    buckets = [(0.25, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 1e9)]
    for lo, hi in buckets:
        sel = [(pre, e) for pre, e in entries if lo <= abs(e["margin"]) < hi]
        if not sel:
            continue
        fl = sum(1 for pre, e in sel
                 if walk(pre, e, confirm_s=0.0, look_s=look_s,
                         tau_min=args.tau_min, hold_s=hold_s,
                         floor_bps=args.floor_bps))
        hold = np.mean([per_share(e, e["won"], None) for _, e in sel])
        wf = [e["won"] for pre, e in sel
              if walk(pre, e, confirm_s=0.0, look_s=look_s, tau_min=args.tau_min,
                 hold_s=hold_s, floor_bps=args.floor_bps)]
        print(f"    |m| {lo:4.2f}-{hi if hi < 1e8 else 99:>4.2f} bps  n={len(sel):4d}  "
              f"flip {fl/len(sel):5.1%}  hold {hold:+.4f}/sh  "
              f"P(win|flip) {np.mean(wf) if wf else float('nan'):5.1%}")

    print("\n=== does the exit still pay AFTER the floor removes the marginals? ===")
    for flo in (0.5, 1.0, 2.0):
        for confirm in (0.0, 3.0):
            rows, n_exit = [], 0
            for pre, e in entries:
                if abs(e["margin"]) < flo:
                    continue
                f = walk(pre, e, confirm_s=confirm, look_s=look_s,
                         hold_s=hold_s, floor_bps=flo,
                         tau_min=args.tau_min)
                px = f["px"] if f else None
                if px is not None:
                    n_exit += 1
                rows.append(dict(ts_ms=e["ts_ms"], day=e["day"], won=e["won"],
                                 per=per_share(e, e["won"], px)))
            battery(rows, f"floor {flo:.1f}bps + exit(confirm {confirm:.0f}s) "
                          f"[{n_exit} exits]")


if __name__ == "__main__":
    main()
