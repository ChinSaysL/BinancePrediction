"""Does requiring a DEEPER cross avoid the reversals?

Prompted by btc-updown-5m-1789403700 on 2026-09-14: spot dipped 0.51 bps below
the strike at tau 58, the arm bought Down at a filled 0.5789, and the round
settled 1.37 bps ABOVE the strike. A marginal cross that immediately reversed.

The arm has a depth CEILING (`cross_max_depth_bps` 5.0 -- past that the venue
has repriced and the edge is gone) but no FLOOR, so a 0.01 bps nick counts the
same as a decisive move. Two things are tested here:

  1. a floor: refuse anything shallower than `min_depth`.
  2. sizing by depth instead of gating on it -- same triggers, stake scaled by
     how decisive the cross was.

Both are scored the same way every other knob in this repo is: compounded
growth under BOTH slippage models, day-clustered t, drop-best-day. Win rate
alone would be misleading here, because a floor also shifts WHICH prices get
taken, and the cheap end is where the payoff lives.

The win-rate half runs on every recorded family (the venue's own strike and
winner, no price needed); the money half is BTC-5m only, because the other
families have almost no recorded ask.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import CROSS_TRADED, cross_knob, settings as S  # noqa: E402
from btcpredict.main import crossed_side                              # noqa: E402

SLIP = {'book': lambda q: q,
        'additive': lambda q: q + 0.088,
        'reverting': lambda q: 0.47 * q + 0.40}
FEE = 200 / 1e4
FRAC = 0.0672


def scan(q, rounds, want_ask):
    """Every trigger the gate takes, tagged with its depth at entry."""
    out = []
    for slug, g in q.groupby('slug', sort=False):
        rr = rounds.get(slug)
        if rr is None or not np.isfinite(rr.strike) or not isinstance(rr.winner, str):
            continue
        pref = slug.rsplit('-', 1)[0]
        if pref not in CROSS_TRADED:
            continue
        rng_gate = cross_knob(pref, "max_round_range_bps")
        g = g.sort_values('ts_ms')
        ts = g.ts_ms.to_numpy(float); sp = g.spot.to_numpy(float)
        ua = g.up_ask.to_numpy(float) if want_ask else None
        da = g.down_ask.to_numpy(float) if want_ask else None
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
            depth = 1e4 * abs(sp[i] - K) / K
            if depth > S.cross_max_depth_bps:
                continue
            seen = sp[:i + 1]
            if rng_gate > 0 and 1e4 * (np.nanmax(seen) - np.nanmin(seen)) / K > rng_gate:
                continue
            ask = np.nan
            if want_ask:
                ask = ua[i] if side == 'Up' else da[i]
                if not np.isfinite(ask) or not (S.cross_min_price <= ask
                                                <= S.cross_max_price):
                    continue
            out.append((pd.Timestamp(ts[i], unit='ms', tz='UTC').strftime('%Y-%m-%d'),
                        pref, slug, side, depth, float(ask),
                        int(side == rr.winner)))
            took, n = side, n + 1
    return pd.DataFrame(out, columns=['day', 'pref', 'slug', 'side', 'depth',
                                      'ask', 'won'])


def growth(t, weights=None):
    """(growth, day-t, drop-best) per slippage model."""
    cells = []
    w = np.ones(len(t)) if weights is None else np.asarray(weights, float)
    for m in SLIP:
        fill = np.clip(SLIP[m](t.ask.to_numpy()), 1e-4, .9999)
        r = np.where(t.won.to_numpy() == 1, (1 - fill) / fill * (1 - FEE), -1.0)
        lg = np.log1p(np.clip(FRAC * w * r, -0.999999, None))
        per = pd.Series(lg, index=t.day.to_numpy()).groupby(level=0).sum()
        sd = per.std(ddof=1)
        tt = per.mean() / (sd / np.sqrt(len(per))) if sd and len(per) > 2 else np.nan
        cells.append((lg.sum(), tt, per.drop(per.idxmax()).sum() if len(per) > 1 else np.nan))
    return cells


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    a = ap.parse_args()
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    rounds = {t.slug: t for t in r.itertuples()}

    q = pd.read_csv(f'{a.base}/quotes_full.csv.gz')
    allm = scan(q, rounds, want_ask=False)
    print(f"=== WIN RATE by depth at entry -- every recorded family "
          f"({len(allm)} triggers, {allm.day.nunique()} days) ===")
    print(f"  {'depth band':<14}{'n':>6}{'win':>8}   per-family win")
    for lo, hi in ((0, .25), (.25, .5), (.5, 1), (1, 2), (2, 3.5), (3.5, 5.01)):
        b = allm[(allm.depth >= lo) & (allm.depth < hi)]
        if b.empty:
            continue
        per = "  ".join(f"{p.split('-')[0]}{g.won.mean():.2f}"
                        for p, g in b.groupby('pref') if len(g) >= 8)
        print(f"  {lo:.2f}-{hi:<9.2f}{len(b):>6}{b.won.mean():>8.3f}   {per}")

    qa = pd.read_csv(f'{a.base}/quotes_arm.csv.gz')
    qa = qa[qa.slug.str.startswith('btc-updown-5m')]
    btc = scan(qa, rounds, want_ask=True)
    print(f"\n=== BTC-5m WITH PRICES: a depth FLOOR ({len(btc)} triggers) ===")
    print(f"  {'min depth':<11}{'n':>5}{'win':>7}{'ask':>7}   "
          + "".join(f"{m+' g/t/drop':<24}" for m in SLIP))
    for md in (0.0, 0.10, 0.25, 0.40, 0.60, 0.80, 1.00, 1.50):
        b = btc[btc.depth >= md]
        if len(b) < 10 or b.day.nunique() < 3:
            print(f"  {md:<11.2f}{len(b):>5}  (too few)")
            continue
        cs = growth(b)
        mark = "  <- SHIPPED (no floor)" if md == 0.0 else ""
        print(f"  {md:<11.2f}{len(b):>5}{b.won.mean():>7.3f}{b.ask.mean():>7.3f}   "
              + "".join(f"{f'{g:+.3f}/{t:+.2f}/{d:+.3f}':<24}" for g, t, d in cs)
              + mark)

    print(f"\n=== instead of gating: SIZE by depth (same triggers) ===")
    print(f"  {'rule':<22}{'n':>5}{'mean w':>8}   "
          + "".join(f"{m+' g/t/drop':<24}" for m in SLIP))
    rules = {
        'flat (shipped)':      np.ones(len(btc)),
        'x0.5 under 0.5bps':   np.where(btc.depth < 0.5, 0.5, 1.0),
        'x0.5 under 1.0bps':   np.where(btc.depth < 1.0, 0.5, 1.0),
        'x0.25 under 0.5bps':  np.where(btc.depth < 0.5, 0.25, 1.0),
        'linear depth/1.0':    np.clip(btc.depth / 1.0, 0.25, 1.0),
        'linear depth/2.0':    np.clip(btc.depth / 2.0, 0.25, 1.0),
    }
    for lab, w in rules.items():
        cs = growth(btc, w)
        print(f"  {lab:<22}{len(btc):>5}{np.mean(w):>8.3f}   "
              + "".join(f"{f'{g:+.3f}/{t:+.2f}/{d:+.3f}':<24}" for g, t, d in cs))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
