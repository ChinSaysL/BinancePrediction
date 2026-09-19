"""Search the WHOLE cross config jointly, under the full live gate chain.

One-at-a-time sweeps hold every other knob at the shipped value, so they can
only find the best neighbour of the current cell -- and the current cell was
itself chosen that way. This searches the grid.

THE SELECTION RULE IS PRE-REGISTERED, because 1,700 cells on a four-day tape
will hand you a winner whether or not one exists:

  1. Score every cell on the TRAIN days at look_s 1.0 AND at the 2.0 the arm
     does not trade.
  2. Rank by MIN(train_L1, train_L2). Taking the minimum puts look-stability
     INSIDE the selection instead of checking it afterwards, so a cell that
     wins only because the crossing rule was nudged cannot be picked at all.
  3. The winner must then beat the shipped config on the TEST days at BOTH
     looks. If it does not, SHIP NOTHING -- that is a real outcome, not a
     failure to find one.

The exposure cap is held at its live value: it is a risk limit, not an edge
knob, and its level was chosen by the operator against ruin, which no
expected-dollar search can price.

    python3 tools/pm_joint_chain.py --jobs 10
"""
from __future__ import annotations

import argparse, datetime as dt, itertools, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pm_full_chain import LIVE, build, sim  # noqa: E402

GRID = dict(
    floor=[0.25, 0.50, 0.75, 1.00],
    ceil=[0.0, 1.5, 2.5],
    px_lo=[0.35, 0.40, 0.45, 0.50],
    px_hi=[0.60, 0.65, 0.70],
    tau_lo=[5.0, 30.0, 60.0],
    tau_hi=[120.0, 180.0, 240.0, 300.0],
)
KEYS = list(GRID)


def fmt(c):
    d = dict(zip(KEYS, c))
    return ("floor %.2f ceil %-3s px[%.2f,%.2f] tau[%.0f,%.0f]"
            % (d["floor"], "off" if d["ceil"] == 0 else "%.1f" % d["ceil"],
               d["px_lo"], d["px_hi"], d["tau_lo"], d["tau_hi"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=10)
    ap.add_argument("--bankroll", type=float, default=5.11)
    a = ap.parse_args()
    B, E = a.bankroll, LIVE["cross_max_open_stake_pct"]
    SHIP = (LIVE["cross_min_margin_bps"], LIVE["cross_max_margin_bps"],
            LIVE["cross_min_price"], LIVE["cross_max_price"],
            LIVE["cross_min_tau_s"], LIVE["cross_max_tau_s"])
    combos = list(itertools.product(*(GRID[k] for k in KEYS)))
    R, TR, TE = {}, {}, {}
    for look in (LIVE["cross_look_s"], 2.0):
        R[look] = build(look, LIVE["cross_hold_s"], a.jobs)
        days = sorted({dt.datetime.fromtimestamp(c[0] / 1000, dt.UTC).strftime("%m-%d")
                       for _, cs in R[look] for c in cs})
        k = len(days) // 2
        TR[look], TE[look] = set(days[:k]), set(days[k:])
    L1, L2 = LIVE["cross_look_s"], 2.0
    print(f"cells: {len(combos)}   train {sorted(TR[L1])}   test {sorted(TE[L1])}")
    print(f"bankroll ${B:.2f}  exposure {E} (held: risk limit, not an edge knob)\n")

    def score(c, look, days):
        g = dict(zip(KEYS, c))
        return sim(R[look], bankroll=B, expo=E, fill_cap=g["px_hi"], days=days,
                   depth=True, fresh=True, **g)

    rows = []
    for c in combos:
        a1, a2 = score(c, L1, TR[L1]), score(c, L2, TR[L2])
        if not a1 or not a2:
            continue
        rows.append((min(a1["d"], a2["d"]), c, a1, a2))
    rows.sort(reverse=True, key=lambda x: x[0])
    print(f"cells with >=15 fills on both looks' train half: {len(rows)}\n")
    print("=== TOP 10 BY THE PRE-REGISTERED RULE: min(train L1, train L2) ===")
    print("  %-46s %-24s %s" % ("config", "TRAIN L1 / L2", "TEST L1 / L2"))
    for m, c, a1, a2 in rows[:10]:
        t1, t2 = score(c, L1, TE[L1]), score(c, L2, TE[L2])
        print("  %-46s $%+6.2f/$%+6.2f      %s"
              % (fmt(c), a1["d"], a2["d"],
                 ("$%+6.2f/$%+6.2f  win %.1f%%/%.1f%%  t%+.2f/%+.2f"
                  % (t1["d"], t2["d"], t1["win"], t2["win"], t1["t"], t2["t"]))
                 if t1 and t2 else "n<15"))

    s1, s2 = score(SHIP, L1, TE[L1]), score(SHIP, L2, TE[L2])
    print("\n=== SHIPPED, on the same test days ===")
    print("  %-46s TEST $%+6.2f/$%+6.2f  win %.1f%%/%.1f%%  t%+.2f/%+.2f"
          % (fmt(SHIP), s1["d"], s2["d"], s1["win"], s2["win"], s1["t"], s2["t"]))

    win = rows[0][1]
    w1, w2 = score(win, L1, TE[L1]), score(win, L2, TE[L2])
    print("\n=== THE VERDICT ===")
    print("  train-selected: %s" % fmt(win))
    if not (w1 and w2):
        print("  -> too few fills out of sample. SHIP NOTHING."); return
    better = w1["d"] > s1["d"] and w2["d"] > s2["d"]
    print("  beats shipped on TEST at look 1.0: $%+.2f vs $%+.2f  %s"
          % (w1["d"], s1["d"], "YES" if w1["d"] > s1["d"] else "no"))
    print("  beats shipped on TEST at look 2.0: $%+.2f vs $%+.2f  %s"
          % (w2["d"], s2["d"], "YES" if w2["d"] > s2["d"] else "no"))
    print("  -> %s" % ("SHIP IT" if better else
                       "SHIP NOTHING (the rule was set before the run)"))
    if better:
        print("\n  full-tape check (train+test, both looks):")
        for look in (L1, L2):
            f = score(win, look, None); sh = score(SHIP, look, None)
            print("    look %.1f  new n=%4d win %.1f%% $%+7.2f t%+.2f day-t%+.2f"
                  "   | shipped n=%4d win %.1f%% $%+7.2f t%+.2f"
                  % (look, f["n"], f["win"], f["d"], f["t"], f["dayt"],
                     sh["n"], sh["win"], sh["d"], sh["t"]))
        print("\n  env to apply:")
        g = dict(zip(KEYS, win))
        for k, v in (("CROSS_MIN_MARGIN_BPS", g["floor"]),
                     ("CROSS_MAX_MARGIN_BPS", g["ceil"]),
                     ("CROSS_MIN_PRICE", g["px_lo"]),
                     ("cross_max_price", g["px_hi"]),
                     ("CROSS_MAX_FILL_PX", g["px_hi"]),
                     ("CROSS_MIN_TAU_S", g["tau_lo"]),
                     ("CROSS_MAX_TAU_S", g["tau_hi"])):
            print(f"    {k}={v}")


if __name__ == "__main__":
    main()
