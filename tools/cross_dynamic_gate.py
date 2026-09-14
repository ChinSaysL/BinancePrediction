"""Should the round-range gate adapt to the prevailing regime?

The gate is a fixed number of bps. It was set so it admits 59.5% of that
family's rounds AT THE VOLATILITY OF THE WEEK IT WAS MEASURED ON -- which means
its selectivity drifts with the market. Measured 2026-09-14 18:49, after two
hours at roughly double baseline range:

    market            gate   last 2h    base   ratio   over gate now
    btc-updown-5m     10.0      14.4     8.5    1.69        84%
    eth-updown-5m     12.9      19.8    11.0    1.80        79%
    bnb-updown-5m      7.8      12.8     6.1    2.09       100%
    btc-updown-15m    13.6      25.0    12.6    1.99       100%

A gate designed to refuse 40% was refusing 84-100%, and the arm took one trade
in 95 minutes across six families.

That is not obviously WRONG -- "calm rounds only" may genuinely mean calm in
absolute terms, and a gate that widens in a storm is a gate that trades the
storm. So this measures it rather than assuming. Three rules:

    static     refuse if range_bps > G                       (shipped)
    trailing   refuse if range_bps > k * median(last N rounds of THIS market)
    sigma      refuse if range_bps > k * sigma_1m * sqrt(elapsed/60)

Both dynamic rules are strictly CAUSAL: the trailing median uses only rounds
that had already CLOSED when the trigger fired, and sigma comes from the
VolEstimator replayed over bars up to that instant.

Scored the way every gate in this repo is scored: compounded growth under BOTH
slippage models, day-clustered t, drop-best-day. Win rate would be misleading
because a looser gate also changes WHICH prices get taken.
"""
from __future__ import annotations

import argparse
import math
import os
import statistics
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import cross_knob, settings as S   # noqa: E402
from btcpredict.main import crossed_side                  # noqa: E402
from btcpredict.pricing.vol import VolEstimator           # noqa: E402

SLIP = {'book': lambda q: q,
        'additive': lambda q: q + 0.088,
        'reverting': lambda q: 0.47 * q + 0.40}
FEE = 200 / 1e4
FRAC = 0.0672


def sigma_series(bars):
    vol = VolEstimator()
    opens, sig = [], []
    prev, prev_day = None, None
    for t in bars.itertuples():
        ts, cl = int(t.open_ms), float(t.close)
        day = ts // 86400000
        if day != prev_day:
            vol._recompute_seasonality()
            prev_day = day
        if prev and prev > 0 and cl > 0:
            vol.add(math.log(cl / prev), hour=int((ts // 3600000) % 24))
        prev = cl
        opens.append(ts)
        sig.append(vol.sigma_1m(hour=int((ts // 3600000) % 24)))
    return np.array(opens, dtype=np.int64), np.array(sig)


def collect(q, rounds, opens, sig, market):
    """Every trigger BEFORE any range gate, with what each rule would need."""
    out = []
    hist: list[float] = []          # full range of rounds already CLOSED
    for slug in sorted(q.slug.unique(),
                       key=lambda s: rounds[s].start_ms if s in rounds else 0):
        rr = rounds.get(slug)
        if rr is None or not np.isfinite(rr.strike) or not isinstance(rr.winner, str):
            continue
        g = q[q.slug == slug].sort_values('ts_ms')
        ts = g.ts_ms.to_numpy(float); sp = g.spot.to_numpy(float)
        ua = g.up_ask.to_numpy(float); da = g.down_ask.to_numpy(float)
        K = float(rr.strike); end = float(rr.end_ms); start = float(rr.start_ms)
        # the trailing median of PRIOR rounds only -- never this one
        trail = statistics.median(hist[-40:]) if len(hist) >= 8 else None
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
            ask = ua[i] if side == 'Up' else da[i]
            if not np.isfinite(ask) or not (S.cross_min_price <= ask <= S.cross_max_price):
                continue
            seen = sp[:i + 1]
            rng = 1e4 * (np.nanmax(seen) - np.nanmin(seen)) / K
            k = int(np.searchsorted(opens, ts[i], side='right')) - 1
            sg = float(sig[k]) if 0 <= k < len(sig) else 0.0
            elapsed = max((ts[i] - start) / 1000.0, 1.0)
            # expected range over the elapsed part of the round, in bps
            exp_bps = 1e4 * sg * math.sqrt(elapsed / 60.0) if sg > 0 else None
            out.append((pd.Timestamp(ts[i], unit='ms', tz='UTC').strftime('%Y-%m-%d'),
                        rng, trail, exp_bps, ask, int(side == rr.winner)))
            took, n = side, n + 1
        full = 1e4 * (np.nanmax(sp) - np.nanmin(sp)) / K
        if np.isfinite(full):
            hist.append(full)
    return pd.DataFrame(out, columns=['day', 'rng', 'trail', 'exp', 'ask', 'won'])


def score(t):
    if len(t) < 10 or t.day.nunique() < 3:
        return None
    cells = []
    for m in SLIP:
        fill = np.clip(SLIP[m](t.ask.to_numpy()), 1e-4, .9999)
        r = np.where(t.won.to_numpy() == 1, (1 - fill) / fill * (1 - FEE), -1.0)
        lg = np.log1p(np.clip(FRAC * r, -0.999999, None))
        per = pd.Series(lg, index=t.day.to_numpy()).groupby(level=0).sum()
        sd = per.std(ddof=1)
        tt = per.mean() / (sd / np.sqrt(len(per))) if sd else np.nan
        cells.append((lg.sum(), tt, per.drop(per.idxmax()).sum()))
    return cells


def row(lab, t, n_all):
    s = score(t)
    if s is None:
        print(f"  {lab:<26}{len(t):>5}  (too few)")
        return
    print(f"  {lab:<26}{len(t):>5}{100*len(t)/n_all:>7.0f}%{t.won.mean():>7.3f}"
          f"{t.ask.mean():>7.3f}   "
          + "".join(f"{f'{g:+.3f}/{tt:+.2f}/{d:+.3f}':<24}" for g, tt, d in s))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    ap.add_argument('--market', default='btc-updown-5m')
    a = ap.parse_args()
    q = pd.read_csv(f'{a.base}/quotes_arm.csv.gz')
    q = q[q.slug.str.startswith(a.market)]
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    rounds = {t.slug: t for t in r.itertuples()}
    bars = pd.read_csv(f'{a.base}/bars.csv.gz').sort_values('open_ms')
    opens, sig = sigma_series(bars)
    t = collect(q, rounds, opens, sig, a.market)
    n = len(t)
    G = cross_knob(a.market, "max_round_range_bps")
    print(f"{a.market}: {n} triggers before any range gate, "
          f"{t.day.nunique()} days\n")
    print(f"  {'rule':<26}{'n':>5}{'kept':>8}{'win':>7}{'ask':>7}   "
          + "".join(f"{m+' g/t/drop':<24}" for m in SLIP))
    row("no gate", t, n)
    row(f"STATIC {G} bps (shipped)", t[t.rng <= G], n)
    for k in (1.0, 1.2, 1.4, 1.7, 2.0):
        sub = t[t.trail.notna() & (t.rng <= k * t.trail)]
        row(f"trailing median x{k}", sub, n)
    for k in (1.0, 1.5, 2.0, 2.5, 3.0):
        sub = t[t.exp.notna() & (t.rng <= k * t.exp)]
        row(f"sigma x{k}", sub, n)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
