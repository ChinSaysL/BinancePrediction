"""The gate-12 increment, scored as an increment.

The range-gate re-validation (FINDINGS 2026-09-14) noted gate 12 as "17% more
fills for no measurable loss", comparing the two samples' 95% CIs. Wrong
instrument: the two samples are NESTED, so the question is what the triggers
gate 12 ADDS are worth on their own. Growth is additive in log space, so

    growth(gate 12) - growth(gate 10)  IS  the (10,12] band's own growth.

No bootstrap of two overlapping samples needed. Same substrate and scoring as
tools/cross_range_gate_recheck.py (which this imports): btc-5m recorded venue
book, entry AT the limit under cross_limit_buffer_inv, measured 169bp fee,
fills modelled as "the ask did not run past the limit within the rest window".
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.cross_range_gate_recheck import (boot, dayt, fills,     # noqa: E402
                                            growth, triggers)


def main() -> int:
    base = 'data/recorded'
    q = pd.read_csv(f'{base}/quotes_arm.csv.gz')
    q = q[q.slug.str.startswith('btc-updown-5m')]
    r = pd.read_csv(f'{base}/rounds.csv.gz')
    rounds = {t.slug: t for t in r.itertuples()}
    allf = fills(triggers(q, rounds, 0.0, 2.0))       # every fill, no range gate

    print("cumulative gates (reproduction of the re-check table):")
    print(f"  {'gate':<6}{'n':>5}{'win':>7}{'ask':>7}{'growth':>9}{'day-t':>8}")
    for gate in (8.0, 10.0, 12.0, 15.0, 20.0, 0.0):
        x = allf if gate <= 0 else allf[allf.rng <= gate]
        lab = "OFF" if gate <= 0 else f"{gate:.0f}"
        print(f"  {lab:<6}{len(x):>5}{x.won.mean():>7.3f}{x.ask.mean():>7.3f}"
              f"{growth(x):>+9.3f}{dayt(x):>+8.2f}")

    print("\nthe INCREMENT each widening buys (growth here IS the delta):")
    for lo, hi in ((0, 8), (8, 10), (10, 12), (12, 15), (15, 20), (20, 1e9)):
        x = allf[(allf.rng > lo) & (allf.rng <= hi)]
        if not len(x):
            continue
        ci = ""
        if len(x) >= 20:
            blo, bhi = boot(x)
            ci = f"   growth CI [{blo:+.3f},{bhi:+.3f}]"
        his = f"{hi:.0f}" if hi < 1e8 else "inf"
        print(f"  ({lo:>2},{his:>3}] n={len(x):>3}  win {x.won.mean():.3f}  "
              f"ask {x.ask.mean():.3f}  edge {x.won.mean()-x.ask.mean():+.3f}  "
              f"growth {growth(x):+.3f}  day-t {dayt(x):+.2f}{ci}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
