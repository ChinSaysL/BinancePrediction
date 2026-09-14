"""Hedge to LOCK, not to rescue: buy the other side while it is still cheap.

The operator's idea was "when a trade reverses against us, buy the other side so
losses are almost always 0". Measured as stated that loses -- by the time spot
has crossed back, the other side IS the favourite and the pair costs a mean
1.383 for something that pays 1.000, a locked 27.7% toll (see the reversal
sections in FINDINGS).

But the mechanic is sound and the timing was the problem. A hedged pair always
pays exactly 1.000, so what matters is only what the PAIR cost:

    entry_px + opposite_px  <  1.0   ->  locked PROFIT of the difference
    entry_px + opposite_px  >  1.0   ->  locked LOSS

Hedging while LOSING is the second case by construction. Hedging while WINNING
is the first: if we bought Up at 0.50 and Down has since fallen to 0.30, buying
Down locks +0.20 a share and the round can no longer lose.

So this walks every cross entry forward and hedges the moment the pair is
buyable under `1 - margin`, then compares against holding to expiry. Shares are
matched (the hedge buys the same share count), because that is what makes the
payout exactly 1.000 regardless of the outcome.

Costs modelled: the hedge pays the fee too, and it pays the ASK, not the mid.
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

SLIP = {'book': lambda q: q,
        'additive': lambda q: q + 0.088,
        'reverting': lambda q: 0.47 * q + 0.40}
FEE = 200 / 1e4
FRAC = 0.0672


def entries(q, rounds, market):
    """Every cross entry, with the OPPOSITE side's forward ask path."""
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
            if G > 0 and 1e4 * (np.nanmax(seen) - np.nanmin(seen)) / K > G:
                continue
            ask = ua[i] if side == 'Up' else da[i]
            if not np.isfinite(ask) or not (S.cross_min_price <= ask <= S.cross_max_price):
                continue
            opp = (da if side == 'Up' else ua)[i:]
            opp = opp[np.isfinite(opp)]
            out.append((pd.Timestamp(ts[i], unit='ms', tz='UTC').strftime('%Y-%m-%d'),
                        side, float(ask), int(side == rr.winner), opp))
            took, n = side, n + 1
    return out


def run(rows, margin, model):
    """Return per-trade net return on the ENTRY stake."""
    day, ret, locked = [], [], 0
    f = SLIP[model]
    for d, _side, ask, won, opp in rows:
        entry = float(np.clip(f(ask), 1e-4, .9999))
        # The ENTRY is slipped by the model -- it is an urgent marketable order
        # chasing a cross. The HEDGE is not: nothing is racing, so it can rest a
        # limit and pay the ask. Slipping it with the cross arm's own numbers
        # (+0.088 additive, or 0.47q+0.40 which turns a 0.30 opposite into
        # 0.541) prices a patient order as an urgent one and was what made this
        # look impossible on the first run.
        hedged = None
        if len(opp):
            cost = np.clip(opp, 1e-4, .9999)
            hit = np.where(entry + cost <= 1.0 - margin)[0]
            if len(hit):
                hedged = float(cost[hit[0]])
        if hedged is not None:
            # matched shares: outlay entry+hedge per share, payout exactly 1
            r = ((1.0 - entry - hedged) / entry) * (1 - FEE)
            locked += 1
        else:
            r = (1 - entry) / entry * (1 - FEE) if won else -1.0
        day.append(d); ret.append(r)
    t = pd.DataFrame({'day': day, 'ret': ret})
    lg = np.log1p(np.clip(FRAC * t.ret.to_numpy(), -0.999999, None))
    per = pd.Series(lg, index=t.day.to_numpy()).groupby(level=0).sum()
    sd = per.std(ddof=1)
    tt = per.mean() / (sd / np.sqrt(len(per))) if sd else np.nan
    return lg.sum(), tt, per.drop(per.idxmax()).sum(), locked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    ap.add_argument('--market', default='btc-updown-5m')
    a = ap.parse_args()
    q = pd.read_csv(f'{a.base}/quotes_arm.csv.gz')
    q = q[q.slug.str.startswith(a.market)]
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    rounds = {t.slug: t for t in r.itertuples()}
    rows = entries(q, rounds, a.market)
    print(f"{a.market}: {len(rows)} cross entries with a forward opposite book\n")

    # how often is the pair EVER buyable under 1.0?
    best = []
    for _d, _s, ask, _w, opp in rows:
        best.append(ask + float(np.min(opp)) if len(opp) else np.nan)
    best = np.array(best, float)
    ok = np.isfinite(best)
    print(f"  cheapest pair cost reachable after entry (entry ask + best opposite):")
    print(f"    median {np.nanmedian(best):.3f}   "
          f"under 1.00 on {100*np.mean(best[ok] < 1.0):.0f}% of entries   "
          f"under 0.95 on {100*np.mean(best[ok] < 0.95):.0f}%")
    print(f"    (a pair pays exactly 1.000, so anything above 1.000 is a locked loss)\n")

    print(f"  {'rule':<26}{'locked':>8}   "
          + "".join(f"{m+' g/t/drop':<24}" for m in ('book', 'additive', 'reverting')))
    for lab, margin in (("no hedge (shipped)", None), ("lock when pair<1.00", 0.0),
                        ("lock when pair<0.98", 0.02), ("lock when pair<0.95", 0.05),
                        ("lock when pair<0.90", 0.10)):
        cells, lk = [], 0
        for model in ('book', 'additive', 'reverting'):
            if margin is None:
                g, tt, dd, _ = run(rows, -9.0, model)   # never triggers
                g, tt, dd, _ = run([(d, s, ask, w, np.array([]))
                                    for d, s, ask, w, _o in rows], 0.0, model)
            else:
                g, tt, dd, lk = run(rows, margin, model)
            cells.append(f"{g:+.3f}/{tt:+.2f}/{dd:+.3f}")
        shown = f"{lk}/{len(rows)}" if margin is not None else "-"
        print(f"  {lab:<26}{shown:>8}   " + "".join(f"{c:<24}" for c in cells))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
