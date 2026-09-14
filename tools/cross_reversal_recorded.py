"""The reversal question, on the bot's OWN RECORDED BOOK.

`cross_reversal_entries.py` answers it on btc5m_ml (31 days, real Chainlink
strikes -- well powered on WIN RATE) and btc5m_prices (5 days of quotes --
whose own resweep docstring warns "treat a sweep over it as a hypothesis, not
a tuning"). The money half therefore rests on the weak substrate.

This runs the same classification against `quotes`, the venue's real book, with
the venue's own strike and settlement -- the instrument the knob audit used and
the only one whose asks actually existed. If the two disagree, this one wins on
pricing and btc5m_ml wins on win rate, because that is where each has power.

A "reversal" is a trigger whose side is opposite the previous trigger's in the
same round. By construction of a crossing these are the ONLY kind of non-first
entry: a crossing flips the sign, so consecutive triggers always alternate.
`cross_max_per_round > 1` therefore buys nothing but reversals.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings as S     # noqa: E402
from btcpredict.main import crossed_side        # noqa: E402

SLIP = {
    'book':      lambda q: q,
    'additive':  lambda q: q + 0.088,
    'reverting': lambda q: 0.47 * q + 0.40,
}
FEE = 200 / 1e4
FRAC = 0.0224 * 2.25


def load(db):
    """Read the export of the bot's recorded book.

    A CSV pair rather than the sqlite file itself: the DB lives on the VPS,
    whose bot venv has no numpy and whose analysis venv has no pydantic, so the
    tables are dumped there with stdlib and scored here.
    """
    base = os.path.dirname(db) or "."
    r = pd.read_csv(os.path.join(base, "rounds.csv.gz"))
    rounds = {t.slug: (t.start_ms, t.end_ms, t.strike, t.winner)
              for t in r.itertuples()}
    q = pd.read_csv(os.path.join(base, "quotes.csv.gz"))
    q = q.sort_values(["slug", "ts_ms"])
    quotes = {slug: g[["ts_ms", "spot", "up_ask", "down_ask"]].to_numpy("float64")
              for slug, g in q.groupby("slug", sort=False)}
    return rounds, quotes


def replay(rounds, quotes, lo, hi, dep, cap, rng_gate, look_s):
    rows = []
    for slug, book in quotes.items():
        if slug not in rounds:
            continue
        start_ms, end_ms, strike, winner = rounds[slug]
        if not strike:
            continue
        ts, spot, ua, da = book[:, 0], book[:, 1], book[:, 2], book[:, 3]
        day = pd.Timestamp(start_ms, unit='ms', tz='UTC').strftime('%Y-%m-%d')
        n = 0
        prev_side = None
        for i in range(len(ts)):
            if n >= cap:
                break
            tau = (end_ms - ts[i]) / 1000.0
            if not (lo <= tau <= hi):
                continue
            # The shipped detector, fed the ticks it would have had. It walks
            # BACKWARDS for the newest tick at least look_s older, so the
            # reference must be inside the slice -- windowing to [t-look_s, t]
            # hides it and the detector then fires ~never (1 trigger in 4,282
            # rounds on the first attempt). Trail by look_s + slack_s, which is
            # the oldest reference crossed_side will still accept.
            j0 = int(np.searchsorted(ts, ts[i] - (look_s + 2.0) * 1000.0, 'left'))
            if i - j0 < 1:
                continue
            side = crossed_side([(int(t), float(p)) for t, p in
                                 zip(ts[j0:i + 1], spot[j0:i + 1])],
                                float(strike), look_s)
            if side is None:
                continue
            depth = 1e4 * abs(spot[i] - strike) / strike
            if depth > dep:
                continue
            if rng_gate > 0:
                seen = spot[:i + 1]
                span = 1e4 * (seen.max() - seen.min()) / strike
                if span > rng_gate:
                    continue
            ask = float(ua[i] if side == 'Up' else da[i])
            if not (0.0 < ask < 1.0):
                continue
            rows.append((day, slug, n, tau, side, ask,
                         int(prev_side is not None and side != prev_side),
                         int(side == winner)))
            prev_side = side
            n += 1
    return pd.DataFrame(rows, columns=['day', 'slug', 'k', 'tau', 'side',
                                       'ask', 'rev_prev', 'won'])


def growth(df, model):
    if df.empty:
        return 0.0, pd.Series(dtype=float)
    fill = np.clip(SLIP[model](df.ask.to_numpy()), 1e-4, 0.9999)
    r = np.where(df.won.to_numpy() == 1, (1 - fill) / fill * (1 - FEE), -1.0)
    lg = np.log1p(np.clip(FRAC * r, -0.999999, None))
    return float(lg.sum()), pd.Series(lg, index=df.day.to_numpy()).groupby(level=0).sum()


def score(df, label):
    parts = [f"  {label:<24} n={len(df):<4}"]
    for m in SLIP:
        tot, per = growth(df, m)
        if len(per) < 3:
            parts.append(f"{m}: n/a")
            continue
        sd = per.std(ddof=1)
        t = per.mean() / (sd / np.sqrt(len(per))) if sd else float('nan')
        parts.append(f"{m}: g={tot:+.3f} t={t:+.2f} drop={per.drop(per.idxmax()).sum():+.3f}")
    print("  ".join(parts))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='data/recorded/x')
    ap.add_argument('--cap', type=int, default=int(S.cross_max_per_round))
    a = ap.parse_args()

    lo, hi = S.cross_min_tau_s, S.cross_max_tau_s
    rounds, quotes = load(a.db)
    t = replay(rounds, quotes, lo, hi, S.cross_max_depth_bps, a.cap,
               S.cross_max_round_range_bps, S.cross_look_s)
    if t.empty:
        print("no triggers")
        return 1
    print(f"gate: tau {lo:.0f}-{hi:.0f}s depth<={S.cross_max_depth_bps} "
          f"look {S.cross_look_s} cap {a.cap} range<={S.cross_max_round_range_bps}")
    print(f"triggers {len(t)}  rounds {t.slug.nunique()}  days {t.day.nunique()}\n")

    print("=== by entry ordinal ===")
    for k in sorted(t.k.unique()):
        x = t[t.k == k]
        print(f"  entry {k}  n={len(x):<4} win={x.won.mean():.3f}  ask={x.ask.mean():.3f}"
              f"  edge={(x.won - x.ask).mean():+.3f}")

    print("\n=== first vs reversal ===")
    for lab, x in (("first", t[t.k == 0]), ("REVERSAL", t[t.rev_prev == 1])):
        if x.empty:
            continue
        e = x.won - x.ask
        se = e.std(ddof=1) / np.sqrt(len(e)) if len(e) > 1 else float('nan')
        print(f"  {lab:<10} n={len(x):<4} win={x.won.mean():.3f} ask={x.ask.mean():.3f}"
              f"  edge={e.mean():+.3f} +-{1.96*se:.3f}")

    a_ = t[t.rev_prev == 1].groupby('day').won.mean()
    b_ = t[t.k == 0].groupby('day').won.mean()
    j = pd.concat([a_.rename('x'), b_.rename('y')], axis=1).dropna()
    if len(j) >= 3:
        d = (j.x - j.y).to_numpy()
        print(f"  win-rate gap day-t={d.mean()/(d.std(ddof=1)/np.sqrt(len(d))):+.2f}"
              f" over {len(j)} days")

    print(f"\n=== compounded growth (f={FRAC:.4f}), three pricings ===")
    score(t[t.k == 0], "cap 1 (first only)")
    score(t, f"cap {a.cap} (as shipped)")
    score(t[t.rev_prev == 0], f"cap {a.cap}, NO reversals")
    score(t[(t.k > 0) & (t.rev_prev == 0)], "same-side repeats alone")
    score(t[t.rev_prev == 1], "reversals alone")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
