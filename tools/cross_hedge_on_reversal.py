"""The hedge gate, as stated: when a cross trade reverses on us, buy the other
side to minimize losses (2026-09-15).

Measured exactly as stated, on the recorded venue book: the reversal is the
first OPPOSITE crossing after entry (the same detector the arm uses, opposite
sign), and the hedge is paid at the opposite side's ASK at that tick. That is
the BEST case -- the tick's raw recorded ask. The cross arm's own slippage
models are also printed, because the hedge leg is chasing a just-moved market
and would face the same fill race the entries do.

It loses, for three reasons that were already on the board:

  * the pair after a reversal costs a mean >1.0 for something that pays
    exactly 1.000 -- a locked toll per leg. "Losses are almost always 0" is
    really "the loss is locked at the current mark, minus one more spread";
  * the algebra: with the first leg sunk, EV(take) - EV(decline) =
    stake_B * [(1-p)/b - 1]. The entry price CANCELS. The hedge is
    economically the same trade as the reversal entry itself, which the
    corpus scores at day-t -3.05 (n=249) and `cross_allow_reversal=False`
    already refuses;
  * the live guard `max_paired_price = 1.0` refuses any order that would hold
    both sides above break-even -- which is the typical reversal case.

The other variant -- lock while WINNING when the pair goes cheap -- is
`tools/cross_lock_hedge.py`; on the same rows it also loses (its lock rule
caps the 88% win distribution). Control here: hold to settlement.
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

SLIP = {'book': lambda q: q,
        'additive': lambda q: q + 0.088,
        'reverting': lambda q: 0.47 * q + 0.40}
FEE = 200 / 1e4
FRAC = 0.0672


def collect(q, rounds, market):
    """Shipped-config entries + the first opposite crossing after each."""
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
            # first opposite crossing after the entry tick
            rev_px, rev_tau = np.nan, np.nan
            for j in range(i + 1, len(ts)):
                tau_j = (end - ts[j]) / 1000.0
                if tau_j <= 0:
                    break
                j0j = int(np.searchsorted(ts, ts[j] - (S.cross_look_s + 2.0) * 1000.0, 'left'))
                if j - j0j < 1:
                    continue
                s2 = crossed_side([(int(a), float(b)) for a, b in
                                   zip(ts[j0j:j + 1], sp[j0j:j + 1])], K, S.cross_look_s)
                if s2 is not None and s2 != side:
                    opp = (da if side == 'Up' else ua)[j]
                    if np.isfinite(opp):
                        rev_px, rev_tau = float(opp), tau_j
                    break
            out.append(dict(day=pd.Timestamp(ts[i], unit='ms', tz='UTC').strftime('%Y-%m-%d'),
                            side=side, entry=float(ask), won=int(side == rr.winner),
                            rev_px=rev_px, rev_tau=rev_tau))
            took, n = side, n + 1
    return pd.DataFrame(out)


def policy_score(t, hedge_fn, model):
    """Compounded growth / day-t / drop-best for one policy, per the repo bar.
    Entry slipped by the model; the hedge leg priced by hedge_fn (raw ask by
    default -- generous to the hedge)."""
    f = SLIP[model]
    rets = []
    for r in t.itertuples():
        e = float(np.clip(f(r.entry), 1e-4, .9999))
        h = hedge_fn(r)
        if h is not None:
            rets.append(((1.0 - e - h) / e) * (1 - FEE))
        else:
            rets.append((1 - e) / e * (1 - FEE) if r.won else -1.0)
    lg = np.log1p(np.clip(FRAC * np.array(rets), -0.999999, None))
    per = pd.Series(lg, index=t.day.to_numpy()).groupby(level=0).sum()
    sd = per.std(ddof=1)
    tt = per.mean() / (sd / math.sqrt(len(per))) if sd else float('nan')
    return lg.sum(), tt, per.drop(per.idxmax()).sum()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    ap.add_argument('--market', default='btc-updown-5m')
    a = ap.parse_args()
    q = pd.read_csv(f'{a.base}/quotes_arm.csv.gz')
    q = q[q.slug.str.startswith(a.market)]
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    rounds = {t.slug: t for t in r.itertuples()}
    t = collect(q, rounds, a.market)
    rev = t[t.rev_px.notna()].copy()
    rev['pair'] = rev.entry + rev.rev_px
    rev['locked'] = (1.0 - rev.pair) / rev.entry * (1 - FEE)
    rev['hold'] = np.where(rev.won == 1, (1 - rev.entry) / rev.entry * (1 - FEE), -1.0)

    print(f"{a.market}: {len(t)} entries, {len(rev)} reversed before the round ended")
    if len(rev):
        print(f"  P(entry still wins | reversed) = {rev.won.mean():.3f}")
        print(f"  pair cost at the reversal: mean {rev.pair.mean():.3f}  "
              f"median {rev.pair.median():.3f}  "
              f"<1.0 on {100*np.mean(rev.pair < 1.0):.0f}% of reversals "
              f"(<1.1 on {100*np.mean(rev.pair < 1.1):.0f}%)")
        print(f"  mean return/$1 of hedging at the reversal:  {rev.locked.mean():+.3f}")
        print(f"  mean return/$1 of HOLDING through it:       {rev.hold.mean():+.3f}")
        print(f"  (hedge leg paid at the raw ask -- best case; "
              f"with the arm's own slippage it is worse)")
        print(f"  live guard check: max_paired_price=1.0 would REFUSE "
              f"{100*np.mean(rev.pair > 1.0):.0f}% of these hedge orders")

    print(f"\n  {'policy':<34}" + "".join(f"{m+' g/t/drop':<24}" for m in SLIP))
    hedge_fn = lambda r: (r.rev_px if np.isfinite(r.rev_px) else None)  # noqa: E731
    hold_fn = lambda r: None                                            # noqa: E731
    for lab, fn in (("hold to settlement (shipped)", hold_fn),
                    ("hedge at every reversal (as stated)", hedge_fn),
                    ("hedge, +0.088 slip on hedge leg", lambda r: (min(r.rev_px + 0.088, 0.999) if np.isfinite(r.rev_px) else None)),  # noqa: E731
                    ("hedge only if pair<1.0 (guard-allowed)", lambda r: (r.rev_px if np.isfinite(r.rev_px) and r.entry + r.rev_px < 1.0 else None))):  # noqa: E731
        cells = []
        for model in SLIP:
            g, tt, dd = policy_score(t, fn, model)
            cells.append(f"{g:+.3f}/{tt:+.2f}/{dd:+.3f}")
        print(f"  {lab:<34}" + "".join(f"{c:<24}" for c in cells))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
