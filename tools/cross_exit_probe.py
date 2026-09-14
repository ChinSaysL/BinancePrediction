"""If the position can be SOLD, is there an exit worth taking?

Every analysis in FINDINGS assumed this venue is buy-only. That was an
assumption, not a finding -- `get_quote(side="SELL")` is refused with
"You have exceeded your available shares", which is an INVENTORY error, not an
unsupported-operation error. The venue validated the request and rejected the
quantity.

If sells work, one thing changes immediately: a losing cross position does not
have to lose its whole stake. Today's losses were -$3.20, -$6.30 and -$6.04 --
all -100% of stake, because the arm holds to settlement by construction.

This measures the exit that was available. Selling means hitting the BID, so a
position entered at the ask and exited at the bid pays the spread twice; the
question is whether cutting a loser early beats that, or whether it is the
hedging result again in another costume (certainty bought at a price above what
it is worth).

Rules tested, all causal and evaluated at the bid:
    hold              the shipped behaviour -- to settlement
    stop at X         sell when the bid falls X below the entry price
    trail from peak   sell when the bid falls X from its best since entry

Priced with the model arm's MEASURED patient slippage on the way in, and at the
raw bid on the way out (a sale crosses to the bid by definition).
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

FEE = 200 / 1e4
FRAC = 0.0672


def entries(q, rounds, market):
    """Cross entries with the forward BID path on the side we hold."""
    out = []
    G = cross_knob(market, "max_round_range_bps")
    for slug, g in q.groupby('slug', sort=False):
        rr = rounds.get(slug)
        if rr is None or not np.isfinite(rr.strike) or not isinstance(rr.winner, str):
            continue
        g = g.sort_values('ts_ms')
        ts = g.ts_ms.to_numpy(float); sp = g.spot.to_numpy(float)
        ua = g.up_ask.to_numpy(float); da = g.down_ask.to_numpy(float)
        ub = g.up_bid.to_numpy(float); db = g.down_bid.to_numpy(float)
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
            ask = ua[i] if side == 'Up' else da[i]
            if not np.isfinite(ask) or not (S.cross_min_price <= ask <= S.cross_max_price):
                continue
            bids = (ub if side == 'Up' else db)[i + 1:]
            bids = bids[np.isfinite(bids)]
            out.append((pd.Timestamp(ts[i], unit='ms', tz='UTC').strftime('%Y-%m-%d'),
                        slug, side, float(ask), bids, int(side == rr.winner)))
            took, n, last = side, n + 1, ts[i]
    return out


def run(rows, rule, param):
    day, ret = [], []
    exits = 0
    for d, _slug, _side, ask, bids, won in rows:
        entry = float(np.clip(ask + 0.0037, 1e-4, .9999))
        sold = None
        if len(bids):
            if rule == 'stop':
                hit = np.where(bids <= entry - param)[0]
                if len(hit):
                    sold = float(bids[hit[0]])
            elif rule == 'trail':
                peak = np.maximum.accumulate(bids)
                hit = np.where(bids <= peak - param)[0]
                if len(hit):
                    sold = float(bids[hit[0]])
        if sold is not None:
            r = (sold - entry) / entry * (1 - FEE)
            exits += 1
        else:
            r = (1 - entry) / entry * (1 - FEE) if won else -1.0
        day.append(d); ret.append(r)
    t = pd.DataFrame({'day': day, 'ret': ret})
    lg = np.log1p(np.clip(FRAC * t.ret.to_numpy(), -0.999999, None))
    per = pd.Series(lg, index=t.day.to_numpy()).groupby(level=0).sum()
    sd = per.std(ddof=1)
    tt = per.mean() / (sd / np.sqrt(len(per))) if sd and len(per) > 2 else np.nan
    return t.ret.mean(), lg.sum(), tt, exits


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
    print(f"{a.market}: {len(rows)} cross entries with a forward bid path\n")

    spreads = []
    for _d, _s, _side, ask, bids, _w in rows:
        if len(bids):
            spreads.append(ask - bids[0])
    print(f"  entry ask minus the bid you would sell into: "
          f"median {np.median(spreads):+.4f}  (the round trip you pay)\n")

    print(f"  {'rule':<20}{'exits':>7}{'EV/$1':>9}{'growth':>9}{'day-t':>8}")
    ev, g, tt, ex = run(rows, 'hold', 0)
    print(f"  {'hold (shipped)':<20}{ex:>7}{ev:>+9.4f}{g:>+9.3f}{tt:>+8.2f}")
    for p in (0.05, 0.10, 0.15, 0.25):
        ev, g, tt, ex = run(rows, 'stop', p)
        print(f"  {f'stop at -{p:.2f}':<20}{ex:>7}{ev:>+9.4f}{g:>+9.3f}{tt:>+8.2f}")
    for p in (0.05, 0.10, 0.20):
        ev, g, tt, ex = run(rows, 'trail', p)
        print(f"  {f'trail -{p:.2f} from peak':<20}{ex:>7}{ev:>+9.4f}{g:>+9.3f}{tt:>+8.2f}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
