"""Every strike crossing in a window, and exactly why the arm did or did not take it.

Built after two failed attempts at this question on 2026-09-14 taught the two
things that make it hard:

  1. NEVER filter the tick series on `up_ask is not null`. Only 48.4% of
     recorded quote rows carry an ask, and dropping the rest hides crossings in
     precisely the stretches where the book feed was dark. The first pass found
     1 trigger in 4,282 rounds because of this.
  2. `acted` in `decisions` means THE ORDER FILLED, not "an order was placed".
     An attempt is a decision whose reason starts `cross into`; an unfilled one
     has no fill row. Querying acted=1-with-no-fill searches for a state that
     cannot exist.

For each crossing this reports one of:
    TRADED        a fill exists
    ATTEMPTED     an order went out and did not fill
    REFUSED       a `cross refused:` row names the gate
    silent:<gate> no row was ever written -- the gate that stopped it, named,
                  by walking _cross_tick's checks in the same order it does
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings as S      # noqa: E402
from btcpredict.main import crossed_side         # noqa: E402


def T(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime('%H:%M:%S')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    ap.add_argument('--prefix', default='w_')
    ap.add_argument('--market', default='btc-updown-5m')
    a = ap.parse_args()
    B, P = a.base, a.prefix
    q = pd.read_csv(f'{B}/{P}quotes.csv.gz')
    r = pd.read_csv(f'{B}/{P}rounds.csv.gz')
    dec = pd.read_csv(f'{B}/{P}decisions.csv.gz')
    fil = pd.read_csv(f'{B}/{P}fills.csv.gz')
    q = q[q.slug.str.startswith(a.market)]
    rounds = {t.slug: t for t in r.itertuples()}

    print(f"gate: tau {S.cross_min_tau_s:.0f}-{S.cross_max_tau_s:.0f}  "
          f"depth<={S.cross_max_depth_bps}  look {S.cross_look_s}  "
          f"cap {S.cross_max_per_round}  range<={S.cross_max_round_range_bps}  "
          f"band [{S.cross_min_price},{S.cross_max_price}]  "
          f"allow_reversal={S.cross_allow_reversal}")

    n_cross = n_taken = 0
    for slug, g in q.groupby('slug', sort=False):
        rr = rounds.get(slug)
        if rr is None or not np.isfinite(rr.strike):
            continue
        g = g.sort_values('ts_ms')
        ts = g.ts_ms.to_numpy(float); spot = g.spot.to_numpy(float)
        ua = g.up_ask.to_numpy(float); da = g.down_ask.to_numpy(float)
        strike, end_ms = float(rr.strike), float(rr.end_ms)
        lines, taken_side, taken_n = [], None, 0
        for i in range(len(ts)):
            tau = (end_ms - ts[i]) / 1000.0
            j0 = int(np.searchsorted(ts, ts[i] - (S.cross_look_s + 2.0) * 1000.0, 'left'))
            if i - j0 < 1:
                continue
            side = crossed_side([(int(t), float(p)) for t, p in
                                 zip(ts[j0:i + 1], spot[j0:i + 1])],
                                strike, S.cross_look_s)
            if side is None:
                continue
            n_cross += 1
            depth = 1e4 * abs(spot[i] - strike) / strike
            seen = spot[:i + 1]
            rng = 1e4 * (np.nanmax(seen) - np.nanmin(seen)) / strike
            ask = ua[i] if side == 'Up' else da[i]

            # _cross_tick's order of checks, so the FIRST failure is the reason.
            why = None
            if taken_n >= S.cross_max_per_round:
                why = 'silent: per-round cap'
            elif not (S.cross_min_tau_s <= tau <= S.cross_max_tau_s):
                why = 'silent: tau outside 5-60'
            elif (not S.cross_allow_reversal and taken_side
                  and taken_side != side):
                why = 'REFUSED: reversal'
            elif depth > S.cross_max_depth_bps:
                why = f'REFUSED: depth {depth:.2f}bps'
            elif S.cross_max_round_range_bps > 0 and rng > S.cross_max_round_range_bps:
                why = f'REFUSED: range {rng:.1f}bps'
            elif not np.isfinite(ask):
                why = 'REFUSED: no ask (book dark)'
            elif not (S.cross_min_price <= ask <= S.cross_max_price):
                why = f'REFUSED: ask {ask:.2f} out of band'
            if why is None:
                why = 'WOULD TAKE'
                taken_side, taken_n = side, taken_n + 1
                n_taken += 1
            lines.append((ts[i], tau, side, depth, rng, ask, why))
        if not lines:
            continue
        rt = dt.datetime.fromtimestamp(rr.start_ms / 1000, dt.UTC).strftime('%H:%M')
        print(f"\n--- round {rt}  strike {strike:.2f}  winner {rr.winner} ---")
        for tsx, tau, side, depth, rng, ask, why in lines:
            askf = f"{ask:.2f}" if np.isfinite(ask) else " --"
            print(f"   {T(tsx)} tau={tau:6.1f} {side:<5} depth={depth:5.2f} "
                  f"range={rng:5.1f} ask={askf}  {why}")
        d = dec[dec.slug == slug]
        f2 = fil[fil.slug == slug]
        for t in d.itertuples():
            print(f"      ledger  {T(t.ts_ms)} acted={t.acted}  {str(t.reason)[:62]}")
        for t in f2.itertuples():
            res = 'VOID' if t.void == 1 else ('open' if pd.isna(t.won)
                                              else ('WIN' if t.won else 'LOSS'))
            print(f"      FILL    {T(t.ts_ms)} {t.side} @{t.filled_px} {res} "
                  f"pnl={t.pnl}")
    print(f"\n=== {n_cross} crossings detected, {n_taken} the gate would take ===")
    print(f"    ledger in window: {len(dec)} decision rows, {len(fil)} fills")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
