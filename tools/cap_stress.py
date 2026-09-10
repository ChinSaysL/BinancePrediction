"""Is the daily stop worth keeping? Stress it where it should matter.

The earlier sweep only ever ran on a POSITIVE edge, and found the monthly
drawdown flat across the whole cap range. But a daily stop is insurance, and
insurance pays off in the states the bootstrap cannot draw from the recorded
pool: an edge that has gone to zero or negative, and a model that is simply
broken. Those are the scenarios that decide whether to keep it.

Run: python -m tools.cap_stress  (or as a script from the repo root)
"""
from __future__ import annotations

import statistics as st

from tools.project_balance import build_pools, simulate

B0 = 76.49
TPD = 44.0
PATHS = 2000
DAYS = 30
CAPS = (0.0, 0.45, 0.20, 0.10)


def row(label: str, pool_rets, edge, caps=CAPS) -> None:
    print(f"\n=== {label} ===")
    print(f"  {'daily stop':>11}{'med balance':>13}{'median DD':>11}{'p95 DD':>9}"
          f"{'P(down)':>10}{'days stopped':>14}")
    for c in caps:
        r = simulate(pool_rets, tilts, bankroll=B0, days=DAYS, edge=edge,
                     tpd=TPD, paths=PATHS, resume_after_halt=True, cap_pct=c)
        name = "OFF" if c == 0.0 else f"{c:.0%}"
        print(f"  {name:>11}${r[DAYS]['med']:>12.2f}{r['med_dd']:>10.1f}%"
              f"{r['p95_dd']:>8.1f}%{r[DAYS]['down']:>10.1%}"
              f"{r['stopped_days']:>10.1f}/{DAYS}")


rets, tilts, tpd = build_pools("btc-updown-5m")
print(f"start ${B0}  |  {TPD:.0f} trades/day  |  {PATHS} paths  |  {DAYS} days")

# The states that matter. Each is the recorded per-$1 distribution re-aimed at a
# different expected return, plus one literal broken model.
row("edge intact (+0.25/$1)", rets, 0.25)
row("edge gone (0.00/$1) -- a fair game, fees aside", rets, 0.0)
row("edge slightly negative (-0.05/$1)", rets, -0.05)
row("edge clearly negative (-0.30/$1)", rets, -0.30)
row("model broken: EVERY trade loses its stake", [-1.05], None)
