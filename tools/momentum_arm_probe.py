"""Would a pure MOMENTUM arm work where the model arm does not?

The model arm is dead: its fair is dominated by the venue's quote, and at tau
60-210 its edge is the same size as the half-spread (+0.0057). The question
here is different -- not "predict the settlement better than the venue" but
"trade the underlying's own move and let the venue catch up", which is what the
cross arm does with a strike crossing.

There is prior evidence in both directions:
  * config.impulse_bps records "impulses of 4-8 bps paid +8% to +21% per $1 in
    BOTH halves" -- measured, and the reason the impulse arm exists.
  * the arm has ONE live fill ever (a loss), so it has no live record.
  * a 5-second trailing return does correlate with the venue's pricing error at
    tau 60-210 (+0.0775, CI [+0.0621,+0.0947]), but the venue reprices inside
    2 seconds, so that edge may not be reachable.

So: sweep the window and the threshold, at both tau bands, and price it under
every slippage model -- including the PATIENT one, because unlike the cross arm
a momentum trigger has no reason to race unless the decay test says it must.

One entry per round. Side = the direction of the move. No strike crossing
required, which is the whole point: the arm fires on the move itself.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SLIP = {'patient': lambda q: q + 0.0037,     # the model arm's MEASURED slippage
        'book': lambda q: q,
        'additive': lambda q: q + 0.088,     # the cross arm's, if it must race
        'reverting': lambda q: 0.47 * q + 0.40}
FEE = 200 / 1e4
FRAC = 0.0672


def entries(q, rounds, win_s, thr_bps, tau_lo, tau_hi, band=(0.10, 0.90)):
    out = []
    for slug, g in q.groupby('slug', sort=False):
        rr = rounds.get(slug)
        if rr is None or not isinstance(rr.winner, str):
            continue
        g = g.sort_values('ts_ms')
        ts = g.ts_ms.to_numpy(float); sp = g.spot.to_numpy(float)
        ua = g.up_ask.to_numpy(float); da = g.down_ask.to_numpy(float)
        end = float(rr.end_ms)
        for i in range(len(ts)):
            tau = (end - ts[i]) / 1000.0
            if not (tau_lo <= tau <= tau_hi):
                continue
            j = int(np.searchsorted(ts, ts[i] - win_s * 1000.0, 'left'))
            if j >= i or sp[j] <= 0:
                continue
            mv = 1e4 * (sp[i] - sp[j]) / sp[j]
            if abs(mv) < thr_bps:
                continue
            side = 'Up' if mv > 0 else 'Down'
            ask = ua[i] if side == 'Up' else da[i]
            if not np.isfinite(ask) or not (band[0] <= ask <= band[1]):
                continue
            out.append((pd.Timestamp(ts[i], unit='ms', tz='UTC').strftime('%Y-%m-%d'),
                        slug, side, float(ask), abs(mv), int(side == rr.winner)))
            break                                    # one entry per round
    return pd.DataFrame(out, columns=['day', 'slug', 'side', 'ask', 'mv', 'won'])


def score(t, model):
    fill = np.clip(SLIP[model](t.ask.to_numpy()), 1e-4, .9999)
    r = np.where(t.won.to_numpy() == 1, (1 - fill) / fill * (1 - FEE), -1.0)
    lg = np.log1p(np.clip(FRAC * r, -0.999999, None))
    per = pd.Series(lg, index=t.day.to_numpy()).groupby(level=0).sum()
    sd = per.std(ddof=1)
    tt = per.mean() / (sd / np.sqrt(len(per))) if sd and len(per) > 2 else np.nan
    return lg.sum(), tt, r.mean()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='data/recorded')
    ap.add_argument('--market', default='btc-updown-5m')
    a = ap.parse_args()
    q = pd.read_csv(f'{a.base}/quotes_arm.csv.gz')
    q = q[q.slug.str.startswith(a.market)]
    r = pd.read_csv(f'{a.base}/rounds.csv.gz')
    rounds = {t.slug: t for t in r.itertuples()}

    for tau_lo, tau_hi, lab in ((60, 210, "MODEL ARM window"),
                                (5, 60, "CROSS ARM window")):
        print(f"\n=== tau {tau_lo}-{tau_hi}s ({lab}) ===")
        print(f"  {'win':<5}{'thr':<6}{'n':>5}{'rounds':>7}{'win':>7}{'ask':>7}"
              f"{'edge':>8}{'EV/$1 patient':>15}{'growth/t patient':>19}")
        for win_s in (2, 5, 15, 30):
            for thr in (2.0, 4.0, 8.0):
                t = entries(q, rounds, win_s, thr, tau_lo, tau_hi)
                if len(t) < 25 or t.day.nunique() < 3:
                    continue
                g, tt, ev = score(t, 'patient')
                print(f"  {win_s:<5}{thr:<6.0f}{len(t):>5}{t.slug.nunique():>7}"
                      f"{t.won.mean():>7.3f}{t.ask.mean():>7.3f}"
                      f"{t.won.mean()-t.ask.mean():>+8.3f}{ev:>+15.4f}"
                      f"{f'{g:+.3f} / t{tt:+.2f}':>19}")

    print("\n=== best cell, priced under every slippage model ===")
    best = None
    for tau_lo, tau_hi in ((60, 210), (5, 60)):
        for win_s in (2, 5, 15, 30):
            for thr in (2.0, 4.0, 8.0):
                t = entries(q, rounds, win_s, thr, tau_lo, tau_hi)
                if len(t) < 25 or t.day.nunique() < 3:
                    continue
                g, tt, _ = score(t, 'patient')
                if best is None or g > best[0]:
                    best = (g, tau_lo, tau_hi, win_s, thr, t)
    if best:
        _, tl, th, w, thr, t = best
        print(f"  tau {tl}-{th}, window {w}s, threshold {thr:.0f}bps, n={len(t)}")
        for m in SLIP:
            g, tt, ev = score(t, m)
            print(f"    {m:<11} EV/$1 {ev:+.4f}   growth {g:+.3f}  day-t {tt:+.2f}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
