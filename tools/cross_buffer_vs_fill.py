"""The cross arm pays its OWN limit, not the book. So what buffer is right?

Measured on 38 live cross orders (2026-09-14):

    decision.quote IS the book ask     (identical in 76% of rows)
    requested_px - ask   +0.1000       the buffer the config chose
    filled_px    - ask   +0.0943       what we actually paid
    price improvement    +0.0113       nearly none

The venue quotes, then bundles the order; it fills at about the limit rather
than price-improving to the book. That makes `cross_limit_buffer_inv` a direct
cost, not a safety margin -- at an ask of 0.6 it sets buf = 0.25*(1-0.6) = 0.10,
which is the +0.10 observed.

Fill model, from the venue's ~1s repricing: an order priced at `ask + buf`
fills when the ask a moment later has not run past it, i.e.
`min(ask over the next `hold` seconds) <= ask + buf`. Calibrated against the
live 71.1% fill rate at buf 0.10 before being trusted anywhere else.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import cross_knob, settings as S   # noqa: E402
from btcpredict.main import crossed_side                  # noqa: E402

FEE = 0.0169          # MEASURED, not the model curve
FRAC = 0.0672


def triggers(q, rounds, market, hold):
    """Cross entries at the shipped gates, with the forward ask path."""
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
            seen = sp[:i + 1]
            if G > 0 and 1e4 * (np.nanmax(seen) - np.nanmin(seen)) / K > G:
                continue
            asks = ua if side == 'Up' else da
            a0 = asks[i]
            if not np.isfinite(a0) or not (S.cross_min_price <= a0 <= S.cross_max_price):
                continue
            k = int(np.searchsorted(ts, ts[i] + hold * 1000.0, 'right'))
            fwd = asks[i + 1:max(k, i + 2)]
            fwd = fwd[np.isfinite(fwd)]
            out.append((pd.Timestamp(ts[i], unit='ms', tz='UTC').strftime('%Y-%m-%d'),
                        slug, float(a0),
                        float(np.min(fwd)) if len(fwd) else float(a0),
                        int(side == rr.winner)))
            took, n, last = side, n + 1, ts[i]
    return out


def score(rows, buf):
    """Fill at the limit when the ask has not run past it."""
    day, ret, fills = [], [], 0
    for d, _slug, a0, amin, won in rows:
        limit = a0 + buf
        if amin > limit + 1e-12:          # ask ran away: no fill
            continue
        fills += 1
        px = float(np.clip(limit, 1e-4, .9999))
        r = ((1 - px) / px - FEE) if won else -1.0
        day.append(d); ret.append(r)
    if not ret:
        return 0, 0.0, 0.0, np.nan
    t = pd.DataFrame({'day': day, 'ret': ret})
    lg = np.log1p(np.clip(FRAC * t.ret.to_numpy(), -0.999999, None))
    per = pd.Series(lg, index=t.day.to_numpy()).groupby(level=0).sum()
    sd = per.std(ddof=1)
    tt = per.mean() / (sd / np.sqrt(len(per))) if sd and len(per) > 2 else np.nan
    return fills, t.ret.mean(), lg.sum(), tt


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
    rows = triggers(q, rounds, a.market, a.hold)
    print(f"{a.market}: {len(rows)} triggers at the shipped gates, "
          f"fill window {a.hold:.0f}s\n")
    print(f"  {'buffer':<9}{'fills':>7}{'fill%':>8}{'EV/$1':>9}"
          f"{'growth':>9}{'day-t':>8}{'note':>26}")
    for buf in (0.00, 0.01, 0.02, 0.03, 0.05, 0.07, 0.10, 0.15, 0.20):
        f, ev, g, tt = score(rows, buf)
        note = "  <- SHIPPED (obs 71%)" if abs(buf - 0.10) < 1e-9 else ""
        print(f"  {buf:<9.2f}{f:>7}{100*f/len(rows):>7.0f}%{ev:>+9.4f}"
              f"{g:>+9.3f}{tt:>+8.2f}{note:>26}")
    print("\n  live check: at buf 0.10 the bot filled 27/38 = 71.1% and paid")
    print("  ask +0.0943. A model far from that is not to be trusted.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
