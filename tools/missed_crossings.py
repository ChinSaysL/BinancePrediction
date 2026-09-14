"""Did the live cross arm miss any real crossing?

For every btc-updown-5m round in a window, replay the arm's own trigger against
the VENUE's strike (`rounds.strike`, the Chainlink number the market settles on
-- NOT a spot-derived reconstruction) and Binance 1s klines, then diff the
result against what the bot actually logged.

Two detectors, because they answer different questions:

  CLOSE  close-to-close with the arm's look-back rule. This is what
         `crossed_side` approximates: compare the newest sample with one
         `cross_look_s` old and require a sign change.
  TOUCH  ANY excursion of the 1s high/low through the strike. This is the true
         set of crossings that existed. The live arm reads every websocket tick,
         so a crossing inside a second IS visible to it -- but only if a
         `_cross_tick` happens to run while price is on the far side.

TOUCH-but-not-CLOSE is therefore the interesting bucket: a real crossing that
the two-point comparison can step over.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings as S     # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=1))         # operator's clock


def fmt(ms):
    return dt.datetime.fromtimestamp(ms / 1000, TZ).strftime('%m-%d %H:%M:%S')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=None)
    ap.add_argument("--klines", default=None)
    a = ap.parse_args()
    base = os.environ.get('SCRATCH', '.')
    d = json.load(open(a.data or f'{base}/xwin.json'))
    K = json.load(open(a.klines or f'{base}/klines.json'))

    kt = np.array([r[0] for r in K], dtype='int64')
    ko = np.array([r[1] for r in K]); kh = np.array([r[2] for r in K])
    kl = np.array([r[3] for r in K]); kc = np.array([r[4] for r in K])

    acted = {}
    for x in d['dec']:
        acted.setdefault(x['slug'], []).append(x)

    lo, hi = S.cross_min_tau_s, S.cross_max_tau_s
    look = S.cross_look_s
    dep = S.cross_max_depth_bps

    print(f"gate: tau {lo:.0f}-{hi:.0f}s  depth<={dep}bps  look {look}s  "
          f"| strike = the VENUE's (rounds.strike) | times UTC+1")
    print(f"{'round (local)':16s} {'strike':>10} {'CLOSE':>6} {'TOUCH':>6} "
          f"{'bot':>4}  note")

    tot_c = tot_t = tot_bot = 0
    missed = []
    for r in d['rounds']:
        if r['strike'] is None:
            continue
        K_ = float(r['strike']); end = r['end_ms']
        w0, w1 = end - int(hi * 1000), end - int(lo * 1000)
        m = (kt >= w0 - 5000) & (kt <= w1)
        if m.sum() < 5:
            continue
        t_, o_, h_, l_, c_ = kt[m], ko[m], kh[m], kl[m], kc[m]

        # CLOSE detector: sign change vs a sample look_s..look_s+2 old
        nc = 0
        side_c = np.sign(c_ - K_)
        for i in range(len(t_)):
            tau = (end - t_[i]) / 1000.0
            if not (lo <= tau <= hi):
                continue
            j = np.searchsorted(t_, t_[i] - int(look * 1000), side='right') - 1
            if j < 0 or (t_[i] - t_[j]) > (look + 2) * 1000:
                continue
            if side_c[i] == 0 or side_c[j] == 0 or side_c[i] == side_c[j]:
                continue
            if 1e4 * abs(c_[i] - K_) / K_ > dep:
                continue
            nc += 1
            if nc >= S.cross_max_per_round:
                break

        # TOUCH detector: did the bar's range straddle the strike at all?
        nt = 0
        for i in range(len(t_)):
            tau = (end - t_[i]) / 1000.0
            if not (lo <= tau <= hi):
                continue
            if l_[i] <= K_ <= h_[i]:
                nt += 1
        nb = len(acted.get(r['slug'], []))
        tot_c += min(nc, S.cross_max_per_round); tot_t += nt; tot_bot += nb
        note = ''
        if nb == 0 and nc > 0:
            note = 'MISSED (close-rule crossing, bot silent)'
            missed.append((r, nc, nt))
        elif nb == 0 and nt > 0:
            note = 'touch-only (sub-second excursion)'
        print(f"{fmt(r['start_ms']):16s} {K_:10.2f} {nc:6d} {nt:6d} {nb:4d}  {note}")

    print("\nMISSED rounds, by SLUG (no hand-converting times):")
    for r, nc, nt in missed:
        print(f"   {r['slug']}  start {fmt(r['start_ms'])}  strike {r['strike']:.2f} "
              f"close-rule {nc}  touch {nt}")
    print(f"\nTOTALS over {len(d['rounds'])} rounds: close-rule {tot_c}, "
          f"touch {tot_t}, bot logged {tot_bot}")
    print(f"rounds where the close rule fired but the bot did not: {len(missed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
