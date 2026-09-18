"""Arm the reversal guard on the CROSSING, not on the entry.

LIVE 2026-09-15 00:59, bnb-updown-15m:

    00:59:21  Up   DECLINED  only 2 tick(s) through the strike, floor is 5
    00:59:24  Down TAKEN     9 ticks through the strike, passes
    00:59:38  spot back above the strike; the round settles Up

Binance's own 1s klines confirm both crossings are real -- 45 trades lifted BNB
to 719.28 and it held above the strike for three seconds on 4 more. So this is
not a bad tick. It is a whipsaw, and the arm took the return leg of it.

`cross_allow_reversal` already refuses the opposite side of a round once an
entry exists -- that is what stopped bnb-5m doing the same thing 70ms earlier.
But it arms on the ENTRY. A crossing the arm DECLINED leaves it unarmed, so a
declined up-cross followed by an accepted down-cross is exactly the hole.

This scores the alternative: remember the side of every CROSSING seen this
round, declined or not, and refuse the opposite one for `block_s` seconds.
Scored like all the other cross work -- entry AT the limit, measured 169bp fee,
fills modelled as "the ask did not run past the limit", day-clustered t,
drop-best-day, bootstrap over ROUNDS.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import cross_knob, settings as S   # noqa: E402
from btcpredict.main import crossed_side, passes_depth_floor  # noqa: E402

FEE = 0.0169
FRAC = 0.0672


def triggers(q, rounds, market, hold, block_s):
    """block_s > 0 arms the reversal guard on every crossing seen."""
    out = []
    G = cross_knob(market, "max_round_range_bps")
    for slug, g in q.groupby('slug', sort=False):
        rr = rounds.get(slug)
        if rr is None or not np.isfinite(rr.strike) or not isinstance(rr.winner, str):
            continue
        g = g.sort_values('ts_ms')
        ts = g.ts_ms.to_numpy(float); sp = g.spot.to_numpy(float)
        ua = g.up_ask.to_numpy(float); da = g.down_ask.to_numpy(float)
        K = float(rr.strike); end = float(rr.end_ms)
        took, n, last = None, 0, -9e9
        seen_side, seen_ms = None, -9e9          # the last CROSSING, declined or not
        for i in range(len(ts)):
            tau = (end - ts[i]) / 1000.0
            if not (S.cross_min_tau_s <= tau <= S.cross_max_tau_s):
                continue
            j0 = int(np.searchsorted(ts, ts[i] - (S.cross_look_s + 2.0) * 1000.0, 'left'))
            if i - j0 < 1:
                continue
            side = crossed_side([(int(a), float(b)) for a, b in
                                 zip(ts[j0:i + 1], sp[j0:i + 1])], K, S.cross_look_s)
            if side is None:
                continue
            # Seen BEFORE any gate: that is the whole point.
            if side != seen_side:
                seen_side, seen_ms = side, ts[i]
            if block_s > 0 and seen_side is not None and side != seen_side:
                continue
            blocked = (block_s > 0 and seen_side is not None and side != seen_side)
            if blocked:
                continue
            if ts[i] - last < S.cross_reentry_gap_s * 1000.0:
                continue
            if n >= S.cross_max_per_round or (took and took != side):
                continue
            # The floor and its tolerance come from ONE definition shared with
            # the live arm. Reading `S.cross_min_depth_ticks * S.cross_tick_size`
            # here replayed a floor the bot does not have: the global 0.01 is
            # wrong per market, and on xrp/doge it puts the floor ABOVE the
            # ceiling, i.e. an empty band the arm can never fire in.
            dep = abs(sp[i] - K)
            if not passes_depth_floor(sp[i], K, market):
                continue
            if 1e4 * dep / K > cross_knob(market, "max_depth_bps"):
                continue
            rng = 1e4 * (np.nanmax(sp[:i + 1]) - np.nanmin(sp[:i + 1])) / K
            if G > 0 and rng > G:
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
                        int(side == rr.winner)))
            took, n, last = side, n + 1, ts[i]
    return out


def flip_guard(q, rounds, market, hold, block_s):
    """Refuse a crossing whose side flipped within block_s of the previous one."""
    out = []
    G = cross_knob(market, "max_round_range_bps")
    for slug, g in q.groupby('slug', sort=False):
        rr = rounds.get(slug)
        if rr is None or not np.isfinite(rr.strike) or not isinstance(rr.winner, str):
            continue
        g = g.sort_values('ts_ms')
        ts = g.ts_ms.to_numpy(float); sp = g.spot.to_numpy(float)
        ua = g.up_ask.to_numpy(float); da = g.down_ask.to_numpy(float)
        K = float(rr.strike); end = float(rr.end_ms)
        took, n, last = None, 0, -9e9
        prev_side, prev_ms = None, -9e9
        for i in range(len(ts)):
            tau = (end - ts[i]) / 1000.0
            if not (S.cross_min_tau_s <= tau <= S.cross_max_tau_s):
                continue
            j0 = int(np.searchsorted(ts, ts[i] - (S.cross_look_s + 2.0) * 1000.0, 'left'))
            if i - j0 < 1:
                continue
            side = crossed_side([(int(a), float(b)) for a, b in
                                 zip(ts[j0:i + 1], sp[j0:i + 1])], K, S.cross_look_s)
            if side is None:
                continue
            flipped = (prev_side is not None and side != prev_side
                       and (ts[i] - prev_ms) <= block_s * 1000.0)
            prev_side, prev_ms = side, ts[i]
            if block_s > 0 and flipped:
                continue
            if ts[i] - last < S.cross_reentry_gap_s * 1000.0:
                continue
            if n >= S.cross_max_per_round or (took and took != side):
                continue
            dep = abs(sp[i] - K)
            if not passes_depth_floor(sp[i], K, market):
                continue
            if 1e4 * dep / K > cross_knob(market, "max_depth_bps"):
                continue
            rng = 1e4 * (np.nanmax(sp[:i + 1]) - np.nanmin(sp[:i + 1])) / K
            if G > 0 and rng > G:
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
                        int(side == rr.winner)))
            took, n, last = side, n + 1, ts[i]
    return out


def fills(rows):
    recs = []
    for d, slug, a0, amin, won in rows:
        buf = max(0.01, min(S.cross_limit_buffer, S.cross_limit_buffer_inv * (1 - a0)))
        lim = round(a0 + buf, 2)
        if amin > lim + 1e-12:
            continue
        px = float(np.clip(lim, 1e-4, .9999))
        recs.append((d, slug, ((1 - px) / px - FEE) if won else -1.0, a0, won))
    return pd.DataFrame(recs, columns=['day', 'slug', 'ret', 'ask', 'won'])


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


def boot(t, n=2000, seed=19):
    g = [x for _, x in t.groupby('slug', sort=False)]
    if len(g) < 5:
        return np.nan, np.nan
    rng = np.random.default_rng(seed); k = len(g)
    o = np.array([growth(pd.concat([g[j] for j in rng.integers(0, k, k)])) for _ in range(n)])
    return float(np.percentile(o, 2.5)), float(np.percentile(o, 97.5))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    ap.add_argument('--market', default='btc-updown-5m')
    ap.add_argument('--hold', type=float, default=20.0)
    a = ap.parse_args()
    q = pd.read_csv(f'{a.base}/quotes_arm.csv.gz')
    q = q[q.slug.str.startswith(a.market)]
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    rounds = {t.slug: t for t in r.itertuples()}
    print(f"{a.market}: refuse a crossing that FLIPPED side within N seconds "
          f"of the previous crossing\n")
    print(f"  {'rule':<26}{'n':>5}{'win':>7}{'ask':>7}{'growth':>9}"
          f"{'day-t':>8}{'drop-best':>11}{'95% CI':>24}")
    for bs in (0.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0):
        t = fills(flip_guard(q, rounds, a.market, a.hold, bs))
        if len(t) < 10:
            continue
        lo, hi = boot(t)
        lab = "SHIPPED (no guard)" if bs <= 0 else f"block flips < {bs:.0f}s"
        print(f"  {lab:<26}{len(t):>5}{t.won.mean():>7.3f}{t.ask.mean():>7.3f}"
              f"{growth(t):>+9.3f}{dayt(t):>+8.2f}{dropbest(t):>+11.3f}"
              f"{f'[{lo:+.3f}, {hi:+.3f}]':>24}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
