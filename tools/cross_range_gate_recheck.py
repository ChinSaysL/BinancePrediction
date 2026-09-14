"""Re-measure the range gate at the CORRECTED entry price.

The gate's justification (FINDINGS, 2026-09-14) is arithmetic:

    blocked triggers  edge +0.018
    kept triggers     edge +0.192
    "+0.018 cannot survive the measured +0.088 slippage"

That +0.088 was never slippage. It was `cross_limit_buffer_inv` -- the arm's own
limit, which the venue fills at. It was halved 0.25 -> 0.125 the same day, so at
a blocked trigger's mean ask of 0.610 the buffer is now 0.125*0.39 = 0.049, not
0.088. The bar the gate was calibrated against moved by half, so the gate has to
be re-asked rather than assumed.

Scored exactly like the buffer sweep: entry AT the limit, the MEASURED 169bp
fee, fills modelled as "the ask did not run past the limit within the rest
window", day-clustered t, drop-best-day, and a bootstrap over ROUNDS.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings as S              # noqa: E402
from btcpredict.main import crossed_side                 # noqa: E402

FEE = 0.0169
FRAC = 0.0672


def triggers(q, rounds, gate, hold):
    """Every cross entry at the shipped gates, with `gate` swapped in.

    gate <= 0 means the range gate is OFF.
    """
    out = []
    for slug, g in q.groupby('slug', sort=False):
        rr = rounds.get(slug)
        if rr is None or not np.isfinite(rr.strike) or not isinstance(rr.winner, str):
            continue
        g = g.sort_values('ts_ms')
        ts = g.ts_ms.to_numpy(float); sp = g.spot.to_numpy(float)
        ua = g.up_ask.to_numpy(float); da = g.down_ask.to_numpy(float)
        K = float(rr.strike); end = float(rr.end_ms)
        took, n, last = None, 0, -9e9
        for i in range(len(ts)):
            tau = (end - ts[i]) / 1000.0
            if not (S.cross_min_tau_s <= tau <= S.cross_max_tau_s):
                continue
            if ts[i] - last < S.cross_reentry_gap_s * 1000.0:
                continue
            j0 = int(np.searchsorted(ts, ts[i] - (S.cross_look_s + 2.0) * 1000.0, 'left'))
            if i - j0 < 1:
                continue
            side = crossed_side([(int(a), float(b)) for a, b in
                                 zip(ts[j0:i + 1], sp[j0:i + 1])], K, S.cross_look_s)
            if side is None or n >= S.cross_max_per_round:
                continue
            if took and took != side:
                continue
            dep = abs(sp[i] - K)
            if dep < S.cross_min_depth_ticks * S.cross_tick_size - 1e-12:
                continue
            if 1e4 * dep / K > S.cross_max_depth_bps:
                continue
            rng = 1e4 * (np.nanmax(sp[:i + 1]) - np.nanmin(sp[:i + 1])) / K
            if gate > 0 and rng > gate:
                continue
            a0 = (ua if side == 'Up' else da)[i]
            if not np.isfinite(a0) or not (S.cross_min_price <= a0 <= S.cross_max_price):
                continue
            k = int(np.searchsorted(ts, ts[i] + hold * 1000.0, 'right'))
            fwd = (ua if side == 'Up' else da)[i + 1:max(k, i + 2)]
            fwd = fwd[np.isfinite(fwd)]
            out.append((pd.Timestamp(ts[i], unit='ms', tz='UTC').strftime('%Y-%m-%d'),
                        slug, float(a0),
                        float(np.min(fwd)) if len(fwd) else float(a0),
                        int(side == rr.winner), float(rng)))
            took, n, last = side, n + 1, ts[i]
    return out


def fills(rows):
    recs = []
    for d, slug, a0, amin, won, rng in rows:
        buf = max(0.01, min(S.cross_limit_buffer, S.cross_limit_buffer_inv * (1 - a0)))
        lim = round(a0 + buf, 2)
        if amin > lim + 1e-12:
            continue
        px = float(np.clip(lim, 1e-4, .9999))
        recs.append((d, slug, ((1 - px) / px - FEE) if won else -1.0, a0, won, rng))
    return pd.DataFrame(recs, columns=['day', 'slug', 'ret', 'ask', 'won', 'rng'])


def growth(t):
    return float(np.log1p(np.clip(FRAC * t.ret.to_numpy(), -0.999999, None)).sum()) if len(t) else 0.0


def dayt(t):
    lg = np.log1p(np.clip(FRAC * t.ret.to_numpy(), -0.999999, None))
    per = pd.Series(lg, index=t.day.to_numpy()).groupby(level=0).sum()
    sd = per.std(ddof=1)
    return per.mean() / (sd / np.sqrt(len(per))) if sd and len(per) > 2 else np.nan


def dropbest(t):
    per = pd.Series(np.log1p(np.clip(FRAC * t.ret.to_numpy(), -0.999999, None)),
                    index=t.day.to_numpy()).groupby(level=0).sum()
    return float(per.sum() - per.max()) if len(per) > 1 else np.nan


def boot(t, n=3000, seed=13):
    g = [x for _, x in t.groupby('slug', sort=False)]
    rng = np.random.default_rng(seed)
    k = len(g)
    o = np.array([growth(pd.concat([g[j] for j in rng.integers(0, k, k)])) for _ in range(n)])
    return float(np.percentile(o, 2.5)), float(np.percentile(o, 97.5))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    ap.add_argument('--market', default='btc-updown-5m')
    ap.add_argument('--hold', type=float, default=2.0)
    a = ap.parse_args()
    q = pd.read_csv(f'{a.base}/quotes_arm.csv.gz')
    q = q[q.slug.str.startswith(a.market)]
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    rounds = {t.slug: t for t in r.itertuples()}
    shipped = S.cross_overrides.get(a.market, {}).get('max_round_range_bps',
                                                      S.cross_max_round_range_bps)
    print(f"{a.market}: buffer inv {S.cross_limit_buffer_inv}, "
          f"rest {a.hold:.0f}s, shipped gate {shipped}\n")
    print(f"  {'gate bps':<11}{'n':>5}{'win':>7}{'ask':>7}{'growth':>9}"
          f"{'day-t':>8}{'drop-best':>11}{'95% CI':>24}")
    for gate in (8.0, 10.0, 12.0, 15.0, 20.0, 30.0, 0.0):
        t = fills(triggers(q, rounds, gate, a.hold))
        if len(t) < 20:
            continue
        lo, hi = boot(t)
        lab = "OFF" if gate <= 0 else f"{gate:.0f}"
        tag = "  <- shipped" if abs(gate - shipped) < 1e-9 else ""
        print(f"  {lab:<11}{len(t):>5}{t.won.mean():>7.3f}{t.ask.mean():>7.3f}"
              f"{growth(t):>+9.3f}{dayt(t):>+8.2f}{dropbest(t):>+11.3f}"
              f"{f'[{lo:+.3f}, {hi:+.3f}]':>24}{tag}")

    print("\n  the triggers the SHIPPED gate refuses, scored on their own:")
    allt = fills(triggers(q, rounds, 0.0, a.hold))
    blk = allt[allt.rng > shipped]
    if len(blk) >= 10:
        lo, hi = boot(blk) if len(blk) >= 20 else (np.nan, np.nan)
        print(f"    n={len(blk)}  win {blk.won.mean():.3f}  ask {blk.ask.mean():.3f}"
              f"  growth {growth(blk):+.3f}  day-t {dayt(blk):+.2f}"
              f"  CI [{lo:+.3f}, {hi:+.3f}]")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
