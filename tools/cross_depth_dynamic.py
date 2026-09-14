"""Does the DEPTH gate need to adapt to volatility? (2026-09-15)

Prompted by the live loss at 23:39 UTC: `btc-updown-5m-1789428900` Up filled
0.6165 (decided 0.561), depth +0.76 bps at the trigger, sigma ~3 bps/min, the
round settled Down -- -$3.07. Question: should `cross_max_depth_bps` (static
5.0, shipped 2026-09-13 on the 31-day sweep) become a function of volatility
or other factors?

Mirror of `cross_dynamic_gate.py` (which asked the same of the RANGE gate and
found no k beats static), on the same recorded venue book -- real asks, real
strikes. Depth is |spot - strike| at the trigger, in bps, and every rule is
strictly causal:

    static    keep depth <= 5.0                       (shipped)
    sig       keep depth <= k * sigma_1m_bps          (looser in storms)
    sig-cap   keep depth <= min(5.0, k * sigma_1m_bps)(tighter in calms)
    exp       keep depth <= k * sigma * sqrt(elapsed/60)  (expected range so far)
    trail     keep depth <= k * median(full range of the last 40 CLOSED rounds)

Also printed: the STATIC sweep (0.25 .. 8), because a cap tight enough to have
blocked the loss entry (<= 0.75) is the thing to cost out first; and the
depth x volatility cell table (win - ask per cell, which is the repo's bar).

Scored the way every gate in this repo is scored: compounded growth under both
slippage models on the real asks, day-clustered t, drop-best, and PAIRED BY
DAY against the shipped static gate. Win rate alone is misleading: a tighter
gate also changes WHICH prices get taken.
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
D_STATIC = float(S.cross_max_depth_bps)


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


def collect(q, rounds, opens, sig, market, apply_range):
    """Every trigger that passes the SHIPPED gates -- depth NOT applied here,
    it is the thing being tested -- with the features the rules need."""
    out = []
    hist: list[float] = []          # full range of rounds already CLOSED
    G = cross_knob(market, "max_round_range_bps")
    for slug in sorted(q.slug.unique(),
                       key=lambda s: rounds[s].start_ms if s in rounds else 0):
        rr = rounds.get(slug)
        if rr is None or not np.isfinite(rr.strike) or not isinstance(rr.winner, str):
            continue
        g = q[q.slug == slug].sort_values('ts_ms')
        ts = g.ts_ms.to_numpy(float); sp = g.spot.to_numpy(float)
        ua = g.up_ask.to_numpy(float); da = g.down_ask.to_numpy(float)
        K = float(rr.strike); end = float(rr.end_ms); start = float(rr.start_ms)
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
            ask = ua[i] if side == 'Up' else da[i]
            if not np.isfinite(ask) or not (S.cross_min_price <= ask <= S.cross_max_price):
                continue
            seen = sp[:i + 1]
            rng = 1e4 * (np.nanmax(seen) - np.nanmin(seen)) / K
            if apply_range and rng > G:
                continue
            depth = 1e4 * abs(sp[i] - K) / K
            k = int(np.searchsorted(opens, ts[i], side='right')) - 1
            sg = float(sig[k]) if 0 <= k < len(sig) else 0.0
            elapsed = max((ts[i] - start) / 1000.0, 1.0)
            exp = 1e4 * sg * math.sqrt(elapsed / 60.0) if sg > 0 else np.nan
            out.append((pd.Timestamp(ts[i], unit='ms', tz='UTC').strftime('%Y-%m-%d'),
                        depth, 1e4 * sg, exp, trail, ask, int(side == rr.winner)))
            took, n = side, n + 1
        full = 1e4 * (np.nanmax(sp) - np.nanmin(sp)) / K
        if np.isfinite(full):
            hist.append(full)
    return pd.DataFrame(out, columns=['day', 'depth', 'sig', 'exp', 'trail',
                                      'ask', 'won'])


def daily_lg(t):
    out = {}
    for m in SLIP:
        fill = np.clip(SLIP[m](t.ask.to_numpy()), 1e-4, .9999)
        r = np.where(t.won.to_numpy() == 1, (1 - fill) / fill * (1 - FEE), -1.0)
        lg = np.log1p(np.clip(FRAC * r, -0.999999, None))
        out[m] = pd.Series(lg, index=t.day.to_numpy()).groupby(level=0).sum()
    return out


def score_row(lab, t, n_all, t_base=None):
    if len(t) < 10 or t.day.nunique() < 3:
        print(f"  {lab:<22}{len(t):>5}  (too few)")
        return
    cells = []
    for m in SLIP:
        per = daily_lg(t)[m]
        sd = per.std(ddof=1)
        tt = per.mean() / (sd / np.sqrt(len(per))) if sd else float('nan')
        cells.append((per.sum(), tt, per.drop(per.idxmax()).sum()))
    paired = ""
    if t_base is not None:
        d0 = daily_lg(t)['reverting']; d1 = daily_lg(t_base)['reverting']
        days = sorted(set(d0.index) | set(d1.index))
        d = (d0.reindex(days, fill_value=0) - d1.reindex(days, fill_value=0)).to_numpy()
        sd = d.std(ddof=1)
        pt = d.mean() / (sd / math.sqrt(len(d))) if sd else float('nan')
        paired = f"   vs static(rev): {d.mean():+.3f} t{pt:+.2f}"
    edge = t.won.mean() - t.ask.mean()
    print(f"  {lab:<22}{len(t):>5}{100*len(t)/n_all:>7.0f}%{t.won.mean():>7.3f}"
          f"{t.ask.mean():>7.3f}{edge:>+8.3f}   "
          + "".join(f"{f'{g:+.3f}/{tt:+.2f}/{d:+.3f}':<22}" for g, tt, d in cells)
          + paired)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    ap.add_argument('--market', default='btc-updown-5m')
    ap.add_argument('--no-range', action='store_true',
                    help='collect BEFORE the range gate too (secondary read)')
    a = ap.parse_args()
    q = pd.read_csv(f'{a.base}/quotes_arm.csv.gz')
    q = q[q.slug.str.startswith(a.market)]
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    rounds = {t.slug: t for t in r.itertuples()}
    bars = pd.read_csv(f'{a.base}/bars.csv.gz').sort_values('open_ms')
    opens, sig = sigma_series(bars)
    t = collect(q, rounds, opens, sig, a.market, not a.no_range)
    n = len(t)
    rg = "" if not a.no_range else " (range gate OFF too)"
    print(f"{a.market}: {n} triggers, {t.day.nunique()} days{rg}; "
          f"static depth gate D={D_STATIC:g} bps; sigma {t.sig.median():.1f} bps/min median\n")
    hdr = (f"  {'rule':<22}{'n':>5}{'kept':>8}{'win':>7}{'ask':>7}{'edge':>8}   "
           + "".join(f"{m+' g/t/drop':<22}" for m in SLIP))
    print(hdr)

    print(" STATIC sweep (what a cap tight enough to block the loss costs):")
    for d in (0.25, 0.5, 0.75, 1.0, 2.0, 5.0, 8.0):
        score_row(f"depth <= {d:g}", t[t.depth <= d], n)

    base = t[t.depth <= D_STATIC]
    print("\n DYNAMIC rules (paired vs shipped static):")
    for k in (0.5, 1.0, 1.5, 2.0):
        score_row(f"sig x{k:g}", t[t.depth <= k * t.sig], n, base)
    for k in (1.0, 2.0, 3.0):
        score_row(f"min(D, sig x{k:g})",
                  t[t.depth <= np.minimum(D_STATIC, k * t.sig)], n, base)
    for k in (0.5, 1.0, 1.5, 2.0):
        score_row(f"exp x{k:g}", t[t.exp.notna() & (t.depth <= k * t.exp)], n, base)
    for k in (0.05, 0.1, 0.2, 0.3):
        score_row(f"trail x{k:g}",
                  t[t.trail.notna() & (t.depth <= k * t.trail)], n, base)

    print("\n depth x volatility cells (win / ask / edge):")
    hi = t.sig > t.sig.median()
    for lab, x in (("calm half  (sig <= med)", t[~hi]),
                   ("storm half (sig >  med)", t[hi])):
        if len(x) < 10:
            continue
        print(f"  {lab}: n={len(x)}")
        for d0, d1 in ((0, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 5.0), (5.0, 1e9)):
            c = x[(x.depth >= d0) & (x.depth < d1)]
            if len(c) == 0:
                continue
            print(f"    depth {d0:>4.2f}-{min(d1, 9.99):<4.2f} n={len(c):>3}  "
                  f"win {c.won.mean():.3f}  ask {c.ask.mean():.3f}  "
                  f"edge {c.won.mean()-c.ask.mean():+.3f}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
