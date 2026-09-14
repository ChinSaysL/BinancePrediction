"""Does the cross signal work on the markets we record but do not trade?

The per-market question has been open since 2026-09-13 and was RETRACTED then,
because the only substrate was a month of 1s klines with the settlement
synthesized from spot -- and a synthesized settlement agrees with the venue only
64% of the time ON TRIGGERED ROUNDS, which is not recoverable at any sample
size. The multi-coin L2 set that replaced it had 50 triggers over 28 rounds.

Since 2026-09-13 the bot records all six families with the venue's OWN strike
and the venue's OWN winner. That fixes the settlement problem completely. What
it does NOT fix is pricing: the websocket book follows only the traded market,
so the recorded families carry spot but almost no ask --

    btc-updown-5m   330,763 ask rows of 361,634      eth-updown-5m  32,857 of 151,116
    eth-updown-15m        2 of  78,501               btc-updown-15m      2 of  78,749
    bnb-updown-5m         0 of  77,417               bnb-updown-15m      2 of   2,041

So this measures the WIN RATE, which needs no price, and stops there. A win rate
is half the answer: whether it pays also depends on the ask, and for four of the
six families that half cannot be measured yet at all. Treat a good win rate here
as a reason to START RECORDING THE BOOK for that family, never as a reason to
trade it.

Bootstrap is over ROUNDS. Triggers inside a round share one price path, and a
per-trigger CI is ~sqrt(k) too narrow -- the exact error that made the retracted
tables look decisive.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings as S      # noqa: E402
from btcpredict.main import crossed_side         # noqa: E402


def triggers_for(q, rounds, dur):
    out = []
    for slug, g in q.groupby('slug', sort=False):
        rr = rounds.get(slug)
        if rr is None or not np.isfinite(rr.strike) or not isinstance(rr.winner, str):
            continue
        g = g.sort_values('ts_ms')
        ts = g.ts_ms.to_numpy(float); sp = g.spot.to_numpy(float)
        K = float(rr.strike); end = float(rr.end_ms)
        took, n = None, 0
        for i in range(len(ts)):
            tau = (end - ts[i]) / 1000.0
            if not (S.cross_min_tau_s <= tau <= S.cross_max_tau_s):
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
            if 1e4 * abs(sp[i] - K) / K > S.cross_max_depth_bps:
                continue
            seen = sp[:i + 1]
            rg = 1e4 * (np.nanmax(seen) - np.nanmin(seen)) / K
            if S.cross_max_round_range_bps > 0 and rg > S.cross_max_round_range_bps:
                continue
            day = pd.Timestamp(ts[i], unit='ms', tz='UTC').strftime('%Y-%m-%d')
            out.append((day, slug, side, int(side == rr.winner)))
            took, n = side, n + 1
    return pd.DataFrame(out, columns=['day', 'slug', 'side', 'won'])


def boot(t, n=4000, seed=3):
    g = [x.won.to_numpy() for _, x in t.groupby('slug', sort=False)]
    rng = np.random.default_rng(seed)
    k = len(g)
    m = np.array([np.concatenate([g[j] for j in rng.integers(0, k, k)]).mean()
                  for _ in range(n)])
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    a = ap.parse_args()
    q = pd.read_csv(f'{a.base}/quotes_full.csv.gz')
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    q['pref'] = q.slug.str.rsplit('-', n=1).str[0]
    rounds = {t.slug: t for t in r.itertuples()}
    print(f"gate: tau {S.cross_min_tau_s:.0f}-{S.cross_max_tau_s:.0f} depth "
          f"{S.cross_max_depth_bps} look {S.cross_look_s} cap "
          f"{S.cross_max_per_round} range {S.cross_max_round_range_bps}\n")
    print(f"  {'market':<18}{'trig':>6}{'rounds':>8}{'days':>6}{'trig/day':>10}"
          f"{'win':>7}   {'95% CI (by round)':<22}{'ask rows':>9}")
    for pref, g in q.groupby('pref'):
        dur = 900 if pref.endswith('15m') else 300
        t = triggers_for(g, rounds, dur)
        if t.empty:
            print(f"  {pref:<18}{0:>6}   (no triggers)")
            continue
        lo, hi = boot(t)
        nd = t.day.nunique()
        asks = int(g.up_ask.notna().sum())
        print(f"  {pref:<18}{len(t):>6}{t.slug.nunique():>8}{nd:>6}{len(t)/nd:>10.1f}"
              f"{t.won.mean():>7.3f}   [{lo:.3f}, {hi:.3f}]{'':<8}{asks:>9,}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
