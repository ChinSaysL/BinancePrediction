"""Cut the CROSS replay cohort by WINDOW, not by slug, and price the caps.

WHY THIS EXISTS. `cross_max_round_stake_mult` says, in its own comment, that
"THE ROUND is one bet ... the round's CUMULATIVE stake is the risk unit, not the
order's". It accumulates on `r.slug`, and with nine families a 5-minute window
holds nine slugs, so the cap is applied nine times independently. Live
2026-09-18 11:20: four families entered the SAME window ($4 against a $2 intent)
and it settled -$2.30; 11:15 took three same-side Downs and settled -$3.12.

The earlier concentration measurement was grouped per SLUG (duplicate entries
inside one family, which `cross_max_per_round = 1` already caps). This groups by
the round's ORACLE PRINT (`rounds.end_ms`), which is the unit the comment means:
every 5m family in one slot shares `end_ms` exactly.

WHAT IT ANSWERS: on the replay cohort, (a) is the entry-level edge of a window a
function of how many entries the window holds, (b) what do the candidate caps
remove, and (c) do they survive the battery (pooled t, day-clustered t, both
halves, drop-best-day).

WHAT IT DOES NOT MODEL, same as `pm_cross_replay`: the tape pays the recorded
ask with no sweep premium and no FAK miss rate. The caps are modelled in ENTRY
COUNT, which is exactly `cross_max_round_stake_mult` when the per-entry sized
stake is constant (rmult x stake / stake = rmult entries).

Usage (analysis venv, on the VPS -- the bot venv has no numpy):
    /root/analysis-venv/bin/python tools/pm_window_cap.py
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

DB = "/root/BinancePrediction/data/btcpredict.db"


def collect(r: pd.DataFrame, q: pd.DataFrame, *, look_s: float,
            floor_bps: float, px_lo: float, px_hi: float,
            tau_lo: float, tau_hi: float) -> list[dict]:
    """One entry per round under the shipped chain, stamped with its WINDOW."""
    out: list[dict] = []
    for _, row in r.iterrows():
        pre = base.precompute(q, row, look_s, px_lo, px_hi, tau_lo, tau_hi)
        if pre is None:
            continue
        e = base.pick(pre, signal="fc", floor_bps=floor_bps, look_s=look_s,
                      px_lo=px_lo, px_hi=px_hi, tau_lo=tau_lo, tau_hi=tau_hi)
        if e is None:
            continue
        e["end_ms"] = int(row.end_ms)
        e["family"] = e["family"]
        out.append(e)
    return sorted(out, key=lambda x: x["ts_ms"])


def battery(rows: list[dict], label: str) -> dict:
    """`pm_cross_replay.batter` plus drop-best-day, which the repo's bar needs."""
    if not rows:
        print(f"    {label:<34} n=0")
        return {}
    d = pd.DataFrame(rows).sort_values("ts_ms")
    v = d.per.to_numpy()
    n = len(v)
    t = v.mean() / (v.std() / math.sqrt(n)) if v.std() else 0.0
    dm = np.array([v[d.day.values == x].mean() for x in d.day.unique()])
    day_t = (dm.mean() / (dm.std() / math.sqrt(len(dm)))) if dm.std() else 0.0
    worst = dm.min()
    drop1 = v.mean() if len(dm) < 2 else (
        np.concatenate([v[d.day.values != x] for x in
                        [d.day.value_counts().idxmax()]]).mean())
    h = n // 2
    print(f"    {label:<30} n={n:4d} per={v.mean():+.4f} tot={v.sum():+7.2f} "
          f"t={t:+5.2f} day-t={day_t:+5.2f} "
          f"halves={v[:h].mean():+.4f}/{v[h:].mean():+.4f} drop1={drop1:+.4f} "
          f"worst-day={worst:+.4f}")
    return dict(n=n, per=v.mean(), total=v.sum(), t=t, day_t=day_t,
                drop1=drop1, worst=worst)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default=DB)
    ap.add_argument("--tau-lo", type=float, default=5.0)
    ap.add_argument("--tau-hi", type=float, default=180.0)
    ap.add_argument("--px-lo", type=float, default=0.10)
    ap.add_argument("--px-hi", type=float, default=None,
                    help="default: the SHIPPED cross_max_fill_px")
    ap.add_argument("--floor-bps", type=float, default=None,
                    help="default: the SHIPPED cross_min_margin_bps")
    args = ap.parse_args()
    from btcpredict.config import settings
    if args.px_hi is None:
        args.px_hi = float(settings.cross_max_fill_px)
    if args.floor_bps is None:
        args.floor_bps = float(settings.cross_min_margin_bps)
    look_s = base.shipped_look_s()

    r, q = base.load(args.db)
    q = q.copy()
    q["fam"] = q.slug.map(base.family_of)
    print(f"POLYMARKET ERA: {len(r)} settled rounds | look_s {look_s:.1f} "
          f"floor {args.floor_bps} band [{args.px_lo:.2f},{args.px_hi:.2f}) "
          f"tau [{args.tau_lo:.0f},{args.tau_hi:.0f}]")
    ents = collect(r, q, look_s=look_s, floor_bps=args.floor_bps,
                   px_lo=args.px_lo, px_hi=args.px_hi,
                   tau_lo=args.tau_lo, tau_hi=args.tau_hi)
    print(f"entries under the shipped chain: {len(ents)}")

    byw: dict[int, list[dict]] = {}
    for e in ents:
        byw.setdefault(e["end_ms"], []).append(e)
    for k, v in byw.items():
        v.sort(key=lambda x: x["ts_ms"])
    print(f"windows (distinct oracle prints): {len(byw)}")

    print("\n=== entries per WINDOW ===")
    for k in sorted({len(v) for v in byw.values()}):
        ws = [w for w, v in byw.items() if len(v) == k]
        e = [x["per"] for w in ws for x in byw[w]]
        print(f"    k={k}  windows={len(ws):3d}  entries={len(e):4d}  "
              f"per-entry={np.mean(e):+.4f}  total={sum(e):+7.2f}")

    print("\n=== per-entry edge by how many the window held ===")
    for k in sorted({len(v) for v in byw.values()}):
        e = [x["per"] for w, v in byw.items() if len(v) == k for x in v]
        print(f"    k={k}  n={len(e):4d}  mean={np.mean(e):+.4f}  "
              f"median={np.median(e):+.4f}  worst={min(e):+.4f}")

    print("\n=== same-SIDE stacking inside one window ===")
    pos: dict[int, list[float]] = {}
    for w, v in byw.items():
        seen: dict[str, int] = {}
        for x in v:
            seen[x["side"]] = seen.get(x["side"], 0) + 1
            pos.setdefault(seen[x["side"]], []).append(x["per"])
    for p in sorted(pos):
        e = pos[p]
        print(f"    entry #{p} on its side  n={len(e):4d}  mean={np.mean(e):+.4f}")

    multi_same = [x["per"] for w, v in byw.items() if len(v) > 1
                  and len({y["side"] for y in v}) == 1 for x in v]
    multi_mixed = [x["per"] for w, v in byw.items() if len(v) > 1
                   and len({y["side"] for y in v}) > 1 for x in v]
    if multi_same:
        print(f"\n    all-one-side windows  n={len(multi_same):4d}  "
              f"mean={np.mean(multi_same):+.4f}  tot={sum(multi_same):+7.2f}")
    if multi_mixed:
        print(f"    mixed-side windows    n={len(multi_mixed):4d}  "
              f"mean={np.mean(multi_mixed):+.4f}  tot={sum(multi_mixed):+7.2f}")

    def cap_by_count(limit: int) -> tuple[list[dict], list[dict]]:
        keep, drop = [], []
        for v in byw.values():
            keep += v[:limit]
            drop += v[limit:]
        return keep, drop

    def cap_same_side() -> tuple[list[dict], list[dict]]:
        keep, drop = [], []
        for v in byw.values():
            seen: dict[str, int] = {}
            for x in v:
                seen[x["side"]] = seen.get(x["side"], 0) + 1
                (keep if seen[x["side"]] == 1 else drop).append(x)
        return keep, drop

    print("\n=== CANDIDATE CAPS (battery per variant) ===")
    battery(ents, "base (no cap)")
    for lim in (1, 2, 3):
        keep, drop = cap_by_count(lim)
        battery(keep, f"cap {lim} per window")
        if drop:
            d = [x["per"] for x in drop]
            print(f"      dropped n={len(d)} tot={sum(x['per'] for x in drop):+7.2f} "
                  f"per-entry={np.mean(d):+.4f} "
                  f"win={sum(1 for x in drop if x['won'])/len(drop):.1%}")
    keep, drop = cap_same_side()
    battery(keep, "same side <=1 per window")
    if drop:
        d = [x["per"] for x in drop]
        print(f"      dropped n={len(d)} tot={sum(x['per'] for x in drop):+7.2f} "
              f"per-entry={np.mean(d):+.4f} "
              f"win={sum(1 for x in drop if x['won'])/len(drop):.1%}")

    # THE ONLY NEGATIVE COHORT the window tables name: an entry on the OPPOSITE
    # side to a position already taken in the same window. That one IS
    # implementable before ordering (the arm holds no cross-family state today),
    # unlike "this window turned out mixed", which you cannot know in advance.
    print("\n=== cross-family REVERSAL: entering against an open same-window side ===")
    rev, same, first = [], [], []
    for v in byw.values():
        seen_sides: set[str] = set()
        for x in v:
            if not seen_sides:
                first.append(x)
            elif x["side"] in seen_sides:
                same.append(x)
            else:
                rev.append(x)
            seen_sides.add(x["side"])
    for name, coh in (("first entry of the window", first),
                      ("same side as an open one", same),
                      ("OPPOSITE side to an open one", rev)):
        e = [x["per"] for x in coh]
        print(f"    {name:<30} n={len(e):4d} mean={np.mean(e):+.4f} "
              f"tot={sum(e):+7.2f} win={sum(1 for x in coh if x['won'])/len(coh):5.1%}")
    if rev:
        ids = {id(x) for x in rev}
        battery([x for x in ents if id(x) not in ids], "skip the reversal cohort")

    print("\n=== the windows the caps touch ===")
    for w, v in sorted(byw.items()):
        if len(v) < 2:
            continue
        tot = sum(x["per"] for x in v)
        print(f"    {pd.to_datetime(w, unit='ms', utc=True).strftime('%m-%d %H:%M')} "
              f"net={tot:+6.2f} k={len(v)}  " +
              " ".join(f"{x['family'].split('-')[0]}/{x['side'][0]}{x['per']:+.2f}"
                       for x in v))


if __name__ == "__main__":
    main()
