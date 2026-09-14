"""The model arm's regime question, replayed the way the CROSS arm was.

SUPERSEDES tools/model_regime_rescue.py, whose method was wrong in two ways
that both flatter the arm:

  1. it scored only the 690 orders that actually FILLED. That is conditioned on
     survival -- an entry the arm wanted and could not get is invisible -- and
     the cross work established that unfilled attempts are not random (they are
     systematically the cheap, high-payoff ones).
  2. those 690 span several config eras. min_edge, the quote band, the haircut
     and the regime window itself all changed during that week, so the sample
     pools strategies that never coexisted. The repo already has a commit for
     this exact mistake ("Cut ab_report at the config change so it cannot pool
     eras").

Here every trigger the gate WOULD take is replayed from the recorded book under
ONE fixed configuration, priced at the ask that existed, exactly as the cross
sweeps were.

### The asymmetry that makes this arm harder to validate, stated once

The cross arm's trigger is QUOTE-FREE -- spot crossed the strike -- so its win
rate was measurable on all 31 days of Chainlink corpus, and that is where its
power came from. The model arm's trigger is `edge = fair - quote`: it cannot be
evaluated without a venue quote. So it is confined to the 7 recorded days and
the 5 priced corpus days, and NO amount of method fixes that. When this arm and
the cross arm show the same day-t, the cross number is the better supported one.
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings as S            # noqa: E402
from btcpredict.edge.signal import evaluate            # noqa: E402
from btcpredict.pricing.digital import fair_value      # noqa: E402
from btcpredict.pricing.vol import VolEstimator        # noqa: E402

SLIP = {'book': lambda q: q,
        'additive': lambda q: q + 0.088,
        'reverting': lambda q: 0.47 * q + 0.40}
FEE = 200 / 1e4
FRAC = 0.0224


def sigma_by_bar(bars):
    """Replay the shipped VolEstimator over the recorded 1m bars.

    Byte-for-byte the same procedure as tools/arms_recorded.sigma_by_bar --
    close-to-close log returns, hour-aware, fed in order, seasonality
    recomputed as the day rolls -- so this replay and that one cannot disagree
    about what the arm's sigma was.
    """
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


def sigma_at(opens, sig, ts):
    k = int(np.searchsorted(opens, ts, side='right')) - 1
    if k < 0:
        return float(S.vol_floor_1m)
    return float(sig[k])


def replay(q, rounds, opens, sig, rng_lo, rng_hi):
    """Every entry the model gate takes, under one fixed config."""
    out = []
    for slug, g in q.groupby('slug', sort=False):
        rr = rounds.get(slug)
        if rr is None:
            continue
        strike, winner, start_ms = rr
        if not strike or not np.isfinite(strike):
            continue
        b = g.sort_values('ts_ms')
        ts = b.ts_ms.to_numpy(float); tau = b.tau_s.to_numpy(float)
        spot = b.spot.to_numpy(float)
        upd = b.up_price.to_numpy(float); dnd = b.down_price.to_numpy(float)
        upa = b.up_ask.to_numpy(float); dna = b.down_ask.to_numpy(float)
        day = pd.Timestamp(start_ms, unit='ms', tz='UTC').strftime('%Y-%m-%d')
        taken = False
        for i in range(len(ts)):
            if taken:
                break                       # one position per round, as live
            if not (S.min_tau_s <= tau[i] <= S.max_tau_s) or tau[i] <= 0:
                continue
            if not (0 < upd[i] < 1 and 0 < dnd[i] < 1):
                continue
            # Causal range-so-far: round open -> THIS instant. Never the full
            # round, which is lookahead.
            seen = spot[:i + 1]
            rng = 1e4 * (np.nanmax(seen) - np.nanmin(seen)) / strike
            if not (rng_lo <= rng < rng_hi):
                continue
            sg = sigma_at(opens, sig, ts[i])
            try:
                fair = float(fair_value(spot[i], strike, tau[i], sg))
            except Exception:
                continue
            s = evaluate(fair, upd[i], dnd[i],
                         half_spread=S.assumed_half_spread,
                         safety_margin=S.safety_margin, min_edge=S.min_edge,
                         min_quote=S.min_quote, max_quote=S.max_quote,
                         edge_haircut=S.edge_haircut)
            if not s.act or s.side is None:
                continue
            ask = upa[i] if s.side == 'Up' else dna[i]
            if not (S.min_quote <= ask <= S.max_quote):
                continue
            out.append((day, slug, tau[i], s.side, ask, rng,
                        int(s.side == winner)))
            taken = True
    return pd.DataFrame(out, columns=['day', 'slug', 'tau', 'side', 'ask',
                                      'range_bps', 'won'])


def score(t, label):
    if len(t) < 5 or t.day.nunique() < 3:
        print(f"  {label:<20} n={len(t):<4} (too few)")
        return
    cells = []
    for m in SLIP:
        fill = np.clip(SLIP[m](t.ask.to_numpy()), 1e-4, .9999)
        r = np.where(t.won.to_numpy() == 1, (1 - fill) / fill * (1 - FEE), -1.0)
        lg = np.log1p(np.clip(FRAC * r, -0.999999, None))
        per = pd.Series(lg, index=t.day.to_numpy()).groupby(level=0).sum()
        sd = per.std(ddof=1)
        tt = per.mean() / (sd / np.sqrt(len(per))) if sd else np.nan
        cells.append(f"{lg.sum():+.3f}/{tt:+.2f}/{per.drop(per.idxmax()).sum():+.3f}")
    print(f"  {label:<20} n={len(t):<4} win={t.won.mean():.3f} ask={t.ask.mean():.3f} "
          f"edge={(t.won - t.ask).mean():+.3f}  " + "  ".join(f"{c:<24}" for c in cells))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    ap.add_argument('--defaults', action='store_true',
                    help='use config.py defaults instead of the DEPLOYED values')
    a = ap.parse_args()
    if not a.defaults:
        # THE DEPLOYED VALUES, read off the box. config.py's defaults are NOT
        # what trades: min_edge 0.12 vs 0.01, band [0.25,0.90] vs [0.45,0.80],
        # haircut 0.85 vs 1.0. Scoring the arm at its defaults is the exact
        # error FINDINGS records twice ("quoted config defaults not .env
        # values"), and it changes the answer -- at the defaults the gate fires
        # 989 times, at the live values it is a different strategy entirely.
        S.min_edge = 0.01
        S.min_quote = 0.45
        S.max_quote = 0.80
        S.edge_haircut = 1.0
        S.safety_margin = 0.01
        S.assumed_half_spread = 0.005
        S.min_tau_s = 60.0
        S.max_tau_s = 210.0
    q = pd.read_csv(f'{a.base}/quotes_arm.csv.gz')
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    bars = pd.read_csv(f'{a.base}/bars.csv.gz').sort_values('open_ms')
    rounds = {t.slug: (t.strike, t.winner, t.start_ms) for t in r.itertuples()
              if isinstance(t.winner, str)}
    opens, sig = sigma_by_bar(bars)
    print(f"book rows {len(q):,} | rounds with an outcome {len(rounds):,} | "
          f"sigma from {len(opens):,} bars")
    print(f"gate: tau {S.min_tau_s:.0f}-{S.max_tau_s:.0f} min_edge {S.min_edge} "
          f"band [{S.min_quote},{S.max_quote}] haircut {S.edge_haircut}")
    print(f"\n{'':58}" + "".join(f"{m+' g/t/drop':<26}" for m in SLIP))

    full = replay(q, rounds, opens, sig, 0.0, 1e9)
    score(full, "no regime gate")
    print()
    for lo, hi in ((0, 3), (3, 5), (5, 8), (8, 12), (12, 20), (20, 1e9)):
        t = full[(full.range_bps >= lo) & (full.range_bps < hi)]
        score(t, f"range {lo}-{hi if hi < 1e8 else 'inf'}")
    print()
    for lo, hi in ((5, 12), (3, 12), (5, 20), (3, 20), (8, 20)):
        t = full[(full.range_bps >= lo) & (full.range_bps < hi)]
        score(t, f"WINDOW {lo}-{hi}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
