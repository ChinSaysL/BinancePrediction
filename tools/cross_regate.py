"""Re-score the conclusions that were computed on a contaminated trade set.

`cross_reversal_recorded.replay` marks `rev_prev` against the IMMEDIATELY
PRECEDING entry:

    int(prev_side is not None and side != prev_side)

so for a round going Up, Down, Down the first Down is marked and the SECOND is
not -- it matches the entry before it. Filtering `rev_prev == 0` therefore keeps
`Up` and that second `Down`: a mixed-side round, and an entry the live guard
would never have taken. The shipped guard skips against the last side ACTUALLY
TAKEN, so a skipped entry never becomes the reference:

    if not S.cross_allow_reversal and took and took != side: continue

Impact on the recorded week: 201 entries / 5 mixed-side rounds / max 3 per round
under the filter, against 199 / 0 / max 4 under the real guard. Growth falls
+2.494 -> +2.319 additive and day-t 2.27 -> 1.98, which is below the bar this
repo holds knobs to -- so every conclusion scored that way needs re-checking.

Unaffected (they already apply `took`): cross_buffer_sweep, cross_limit_rules,
cross_market_scan, cross_depth_floor, cross_dynamic_gate, cross_lock_hedge,
cross_multi_exposure.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings as S                        # noqa: E402
from tools.cross_reversal_recorded import load, replay, SLIP, FEE  # noqa: E402

FRAC = 0.0672


def guarded(raw: pd.DataFrame) -> pd.DataFrame:
    """The entries the LIVE reversal guard would actually have taken."""
    if S.cross_allow_reversal:
        return raw
    keep = []
    for _slug, g in raw.groupby('slug', sort=False):
        took = None
        for row in g.sort_values('k').itertuples():
            if took is not None and row.side != took:
                continue
            keep.append(row.Index)
            took = row.side
    return raw.loc[keep]


def score(t, frac=FRAC):
    """(growth, day-t, drop-best) per slippage model."""
    out = []
    for m in SLIP:
        fill = np.clip(SLIP[m](t.ask.to_numpy()), 1e-4, .9999)
        r = np.where(t.won.to_numpy() == 1, (1 - fill) / fill * (1 - FEE), -1.0)
        lg = np.log1p(np.clip(frac * r, -0.999999, None))
        per = pd.Series(lg, index=t.day.to_numpy()).groupby(level=0).sum()
        sd = per.std(ddof=1)
        tt = per.mean() / (sd / np.sqrt(len(per))) if sd and len(per) > 2 else np.nan
        dd = per.drop(per.idxmax()).sum() if len(per) > 1 else np.nan
        out.append((lg.sum(), tt, dd))
    return out


def row(lab, t, mark=""):
    if len(t) < 10 or t.day.nunique() < 3:
        print(f"  {lab:<22}{len(t):>5}  (too few)")
        return
    s = score(t)
    print(f"  {lab:<22}{len(t):>5}{t.won.mean():>7.3f}   "
          + "".join(f"{f'{g:+.3f}/{tt:+.2f}/{d:+.3f}':<24}" for g, tt, d in s)
          + mark)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded/x')
    a = ap.parse_args()
    rounds, quotes = load(a.base)

    def R(dep=None, cap=None, rng=None, look=None):
        raw = replay(rounds, quotes, S.cross_min_tau_s, S.cross_max_tau_s,
                     S.cross_max_depth_bps if dep is None else dep,
                     int(S.cross_max_per_round) if cap is None else cap,
                     S.cross_max_round_range_bps if rng is None else rng,
                     S.cross_look_s if look is None else look)
        return guarded(raw)

    hdr = (f"  {'':<22}{'n':>5}{'win':>7}   "
           + "".join(f"{m+' g/t/drop':<24}" for m in SLIP))

    print("=== 1. RANGE GATE (was: NOT ESTABLISHED -> SUPPORTED) ===")
    print(hdr)
    for g in (0, 8, 10, 12, 15, 20):
        row("OFF" if g == 0 else f"gate {g}", R(rng=g),
            "  <- SHIPPED" if g == 10 else "")

    print("\n=== 2. DEPTH GATE (was: 5.0 right, tightening hurts) ===")
    print(hdr)
    for d in (0.5, 1.0, 2.0, 5.0, 8.0):
        row(f"depth {d}", R(dep=d), "  <- SHIPPED" if d == 5.0 else "")

    print("\n=== 3. PRICE BAND (was: raising the floor helps on paper) ===")
    print(hdr)
    t = R()
    for lo, hi in ((0.10, 0.90), (0.45, 0.90), (0.50, 0.90), (0.50, 0.80)):
        row(f"band [{lo:.2f},{hi:.2f}]", t[(t.ask >= lo) & (t.ask <= hi)],
            "  <- SHIPPED" if (lo, hi) == (0.10, 0.90) else "")

    print("\n=== 4. STAKE MULTIPLIER (was: 2.25 -> 3.0) ===")
    print(f"  {'':<22}{'stake':>7}   "
          + "".join(f"{m+' g/t/drop':<24}" for m in SLIP))
    t = R()
    for mult in (1.5, 2.25, 3.0, 4.0):
        s = score(t, frac=0.0224 * mult)
        print(f"  {f'mult {mult}':<22}{100*0.0224*mult:>6.2f}%   "
              + "".join(f"{f'{g:+.3f}/{tt:+.2f}/{d:+.3f}':<24}" for g, tt, d in s)
              + ("  <- SHIPPED" if mult == 3.0 else ""))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
