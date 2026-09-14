"""Cross-only sizing over the FULL 31-day corpus.

The 7-day sweep had one real weakness: a day-block bootstrap over 7 blocks,
of which only 5 carry usable quotes (btc5m_prices.parquet degrades on its last
three days -- the ITM ask collapses to a 0.02 median on 5-91 rows, which is not
a market). This widens the TRADE sequence to all 31 days, where the strike is
the venue's real Chainlink `strike_cl` and the settlement is its own, and keeps
the ask empirical.

Construction, stated plainly because it is a hybrid:
  trades    all 31 corpus days, real crossings, real outcomes (1,413 total)
  ask       drawn from the 5 clean priced days, conditional on the |moneyness|
            bucket of the crossing, with the DAY sampled per simulated day so
            day-level ask regimes survive (per-day ask medians run 0.59-0.80)
  validation the same draw is scored against the days where the true ask IS
            known; if it does not reproduce their EV, the construction is wrong

This is not a substitute for 31 days of real quotes. It is a way to get 31
day-blocks into the bootstrap instead of 5.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings as S     # noqa: E402

CLEAN_DAYS = [20572, 20576, 20580, 20584, 20588]     # quotes intact
BUCKETS = [0, 0.5, 1.0, 2.0, 3.0, 5.0]


def load():
    ml = pd.read_parquet('data/btc5m_ml.parquet',
                         columns=['window_id', 'day', 'tau_s', 'cl_delta_bps', 'y_up']).dropna()
    px = pd.read_parquet('data/btc5m_prices.parquet',
                         columns=['window_id', 'tau_s', 'up_sell_px', 'dn_sell_px'])
    return ml, px


def crossings(ml, px=None):
    """Real crossings. With px, attach the true ask."""
    m = ml if px is None else ml.merge(px, on=['window_id', 'tau_s'], how='inner')
    m = m.sort_values(['window_id', 'tau_s'], ascending=[True, False])
    out = []
    for _wid, g in m.groupby('window_id', sort=False):
        d = g.cl_delta_bps.to_numpy(); tau = g.tau_s.to_numpy()
        y = int(g.y_up.iloc[0]); day = g.day.iloc[0]; n = 0
        ua = g.up_sell_px.to_numpy() if px is not None else None
        da = g.dn_sell_px.to_numpy() if px is not None else None
        for i in range(1, len(d)):
            if not (S.cross_min_tau_s <= tau[i] <= S.cross_max_tau_s):
                continue
            if np.sign(d[i]) == np.sign(d[i - 1]) or d[i] == 0:
                continue
            if abs(d[i]) > S.cross_max_depth_bps:
                continue
            up = d[i] > 0
            ask = None
            if px is not None:
                ask = ua[i] if up else da[i]
                if not (0.0 < ask < 1.0):
                    continue
            out.append((day, abs(d[i]), int(up == bool(y)), ask))
            n += 1
            if n >= S.cross_max_per_round:
                break
    return pd.DataFrame(out, columns=['day', 'm', 'won', 'ask'])


def ret_of(ask, won):
    fee = 200.0 * np.minimum(1.0, (1 - ask) / ask) / 1e4
    return np.where(won == 1, (1 - ask) / ask * (1 - fee), -1.0)


def ask_pools(priced):
    """|moneyness| bucket -> {day -> array of asks}."""
    p = priced[priced.day.isin(CLEAN_DAYS)].copy()
    p['b'] = pd.cut(p.m, BUCKETS, include_lowest=True)
    pools = {}
    for (b, d), g in p.groupby(['b', 'day'], observed=True):
        pools.setdefault(b, {})[d] = g.ask.to_numpy()
    return pools, sorted(p.b.cat.categories, key=lambda x: x.left)


def draw(pools, cats, m_arr, day_pick, rng):
    b = pd.cut(m_arr, BUCKETS, include_lowest=True)
    out = np.empty(len(m_arr))
    for i, (mb) in enumerate(b):
        pool = pools.get(mb) or pools[cats[-1]]
        arr = pool.get(day_pick)
        if arr is None or not len(arr):
            arr = np.concatenate(list(pool.values()))
        out[i] = arr[rng.integers(0, len(arr))]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=50.96)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--paths", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=9)
    ap.add_argument("--depth-cap", type=float, default=55.0)
    a = ap.parse_args()

    ml, px = load()
    priced = crossings(ml, px)
    allc = crossings(ml)
    pools, cats = ask_pools(priced)
    rng = np.random.default_rng(a.seed)

    print(f"trade sequence: {len(allc)} crossings over {allc.day.nunique()} days "
          f"({len(allc)/allc.day.nunique():.1f}/day, win {allc.won.mean():.4f})")
    print(f"ask pools: {len(priced[priced.day.isin(CLEAN_DAYS)])} real asks "
          f"from {len(CLEAN_DAYS)} clean days")

    # --- validation: reproduce the known days' EV with drawn asks -------------
    known = priced[priced.day.isin(CLEAN_DAYS)]
    true_ev = ret_of(known.ask.to_numpy(), known.won.to_numpy()).mean()
    sim = []
    for _ in range(400):
        dp = CLEAN_DAYS[rng.integers(0, len(CLEAN_DAYS))]
        ask = draw(pools, cats, known.m.to_numpy(), dp, rng)
        sim.append(ret_of(ask, known.won.to_numpy()).mean())
    print(f"\nVALIDATION on the 5 clean days: true EV {true_ev:+.4f}/$1 | "
          f"drawn-ask EV {np.mean(sim):+.4f} [{np.percentile(sim,5):+.3f},"
          f"{np.percentile(sim,95):+.3f}]")
    ok = np.percentile(sim, 5) <= true_ev <= np.percentile(sim, 95)
    print(f"  true EV inside the drawn 90% band: {ok}")

    # --- sizing sweep over 31 day-blocks -------------------------------------
    day_groups = [g for _, g in allc.groupby('day')]
    print(f"\n=== sizing, {a.days}d from ${a.start:.2f}, {a.paths} paths, "
          f"{len(day_groups)} day-blocks (was 5) ===")
    print(f"{'frac':>7} {'mult':>6} {'median':>11} {'p10':>10} {'p90':>12} "
          f"{'P(<start)':>10} {'P(ruin)':>8} {'stops/30d':>10}")
    for frac in (0.0224, 0.0336, 0.0504, 0.075, 0.10, 0.15, 0.20):
        r2 = np.random.default_rng(a.seed)
        bank = np.full(a.paths, a.start)
        stops = np.zeros(a.paths)
        for _d in range(a.days):
            gi = r2.integers(0, len(day_groups), a.paths)
            dp = np.array(CLEAN_DAYS)[r2.integers(0, len(CLEAN_DAYS), a.paths)]
            day_start = bank.copy()
            halted = np.zeros(a.paths, dtype=bool)
            maxn = max(len(g) for g in day_groups)
            for j in range(maxn):
                live = ~halted
                if not live.any():
                    break
                won = np.full(a.paths, -1); mm = np.zeros(a.paths)
                has = np.zeros(a.paths, dtype=bool)
                for k, g in enumerate(day_groups):
                    sel = (gi == k)
                    if not sel.any() or j >= len(g):
                        continue
                    won[sel] = g.won.iloc[j]; mm[sel] = g.m.iloc[j]; has[sel] = True
                act = live & has
                if not act.any():
                    continue
                ask = np.empty(a.paths)
                for dd in CLEAN_DAYS:
                    s2 = act & (dp == dd)
                    if s2.any():
                        ask[s2] = draw(pools, cats, mm[s2], dd, r2)
                stake = np.maximum(bank * frac, 1.0)
                stake = np.minimum(np.minimum(stake, a.depth_cap), bank)
                r = np.where(won == 1,
                             (1 - ask) / np.maximum(ask, 1e-6) *
                             (1 - 200 * np.minimum(1, (1 - ask) / np.maximum(ask, 1e-6)) / 1e4),
                             -1.0)
                bank = np.maximum(bank + np.where(act, stake * r, 0.0), 0.0)
                newly = (~halted) & (bank < day_start * (1 - S.daily_loss_cap_pct))
                stops += newly; halted |= newly
        print(f"{frac:7.2%} {frac/0.0224:6.2f} ${np.median(bank):10.2f} "
              f"${np.percentile(bank,10):9.2f} ${np.percentile(bank,90):11.2f} "
              f"{np.mean(bank<a.start):10.1%} {np.mean(bank<5):8.1%} "
              f"{np.mean(stops):10.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
