"""What does trading six families at once actually look like, hour by hour?

Widening the cross arm from BTC-5m to every family in CROSS_TRADED raises two
questions that per-market win rates cannot answer:

  1. CONCURRENCY. There is no concurrent-exposure limit in the bot
     (stake_cap_usdt 0, cross_max_per_day 0), and six families at five entries
     a round is up to thirty open positions at 6.72% of bankroll each. How many
     are actually open at once?

  2. CORRELATION. BTC, ETH and BNB cross their strikes on the same move, and
     btc-updown-5m / btc-updown-15m are the SAME ASSET on two clocks. If the
     open positions are usually the same side at the same time, six families is
     one directional bet held six times, and the aggregate Kelly -- not the
     per-trade one -- is what binds.

Measured, not assumed: replay the shipped gate on every recorded family, hold
each entry to its own round's expiry, and walk a clock over the result.

Only the WIN RATE is real here; the other families have almost no recorded ask,
so stake is treated as notional and the P/L column uses BTC's fill distribution
as a stand-in. Read the concurrency and correlation columns, not the dollars.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import CROSS_TRADED, settings as S   # noqa: E402
from btcpredict.config import cross_knob                    # noqa: E402
from btcpredict.main import crossed_side                    # noqa: E402


def triggers(q, rounds):
    """Every entry the shipped gate takes, per family, with its expiry."""
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
            if rng_gate > 0 and 1e4 * (np.nanmax(seen) - np.nanmin(seen)) / K > rng_gate:
                continue
            out.append((pref, slug, float(ts[i]), end, side,
                        int(side == rr.winner)))
            took, n = side, n + 1
    return pd.DataFrame(out, columns=['pref', 'slug', 'open_ms', 'close_ms',
                                      'side', 'won'])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    # THE DEPLOYED value, read off the box. config.py's default is 0.04 and the
    # .env that trades sets 0.0224 -- using the default here overstates every
    # exposure figure by 1.8x, which is the error FINDINGS records twice.
    ap.add_argument('--stake-fraction', type=float, default=0.0224)
    a = ap.parse_args()
    frac = a.stake_fraction * S.cross_stake_mult
    q = pd.read_csv(f'{a.base}/quotes_full.csv.gz')
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    rounds = {t.slug: t for t in r.itertuples()}
    t = triggers(q, rounds)
    if t.empty:
        print("no triggers")
        return 1
    t['day'] = pd.to_datetime(t.open_ms, unit='ms', utc=True).dt.strftime('%Y-%m-%d')
    print(f"triggers {len(t)} across {t.pref.nunique()} families, "
          f"{t.day.nunique()} days\n")
    print(f"  {'family':<18}{'trig':>6}{'win':>7}")
    for pref, g in t.groupby('pref'):
        print(f"  {pref:<18}{len(g):>6}{g.won.mean():>7.3f}")

    # --- concurrency: walk a 1s clock and count live positions --------------
    ev = ([(o, +1, s, p) for o, s, p in zip(t.open_ms, t.side, t.pref)]
          + [(c, -1, s, p) for c, s, p in zip(t.close_ms, t.side, t.pref)])
    ev.sort()
    cur, peak, series = 0, 0, []
    for _ms, d, _s, _p in ev:
        cur += d
        peak = max(peak, cur)
        series.append(cur)
    series = np.array([x for x in series if x > 0])
    print(f"\n  === CONCURRENCY (positions open at the same instant) ===")
    print(f"    peak {peak}   median while live {np.median(series):.0f}   "
          f"p90 {np.percentile(series, 90):.0f}   p99 {np.percentile(series, 99):.0f}")
    print(f"    at {100 * frac:.2f}% of bankroll each:")
    for lab, v in (("peak", peak), ("p99", np.percentile(series, 99)),
                   ("p90", np.percentile(series, 90))):
        print(f"      {lab:<5} {v:>3.0f} open -> "
              f"{100 * v * frac:>5.1f}% of bankroll at risk")

    # --- correlation: when several are open, are they the same side? --------
    print(f"\n  === CORRELATION (are simultaneous positions the same bet?) ===")
    t = t.sort_values('open_ms')
    same = tot = 0
    worst = 0
    for row in t.itertuples():
        live = t[(t.open_ms <= row.open_ms) & (t.close_ms > row.open_ms)]
        if len(live) < 2:
            continue
        tot += 1
        share = (live.side == live.side.mode().iloc[0]).mean()
        same += share
        worst = max(worst, len(live))
    print(f"    overlapping moments {tot}   mean share on the SAME side "
          f"{same / max(tot, 1):.3f}   (0.5 = independent, 1.0 = one bet)")

    # --- the whole-account swing: settle every open position together -------
    print(f"\n  === WORST CORRELATED MOMENT ===")
    grp = t.groupby(t.close_ms // 1000)
    losses = grp.apply(lambda g: (g.won == 0).sum(), include_groups=False)
    print(f"    most simultaneous LOSSES at one expiry: {losses.max()}  "
          f"-> {100 * losses.max() * frac:.1f}% "
          f"of bankroll lost at once")
    print(f"    daily loss cap is {100 * S.daily_loss_cap_pct:.0f}%")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
