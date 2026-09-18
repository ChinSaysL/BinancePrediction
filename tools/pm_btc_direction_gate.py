"""SHOULD AN ALT BE ALLOWED TO TRADE AGAINST BTC? Price the cross-asset gate.

THE OBSERVATION (operator, 2026-09-18). One 5-minute window, three fills:

    family  side   level        spot@entry   entry-vs-level   mean@bell   settled
    btc     Down   77808.0245   77804.0650      -0.51 bps      -2.42 bps     Down  WON
    sol     Up     105.9225     105.9550        +3.07 bps      -7.80 bps     Down  lost
    xrp     Up     1.3301       1.3302          +1.25 bps      -5.18 bps     Down  lost

BTC was marginally under its own level; SOL and XRP were 1-3 bps OVER theirs, so
the arm bought them Up while the complex turned down and BTC's Down was right.
The question is whether that is a repeatable structure or three correlated draws.

HOW IT IS PRICED. For every entry the SHIPPED rule chain takes (`pick`: signal
fc, the 0.25 bps floor, band 0.10-0.65, tau 5-180, one per round) on the ALT
families, look up BTC's state at that same instant IN THE ROUND WITH THE SAME
START -- `state = 1e4*(btc_spot - btc_level)/btc_level`, BTC's own distance past
the level it is paid against -- and split the entry by whether its side AGREES
with that sign. A deadband sweep is included because a 0.5 bps state is noise,
and the increment is scored on its own, because what a gate REMOVES is a
different question from what it leaves.

CAVEATS THAT BOUND ANY RESULT HERE. The alt entries inside one 5-minute window
are not independent draws: every family settles on its own oracle print but the
underlying is one complex (the daily EV series of these rules are 73%
correlated, and `cross_max_open_stake_pct` exists because the ACCOUNT treats
same-side same-round positions as 0.945-correlated). A gate that looks good
because it removes the entries of one bad day is the same trap as every other
"gate" this repo has rejected -- which is why the battery below is the same four
checks, plus drop-best-day.

Usage (VPS, analysis venv):
    /root/analysis-venv/bin/python tools/pm_btc_direction_gate.py
    /root/analysis-venv/bin/python tools/pm_btc_direction_gate.py --by-family
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import OrderedDict

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/BinancePrediction")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pm_cross_replay import family_of, load, pick, precompute  # noqa: E402
from pm_tau_window import line, stat                            # noqa: E402

DEADBANDS = (0.0, 0.5, 1.0, 2.0, 3.0, 5.0)
BTC_PREFIX = {"5m": "btc-updown-5m", "15m": "btc-updown-15m"}


def btc_state(pre_btc: dict | None, ts_ms: int, mode: str = "level",
              look_s: float = 60.0) -> float | None:
    """BTC's direction at `ts_ms`, in bps: `level` = its distance past the level
    it is paid against; `mom60` = its return over the last `look_s` seconds (the
    operator's reading of "BTC went down")."""
    if not pre_btc:
        return None
    tape = pre_btc.get("tape") or []
    if mode == "mom60":
        why = [p for t, p in tape if t <= int(ts_ms)]
        back = [p for t, p in tape if t <= int(ts_ms) - int(look_s * 1000)]
        if not why or not back:
            return None
        return 1e4 * (why[-1] - back[-1]) / back[-1]
    level = pre_btc.get("level")
    if not level:
        return None
    up_to = [p for t, p in tape if t <= int(ts_ms)]
    if not up_to:
        return None
    return 1e4 * (up_to[-1] - level) / level


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?",
                    default="/root/BinancePrediction/data/btcpredict.db")
    ap.add_argument("--look-s", type=float, default=None)
    ap.add_argument("--floor-bps", type=float, default=0.25)
    ap.add_argument("--px-lo", type=float, default=0.10)
    ap.add_argument("--px-hi", type=float, default=0.65)
    ap.add_argument("--tau-lo", type=float, default=5.0)
    ap.add_argument("--tau-hi", type=float, default=180.0)
    ap.add_argument("--btc-15m", action="store_true",
                    help="use btc-updown-15m for the 15m alts (default: the "
                         "15m alt is compared against the btc-15m round)")
    ap.add_argument("--by-family", action="store_true")
    args = ap.parse_args()
    if args.look_s is None:
        from pm_cross_replay import shipped_look_s
        args.look_s = shipped_look_s()

    r, q = load(args.db)
    if r.empty:
        print("no PM-era settled rounds")
        return
    q = q.copy()
    q["fam"] = q.slug.map(family_of)

    # EVERY round gets a precompute: the alts need their own level, and BTC's
    # needs its level + tape for the state lookup.
    pre_by_key: dict[tuple[str, int], dict] = OrderedDict()
    for _, rr in r.iterrows():
        pre = precompute(q, rr, args.look_s, 0.01, 0.99, 1.0, 900.0)
        if pre is not None:
            pre_by_key[(family_of(rr.slug), int(rr.start_ms))] = pre

    rows: list[dict] = []
    for (fam, start), pre in pre_by_key.items():
        if fam.startswith("btc"):
            continue
        got = pick(pre, signal="fc", floor_bps=args.floor_bps,
                   look_s=args.look_s, px_lo=args.px_lo, px_hi=args.px_hi,
                   tau_lo=args.tau_lo, tau_hi=args.tau_hi)
        if not got:
            continue
        window = "15m" if fam.endswith("15m") else "5m"
        pbtc = pre_by_key.get((BTC_PREFIX[window], start))
        st = btc_state(pbtc, got["ts_ms"], "level")
        mo = btc_state(pbtc, got["ts_ms"], "mom60")
        if st is None or mo is None:
            continue
        got = dict(got)
        got["btc_state"] = st
        got["btc_mom"] = mo
        got["start"] = start
        got["window"] = window
        # AGREE = the side we bought matches the sign of BTC's own state.
        got["agree"] = (got["side"] == "Up") == (st > 0)
        got["agree_mom"] = (got["side"] == "Up") == (mo > 0)
        rows.append(got)
    if not rows:
        print("no alt entries with a BTC counterpart")
        return
    d = pd.DataFrame(rows)
    fams = ", ".join(sorted(d.family.unique()))
    print(f"POLYMARKET ERA: {len(d)} ALT entries with a BTC round at the same "
          f"start ({fams}), tau [{args.tau_lo:.0f},{args.tau_hi:.0f}]")
    print(f"BTC state = the alt entry's instant, BTC's spot vs BTC's own level")
    print(f"  agree   n={int(d.agree.sum()):4d}   "
          f"disagree n={int((~d.agree).sum()):4d}")

    print("\n=== THE SPLIT: BTC STATE (its distance past its own level) ===")
    line("agrees with BTC", stat(d[d.agree].to_dict("records")))
    line("AGAINST BTC", stat(d[~d.agree].to_dict("records")))
    line("all", stat(d.to_dict("records")))

    print("\n=== THE SPLIT: BTC MOMENTUM (its 60 s return) ===")
    line("agrees with BTC", stat(d[d.agree_mom].to_dict("records")))
    line("AGAINST BTC", stat(d[~d.agree_mom].to_dict("records")))
    mo_only = d[~d.agree_mom]
    if len(mo_only):
        print(f"    and the subset that is against BOTH readings "
              f"n={len(d[~d.agree & ~d.agree_mom])} "
              f"per-share={d[~d.agree & ~d.agree_mom].per.mean():+.4f}")

    for col, name in (("btc_state", "STATE vs its level"),
                      ("btc_mom", "MOMENTUM (60 s)")):
        print(f"\n=== DEADBAND on {name}: only count |bps| beyond delta ===")
        print("  delta   agrees-with-BTC        |  AGAINST-BTC cohort")
        for dl in DEADBANDS:
            live = d[d[col].abs() >= dl]
            if live.empty:
                continue
            ag = stat(live[live.agree if col == "btc_state"
                           else live.agree_mom].to_dict("records"))
            no = stat(live[~live.agree if col == "btc_state"
                           else ~live.agree_mom].to_dict("records"))
            a = (f"n={ag['n']:4d} {ag['per']:+.4f} t={ag['t']:+.2f}"
                 if ag else "n=   0")
            b = (f"n={no['n']:4d} {no['per']:+.4f} t={no['t']:+.2f} "
                 f"day-t={no['day_t']:+.2f} "
                 f"halves={no['h1']:+.3f}/{no['h2']:+.3f} "
                 f"drop={no['drop']:+.3f}") if no else "n=   0"
            print(f"  {dl:4.1f}   {a}   |  {b}")

    base = stat(d.to_dict("records"))
    gate = stat(d[d.agree].to_dict("records"))
    if base and gate:
        print(f"\n=== THE INCREMENT: what the gate REMOVES ===")
        print(f"  kept   n={gate['n']:4d} per-share={gate['per']:+.4f} "
              f"total={gate['total']:+.2f} t={gate['t']:+.2f} "
              f"day-t={gate['day_t']:+.2f} halves={gate['h1']:+.4f}/"
              f"{gate['h2']:+.4f} drop-best={gate['drop']:+.4f}")
        rem = d[~d.agree]
        print(f"  removed n={len(rem):4d} per-share={rem.per.mean():+.4f} "
              f"total={rem.per.sum():+.2f}  "
              f"(a gate is only worth it if THIS number is negative)")

    if args.by_family:
        print("\n=== PER FAMILY: agrees / against ===")
        for fam in sorted(d.family.unique()):
            sub = d[d.family == fam]
            line(f"  {fam} with ", stat(sub[sub.agree].to_dict("records")))
            line(f"  {fam} vs BTC", stat(sub[~sub.agree].to_dict("records")))

    # ---- ROUND CONCENTRATION --------------------------------------------
    # The cap is per-FAMILY, so one 5-minute window can spend several of the
    # account's slots on ONE correlated bet. `1789716300` did exactly that:
    # btc Down (+0.854) with sol Up (-1.029) and xrp Up (-1.034), round net
    # -1.209, against -0.03 if only the BTC leg had been taken.
    print("\n=== ROUND CONCENTRATION: k = entries taken in the SAME window ===")
    print("  k   rounds   per-round EV   per-round total   per-entry EV   win")
    per_round = d.copy()
    per_round["round"] = (per_round.window.astype(str) + "|"
                          + per_round.start.astype(str))
    g = per_round.groupby("round")
    for k in sorted(g.size().unique()):
        rr = g.filter(lambda x: len(x) == k)
        rsum = rr.groupby("round").per.sum()
        print(f"  {k:2d}   {len(rsum):6d}   {rsum.mean():+12.4f}   "
              f"{rsum.sum():+14.2f}   {rr.per.mean():+12.4f}   "
              f"{rr.won.mean():5.1%}")
    multi = per_round[per_round.groupby("round")["per"].transform("size") > 1]
    if len(multi):
        ms = multi.groupby("round").per.sum()
        print(f"\n  rounds with k>=2: {len(ms)}  per-round mean {ms.mean():+.4f} "
              f"total {ms.sum():+.2f} "
              f"(vs {len(d) - len(multi)} single-entry entries worth "
              f"{per_round[per_round.groupby('round')['per'].transform('size') == 1].per.sum():+.2f})")
        h = len(ms) // 2
        print(f"  their halves: {ms.iloc[:h].mean():+.4f}/{ms.iloc[h:].mean():+.4f}  "
              f"days: {ms.groupby(multi.groupby('round').day.first()).mean().round(3).to_dict()}")

    print("\n  HOW TO READ: the alt entries inside one window are ONE complex --")
    print("  they settle on separate prints but they are not independent draws.")
    print("  A pass needs pooled t>=2 AND day-t>=2 AND both halves positive AND")
    print("  drop-best positive on the KEPT cohort, and the REMOVED cohort has to")
    print("  be negative for the gate to be doing anything but fitting a day.")


if __name__ == "__main__":
    main()
