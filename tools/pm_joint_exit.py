"""THE FULL SWEEP: entry gates AND exit rule, searched together.

The entry config was chosen with the exit OFF, and the exit rules were compared
at the shipped entry. Neither is safe alone: an exit frees its slot, so it
changes what a wider tau window is worth, and a wider window changes how often
the exit has something to leave. This searches them jointly.

SELECTION IS PRE-REGISTERED, as in pm_joint_chain: rank by MIN(train look 1.0,
train look 2.0), which puts look-stability inside the choice, then require the
winner to beat what ships on the HELD-OUT days at BOTH looks.

    python3 tools/pm_joint_exit.py --jobs 10
"""
from __future__ import annotations

import argparse, datetime as dt, itertools, os, sys
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tools"))
from pm_full_chain import LIVE  # noqa
from pm_exit_chain import build  # noqa
from pm_exit_models import sim  # noqa

GRID = dict(
    floor=[0.50, 0.75],
    px_hi=[0.65, 0.70, 0.75],
    tau_hi=[180.0, 240.0, 300.0],
    exit=[("off", None), ("margin", 0.5), ("margin", 1.0), ("margin", 2.0),
          ("margin", 4.0), ("flip", 3.0)],
    xmin_tau=[8.0, 30.0, 60.0],
)
KEYS = list(GRID)
_CTX = None


def _init(bankroll, expo, tr, te, l1, l2):
    """Python 3.14 starts workers with `forkserver` on Linux, so module globals
    are NOT inherited -- the caches must be re-opened in each worker rather
    than relied on from the parent. Each worker reads the pickles itself, which
    is cheaper than shipping ~100 MB of them down the pipe ten times."""
    global _CTX
    R = {l: build(l, LIVE["cross_hold_s"], 1) for l in (l1, l2)}
    _CTX = (R, bankroll, expo, tr, te, l1, l2)


def _cfg(c):
    d = dict(zip(KEYS, c))
    return dict(floor=d["floor"], ceil=LIVE["cross_max_margin_bps"],
                px_lo=LIVE["cross_min_price"], px_hi=d["px_hi"],
                tau_lo=LIVE["cross_min_tau_s"], tau_hi=d["tau_hi"],
                fill_cap=d["px_hi"]), d["exit"], d["xmin_tau"]


def fmt(c):
    d = dict(zip(KEYS, c))
    r, p = d["exit"]
    return ("floor %.2f px<=%.2f tau<=%3.0f  exit %-7s %-5s xmin %2.0f"
            % (d["floor"], d["px_hi"], d["tau_hi"], r,
               "" if p is None else p, d["xmin_tau"]))


def _score(c):
    R, B, E, TR, TE, L1, L2 = _CTX
    g, (rule, prm), xmt = _cfg(c)
    out = {}
    for look in (L1, L2):
        a = sim(R[look], bankroll=B, expo=E, rule=rule, prm=prm,
                min_tau=xmt, days=TR[look], **dict(g))
        b = sim(R[look], bankroll=B, expo=E, rule=rule, prm=prm,
                min_tau=xmt, days=TE[look], **dict(g))
        if not a or not b:
            return (None, c, None)
        out[look] = (a, b)
    return (min(out[L1][0]["d"], out[L2][0]["d"]), c, out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=10)
    ap.add_argument("--bankroll", type=float, default=5.11)
    a = ap.parse_args()
    global _CTX
    L1, L2 = LIVE["cross_look_s"], 2.0
    R, TR, TE = {}, {}, {}
    for look in (L1, L2):
        R[look] = build(look, LIVE["cross_hold_s"], a.jobs)
        days = sorted({dt.datetime.fromtimestamp(c[0] / 1000, dt.UTC).strftime("%m-%d")
                       for _, cs, *_ in R[look] for c in cs})
        k = len(days) // 2
        TR[look], TE[look] = set(days[:k]), set(days[k:])
    E = LIVE["cross_max_open_stake_pct"]
    _CTX = (R, a.bankroll, E, TR, TE, L1, L2)          # for the parent's own use
    combos = list(itertools.product(*(GRID[k] for k in KEYS)))
    print(f"cells: {len(combos)}   train {sorted(TR[L1])}   test {sorted(TE[L1])}",
          flush=True)
    with ProcessPoolExecutor(a.jobs, initializer=_init,
                             initargs=(a.bankroll, E, TR, TE, L1, L2)) as ex:
        rows = [r for r in ex.map(_score, combos, chunksize=4) if r[0] is not None]
    rows.sort(reverse=True, key=lambda x: x[0])
    SHIP = (LIVE["cross_min_margin_bps"], LIVE["cross_max_price"],
            LIVE["cross_max_tau_s"], ("off", None), LIVE["cross_exit_min_tau_s"])
    print("\n=== TOP 12 BY MIN(train L1, train L2) ===")
    print("  %-52s %-22s %s" % ("config", "TRAIN L1/L2", "TEST L1/L2  win%"))
    for m, c, o in rows[:12]:
        a1, b1 = o[L1]; a2, b2 = o[L2]
        print("  %-52s $%+6.2f/$%+6.2f   $%+6.2f/$%+6.2f  %4.1f%%/%4.1f%%"
              % (fmt(c), a1["d"], a2["d"], b1["d"], b2["d"], b1["win"], b2["win"]))
    shipped = next((r for r in rows if r[1] == SHIP), None)
    if shipped:
        m, c, o = shipped
        print("\n=== SHIPPED ===")
        print("  %-52s $%+6.2f/$%+6.2f   $%+6.2f/$%+6.2f  %4.1f%%/%4.1f%%"
              % (fmt(c), o[L1][0]["d"], o[L2][0]["d"], o[L1][1]["d"],
                 o[L2][1]["d"], o[L1][1]["win"], o[L2][1]["win"]))
    print("\n=== VERDICT ===")
    w = rows[0]
    print("  train-selected: %s" % fmt(w[1]))
    if shipped:
        b = w[2][L1][1]["d"] > shipped[2][L1][1]["d"] and \
            w[2][L2][1]["d"] > shipped[2][L2][1]["d"]
        print("  beats shipped on TEST at both looks: %s" % ("YES -> SHIP" if b
              else "no -> SHIP NOTHING (the rule was set before the run)"))


if __name__ == "__main__":
    main()
