"""Can a regime gate rescue the model arm, the way it rescued the cross arm?

The model arm was switched off on 2026-09-14 after a knob sweep in which no
setting cleared the bar. That sweep covered min_edge, haircut, persistence,
min_quote, max_quote and max_tau -- it never swept the round-range window,
which hours later turned out to be the single most useful knob the cross arm
has (NOT ESTABLISHED -> SUPPORTED, day-t 2.27-4.08 on the recorded book).

So the arm deserves one more question before it stays off: is there a REGIME,
measured causally, in which its 688 live fills actually carry edge?

Method, matching the standing rule:
  * real fills at real filled prices, from the live ledger
  * range-so-far measured from the round open to the ENTRY instant, never the
    full-round range (that is lookahead)
  * scored on compounded growth under both slippage models, not EV/$1
  * day-clustered t and drop-best-day

The honest null is that it stays off: lifetime the arm won 0.64971 against an
average ask of 0.64967 over 688 fills, which is zero margin to four decimals.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SLIP = {
    'book':      lambda q: q,
    'additive':  lambda q: q + 0.088,
    'reverting': lambda q: 0.47 * q + 0.40,
}
FEE = 200 / 1e4
FRAC = 0.0224            # the model arm's own sizing (no cross multiplier)


def load(base='data/recorded'):
    f = pd.read_csv(f'{base}/fills_full.csv.gz')
    q = pd.read_csv(f'{base}/quotes_full.csv.gz')
    r = pd.read_csv(f'{base}/rounds.csv.gz')
    f = f[(f['mode'] == 'live') & f.won.notna() & (f.void.fillna(0) == 0)].copy()
    f = f[f.filled_px.notna() & f.stake.notna()]
    return f, q, r


def add_regime(f, q, r):
    """Range-so-far in bps, round open -> entry instant, per fill."""
    strike = dict(zip(r.slug, r.strike))
    q = q.sort_values(['slug', 'ts_ms'])
    grp = {s: (g.ts_ms.to_numpy(), g.spot.to_numpy())
           for s, g in q.groupby('slug', sort=False)}
    out = []
    for t in f.itertuples():
        ts, sp = grp.get(t.slug, (None, None))
        k = strike.get(t.slug)
        if ts is None or not k or not np.isfinite(k):
            out.append(np.nan)
            continue
        m = ts <= t.ts_ms
        if m.sum() < 2:
            out.append(np.nan)
            continue
        seen = sp[m]
        out.append(1e4 * (np.nanmax(seen) - np.nanmin(seen)) / k)
    f = f.copy()
    f['range_bps'] = out
    f['day'] = pd.to_datetime(f.ts_ms, unit='ms', utc=True).dt.strftime('%Y-%m-%d')
    return f


def stats(x, label):
    if len(x) < 5:
        print(f"  {label:<22} n={len(x):<4} (too few)")
        return
    w = x.won.mean()
    ask = x.filled_px.mean()
    cells = []
    for m in SLIP:
        fill = np.clip(SLIP[m](x.filled_px.to_numpy()), 1e-4, .9999)
        # `filled_px` is already the realised price, so `book` is the truth and
        # the two models are a sensitivity check on top of it.
        ret = np.where(x.won.to_numpy() == 1, (1 - fill) / fill * (1 - FEE), -1.0)
        lg = np.log1p(np.clip(FRAC * ret, -0.999999, None))
        per = pd.Series(lg, index=x.day.to_numpy()).groupby(level=0).sum()
        sd = per.std(ddof=1)
        t = per.mean() / (sd / np.sqrt(len(per))) if sd and len(per) > 2 else np.nan
        drop = per.drop(per.idxmax()).sum() if len(per) > 1 else np.nan
        cells.append(f"{lg.sum():+.3f}/{t:+.2f}/{drop:+.3f}")
    print(f"  {label:<22} n={len(x):<4} win={w:.3f} ask={ask:.3f} "
          f"edge={w-ask:+.3f}  " + "  ".join(cells))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    a = ap.parse_args()
    f, q, r = load(a.base)
    f = add_regime(f, q, r)
    f = f[f.range_bps.notna()]
    print(f"model-arm settled fills with a causal regime: {len(f)}  "
          f"days {f.day.nunique()}")
    print(f"{'':24}{'':26}{'  '.join(f'{m} g/t/drop' for m in SLIP)}")
    stats(f, "ALL (arm as shipped)")

    print("\n=== by range-so-far at entry ===")
    for lo, hi in ((0, 3), (3, 5), (5, 8), (8, 12), (12, 20), (20, 1e9)):
        stats(f[(f.range_bps >= lo) & (f.range_bps < hi)],
              f"{lo}-{'inf' if hi > 1e8 else hi} bps")

    print("\n=== candidate windows ===")
    for lo, hi in ((5, 12), (3, 12), (0, 8), (8, 1e9), (0, 12), (12, 1e9)):
        stats(f[(f.range_bps >= lo) & (f.range_bps < hi)],
              f"window {lo}-{'inf' if hi > 1e8 else hi}")

    print("\n=== by entry price, for contrast ===")
    for lo, hi in ((0, 0.5), (0.5, 0.65), (0.65, 0.8), (0.8, 1.0)):
        stats(f[(f.filled_px >= lo) & (f.filled_px < hi)], f"ask {lo}-{hi}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
