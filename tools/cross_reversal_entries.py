"""Is a cross trigger that REVERSES an earlier trigger in the same round
worth taking, or should the arm decline it?

Prompted by the 2026-09-14 12:05 round, where cross bought Up at 0.611 and
then Down at 0.589 twenty-two seconds later. Combined ask 1.199 on ~equal
share counts: the overlap pays 1.000 for 1.199, a locked -$0.87 that cannot
win. The code comment next to `cross_max_per_round` calls this "naturally
hedged rather than doubled" -- an assertion that was never measured.

THE DECISION COLLAPSES. Treating the first position as sunk, at the instant
of the second trigger:

    decline:  S_A if A wins, else 0
    take:     the same, plus S_B if B wins, minus stake_B

    EV(take) - EV(decline) = stake_B * [ (1 - p)/b - 1 ]        p = P(A wins)

which is positive iff `1 - p > b`, i.e. iff the second entry is +EV ON ITS
OWN. The first entry's price cancels. There is no hedging arithmetic and no
"locked loss" consideration -- the overlap framing is a red herring. The only
question that matters is whether a reversal trigger wins often enough to beat
its own ask.

That is a WIN RATE question, so it runs on all 31 days of real Chainlink
strikes rather than the 5 days that carry usable quotes.

CAVEAT ON THE LOOK. The corpus tau grid is 2-10s apart inside the trade
window, so `look_steps=1` is a 2-10s look-back against the live `look_s` of
1.0. Same approximation the knob audit ran under; it makes triggers slightly
staler here than live, which if anything understates every arm's win rate
uniformly and should not move a FIRST-vs-REVERSAL comparison.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings as S     # noqa: E402

# The only days whose quotes are usable. btc5m_prices.parquet samples every
# 4th day and its last three degrade to a 0.02 median ask on 5-91 rows.
CLEAN = [20572, 20576, 20580, 20584, 20588]


def replay(lo, hi, dep, cap, rng_gate, look_steps, priced):
    """Every trigger the live gate would take, tagged by its role in the round.

    Tags per trigger:
      k        entry ordinal within the round (0 = first)
      rev_prev side differs from the PREVIOUS trigger's side
      rev_head side differs from the FIRST trigger's side
    """
    cols = ['window_id', 'day', 'tau_s', 'cl_delta_bps', 'y_up']
    ml = pd.read_parquet('data/btc5m_ml.parquet', columns=cols).dropna()
    if priced:
        px = pd.read_parquet('data/btc5m_prices.parquet',
                             columns=['window_id', 'tau_s', 'up_sell_px', 'dn_sell_px'])
        ml = ml.merge(px, on=['window_id', 'tau_s'], how='inner')
        ml = ml[ml.day.isin(CLEAN)]
    ml = ml.sort_values(['window_id', 'tau_s'], ascending=[True, False])

    rows = []
    for _w, g in ml.groupby('window_id', sort=False):
        d = g.cl_delta_bps.to_numpy()
        tau = g.tau_s.to_numpy()
        y = int(g.y_up.iloc[0])
        day = int(g.day.iloc[0])
        ua = g.up_sell_px.to_numpy() if priced else None
        da = g.dn_sell_px.to_numpy() if priced else None

        n = 0
        prev_side = None
        head_side = None
        for i in range(look_steps, len(d)):
            if n >= cap:
                break
            if not (lo <= tau[i] <= hi):
                continue
            j = i - look_steps
            if d[i] == 0 or np.sign(d[i]) == np.sign(d[j]):
                continue
            if abs(d[i]) > dep:
                continue
            # Causal range-so-far, round open -> this instant. cl_delta_bps is
            # (spot - strike)/strike in bps, so its running span IS the spot
            # range in bps. Never the full-round range, which is lookahead.
            if rng_gate > 0:
                span = float(d[:i + 1].max() - d[:i + 1].min())
                if span > rng_gate:
                    continue
            side = 'Up' if d[i] > 0 else 'Down'
            if head_side is None:
                head_side = side
            ask = None
            if priced:
                ask = float(ua[i] if side == 'Up' else da[i])
                if not (0.0 < ask < 1.0):
                    continue
            rows.append((day, _w, n, tau[i], side,
                         int(prev_side is not None and side != prev_side),
                         int(side != head_side),
                         int((side == 'Up') == (y == 1)),
                         ask))
            prev_side = side
            n += 1

    return pd.DataFrame(rows, columns=['day', 'wid', 'k', 'tau', 'side',
                                       'rev_prev', 'rev_head', 'won', 'ask'])


def daily_t(df, col, by):
    """Day-clustered t on the difference in `col` between the two groups of
    `by`. Triggers inside a day share a price path and are not independent."""
    a = df[df[by] == 1].groupby('day')[col].mean()
    b = df[df[by] == 0].groupby('day')[col].mean()
    j = pd.concat([a.rename('x'), b.rename('y')], axis=1).dropna()
    if len(j) < 3:
        return float('nan'), len(j)
    diff = (j.x - j.y).to_numpy()
    sd = diff.std(ddof=1)
    if sd == 0:
        return float('nan'), len(j)
    return float(diff.mean() / (sd / np.sqrt(len(diff)))), len(j)


def show(df, label):
    if df.empty:
        print(f"  {label:<26} n=0")
        return
    w = df.won.mean()
    se = (w * (1 - w) / len(df)) ** 0.5
    extra = ""
    if df.ask.notna().any():
        a = df.ask.mean()
        ev = np.where(df.won == 1, (1 - df.ask) / df.ask * (1 - 200 / 1e4), -1.0).mean()
        extra = f"  ask={a:.3f}  EV/$1={ev:+.3f}  breakeven_win={a:.3f}"
    print(f"  {label:<26} n={len(df):<5} rounds={df.wid.nunique():<5} "
          f"win={w:.3f} +-{1.96*se:.3f}{extra}")


#: Both fitted on live fills. They disagree about nearly every knob, so the
#: repo's standing rule is that a finding must hold under BOTH.
SLIP = {
    'additive':   lambda q: q + 0.088,
    'reverting':  lambda q: 0.47 * q + 0.40,
}
FEE = 200 / 1e4
FRAC = 0.0224 * 2.25          # live stake_fraction * cross_stake_mult


def growth(df, model, frac=FRAC):
    """Compounded growth, sum of log(1 + f*r), priced at what we FILL at.

    EV/$1 is not the scoring metric: a variant that lifts EV by cutting volume
    can still compound worse. Returns (total growth, per-day series).
    """
    if df.empty:
        return 0.0, pd.Series(dtype=float)
    q = df.ask.to_numpy()
    fill = np.clip(SLIP[model](q), 1e-4, 0.9999)
    r = np.where(df.won.to_numpy() == 1, (1 - fill) / fill * (1 - FEE), -1.0)
    lg = np.log1p(np.clip(frac * r, -0.999999, None))
    s = pd.Series(lg, index=df.day.to_numpy())
    return float(lg.sum()), s.groupby(level=0).sum()


def score(df, label):
    """Growth under both slippage models, with day-t and drop-best-day."""
    out = [f"  {label:<22} n={len(df):<4}"]
    for m in SLIP:
        tot, per = growth(df, m)
        if len(per) < 3:
            out.append(f"{m}: n/a")
            continue
        t = per.mean() / (per.std(ddof=1) / np.sqrt(len(per))) if per.std(ddof=1) else float('nan')
        drop = per.drop(per.idxmax()).sum()
        out.append(f"{m}: growth={tot:+.3f} day-t={t:+.2f} drop-best={drop:+.3f}")
    print("   ".join(out))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--look-steps', type=int, default=1)
    ap.add_argument('--cap', type=int, default=int(S.cross_max_per_round))
    ap.add_argument('--priced', action='store_true',
                    help='restrict to the 5 days carrying usable quotes')
    a = ap.parse_args()

    lo, hi = S.cross_min_tau_s, S.cross_max_tau_s
    dep = S.cross_max_depth_bps
    rng = S.cross_max_round_range_bps
    print(f"gate: tau {lo:.0f}-{hi:.0f}s  depth<={dep}bps  cap {a.cap}/round  "
          f"round-range<={rng}bps  look_steps={a.look_steps}"
          f"{'  [PRICED DAYS ONLY]' if a.priced else ''}")

    t = replay(lo, hi, dep, a.cap, rng, a.look_steps, a.priced)
    if t.empty:
        print("no triggers")
        return 1
    print(f"\ntriggers {len(t)}  rounds {t.wid.nunique()}  days {t.day.nunique()}")

    print("\n=== by entry ordinal ===")
    for k in sorted(t.k.unique()):
        show(t[t.k == k], f"entry {k}")

    print("\n=== FIRST vs REVERSAL (reverses the previous trigger) ===")
    show(t[t.k == 0], "first entry")
    show(t[(t.k > 0) & (t.rev_prev == 0)], "continuation (same side)")
    show(t[t.rev_prev == 1], "REVERSAL (opposite)")

    print("\n=== reversal vs the rest, day-clustered ===")
    tt, nd = daily_t(t, 'won', 'rev_prev')
    print(f"  win-rate difference   day-t={tt:+.2f}  over {nd} days")
    if t.ask.notna().any():
        t2 = t.copy()
        t2['ev'] = np.where(t2.won == 1, (1 - t2.ask) / t2.ask * (1 - 200 / 1e4), -1.0)
        te, nde = daily_t(t2, 'ev', 'rev_prev')
        print(f"  EV difference         day-t={te:+.2f}  over {nde} days")

    rv = t[t.rev_prev == 1]
    if t.ask.notna().any():
        # Per-trade EDGE, not mean-win vs mean-ask. Comparing the two means is
        # a Jensen error once the ask varies across trades.
        print("\n=== per-trade edge (win - ask), the venue's mispricing ===")
        for lab, x in (("first entry", t[t.k == 0]), ("REVERSAL", rv)):
            if x.empty:
                continue
            e = (x.won - x.ask)
            se = e.std(ddof=1) / np.sqrt(len(e)) if len(e) > 1 else float('nan')
            print(f"  {lab:<22} edge={e.mean():+.3f} +-{1.96*se:.3f}  n={len(e)}")

        print("\n=== compounded growth, both slippage models "
              f"(f={FRAC:.4f}) ===")
        score(t[t.k == 0], "cap 1 (first only)")
        score(t, f"cap {a.cap} (with reversals)")
        score(rv, "the reversals alone")

    print("\n=== verdict ===")
    if rv.empty:
        print("  no reversal entries under this gate")
    else:
        print(f"  reversal win {rv.won.mean():.3f} vs first {t[t.k==0].won.mean():.3f}"
              f"  (day-t {tt:+.2f} over {nd} days)")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
