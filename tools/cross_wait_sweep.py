"""How long should a cross limit rest before cancel? Sweep the wait (2026-09-15).

`cross_limit_wait_s` shipped at 2.0 on 2026-09-14, chosen against 20s on the
adverse-selection argument ("an order fills only when the ask comes back DOWN
to it, which is the move that makes the side we bought less likely to win") --
but only TWO values were ever compared (2 vs 20). This sweeps the middle.

Fill model, the same one the buffer tools use: the order lands at t+LAT
(measured 2.7-5.2s live), then rests for W seconds, and fills if the ask trades
at or below the limit in [LAT, LAT+W] (bounded by the bell, tau-1). It fills at
the better of (limit, the ask that crossed it). Longer W adds fills; the
question is whether they are WORTH having -- so watch the winning rate and the
compounded growth together.

Rule held at the shipped limit: ask + max(0.01, min(0.25, 0.125*(1-ask))).
Scored the repo way: growth under the book pricing, day-clustered t, drop-best,
at the current stake fraction (0.0672).
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import cross_knob, settings as S   # noqa: E402
from btcpredict.main import crossed_side                  # noqa: E402

FEE = 200 / 1e4
FRAC = 0.0672          # stake_fraction 0.0224 * cross_stake_mult 3.0
LAT = 3.0              # measured decision -> resting on the book


def collect(q, rounds, market, horizon):
    """Shipped-gate triggers with the forward ask path out to LAT+horizon."""
    out = []
    G = cross_knob(market, 'max_round_range_bps')
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
            if not S.cross_allow_reversal and took and took != side:
                continue
            dep = abs(sp[i] - K)
            if dep < S.cross_min_depth_ticks * S.cross_tick_size - 1e-12:
                continue
            if 1e4 * dep / K > S.cross_max_depth_bps:
                continue
            seen = sp[:i + 1]
            if G > 0 and 1e4 * (np.nanmax(seen) - np.nanmin(seen)) / K > G:
                continue
            ask = ua[i] if side == 'Up' else da[i]
            if not np.isfinite(ask) or not (S.cross_min_price <= ask <= S.cross_max_price):
                continue
            widx = ua if side == 'Up' else da
            k0 = int(np.searchsorted(ts, ts[i] + LAT * 1000.0, 'left'))
            k1 = int(np.searchsorted(ts, ts[i] + min(tau - 1.0, LAT + horizon) * 1000.0, 'right'))
            path_ts = ts[k0:k1]; path_ask = widx[k0:k1]
            ok = np.isfinite(path_ask)
            out.append((pd.Timestamp(ts[i], unit='ms', tz='UTC').strftime('%Y-%m-%d'),
                        float(ts[i]), float(ask), int(side == rr.winner),
                        path_ts[ok], path_ask[ok]))
            took, n, last = side, n + 1, ts[i]
    return out


def limit_for(ask):
    return min(round(ask + max(0.01, min(S.cross_limit_buffer,
                                          0.125 * (1.0 - ask))), 2), 0.99)


def run(row, wait, price):
    day, t0, ask, won, path_ts, path_ask = row
    lim = limit_for(ask)
    upto = t0 + (LAT + wait) * 1000.0
    seg = path_ask[path_ts <= upto]
    hit = seg[seg <= lim]
    if len(hit) == 0:
        return None
    px = min(lim, float(hit[0])) if price == 'touch' else lim
    return (day, won, px)


def score(t):
    fill = np.clip(t.ask.to_numpy(), 1e-4, .9999)
    r = np.where(t.won.to_numpy() == 1, (1 - fill) / fill * (1 - FEE), -1.0)
    lg = np.log1p(np.clip(FRAC * r, -0.999999, None))
    per = pd.Series(lg, index=t.day.to_numpy()).groupby(level=0).sum()
    sd = per.std(ddof=1)
    tt = per.mean() / (sd / math.sqrt(len(per))) if sd else float('nan')
    return lg.sum(), tt, per.drop(per.idxmax()).sum()


def main() -> int:
    global LAT
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    ap.add_argument('--market', default='btc-updown-5m')
    ap.add_argument('--latency', type=float, default=3.0)
    ap.add_argument('--price', choices=('touch', 'limit'), default='touch')
    a = ap.parse_args()
    LAT = a.latency
    q = pd.read_csv(f'{a.base}/quotes_arm.csv.gz')
    q = q[q.slug.str.startswith(a.market)]
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    rounds = {t.slug: t for t in r.itertuples()}
    rows = collect(q, rounds, a.market, horizon=20.0)
    n = len(rows)
    print(f"{a.market}: {n} triggers, {len({r[0] for r in rows})} days; "
          f"placement latency {LAT}s; shipped wait {S.cross_limit_wait_s}s\n")
    print(f"  {'wait(s)':>8}{'fills':>7}{'fill%':>7}{'win':>7}{'mean px':>9}"
          f"   growth / day-t / drop-best")
    for w in (0.5, 1.0, 2.0, 2.5, 3.0, 5.0, 10.0, 20.0, 30.0, 45.0):
        got = [x for x in (run(row, w, a.price) for row in rows) if x is not None]
        if len(got) < 10:
            print(f"  {w:>8.1f}{len(got):>7}  (too few)")
            continue
        t = pd.DataFrame(got, columns=['day', 'won', 'ask'])
        g, tt, dd = score(t)
        mark = "  <- SHIPPED" if w == S.cross_limit_wait_s else ""
        print(f"  {w:>8.1f}{len(got):>7}{100*len(got)/n:>7.1f}{t.won.mean():>7.3f}"
              f"{t.ask.mean():>9.3f}   {g:+.3f} / {tt:+.2f} / {dd:+.3f}{mark}")
    print("\n  per-day log sums, wait 2 vs 20 (stability check):")
    for w in (2.0, 20.0):
        got = [x for x in (run(row, w, a.price) for row in rows) if x is not None]
        t = pd.DataFrame(got, columns=['day', 'won', 'ask'])
        fill = np.clip(t.ask.to_numpy(), 1e-4, .9999)
        r = np.where(t.won.to_numpy() == 1, (1 - fill) / fill * (1 - FEE), -1.0)
        lg = pd.Series(np.log1p(np.clip(FRAC * r, -0.999999, None)),
                       index=t.day.to_numpy()).groupby(level=0).sum()
        print(f"   wait {w:>4.1f}: " + " ".join(f"{d}:{v:+.2f}" for d, v in lg.items()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
