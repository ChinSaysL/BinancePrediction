"""Compounding projection for the cross arm on ONE market vs MANY.

Answers the multi-market experiment in the shape the earlier corpus table used:
$50 start, day 1/7/30, median/p10/p90, $/day, $/hour, $/trade -- but with the
market set as the variable.

Per-market inputs and where each comes from:
  trigger rate  tools/cross_market_month.py, a full August month of synthesized
                rounds, ANCHORED so btc-updown-5m equals the 31-day corpus
                (25.5 triggers/day, win 0.6907). The month replay fires 2.6x
                more often than the corpus because a 1s tape registers crosses
                that the bot's ~0.3/s recorded tape never sees, so the rates are
                scaled by that same ratio and the win rates shifted by the same
                offset. Ranking is measured; the LEVEL is the corpus anchor.
  ask           tools/cross_book_pricing.py, the venue's own L2 inside the
                arm's gate (|moneyness| <= 0.5 bps, tau 10-60).

Sizing is the deployed rule: stake_fraction x tilt x cross_stake_mult, floored
at min_stake_usdt, compounded after every trade, with the daily loss stop and
cross_max_per_day both enforced.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings as S   # noqa: E402

# name: (triggers/day, win, ask, n_ask)
MARKETS = {
    "btc-updown-5m":  (25.5, 0.6907, 0.540, 51),
    "eth-updown-5m":  (25.0, 0.6647, 0.609, 28),
    "bnb-updown-5m":  (28.1, 0.6547, 0.810, 25),
}

SETS = {
    "BTC 5m only (shipped)": ["btc-updown-5m"],
    "BTC+ETH 5m":            ["btc-updown-5m", "eth-updown-5m"],
    "BTC+ETH+BNB 5m":        ["btc-updown-5m", "eth-updown-5m", "bnb-updown-5m"],
}

# --- the two live ARMS, measured on the 8-day PRICED corpus -----------------
# tools/arms_backtest.py, real venue quotes, $1 flat, live position rule:
#   model  1363 fills 170.4/day WR 52.6% px 0.500 EV/$ +0.0452 day-t 1.31
#   cross   210 fills  26.2/day WR 59.5% px 0.506 EV/$ +0.1968 day-t 0.65
# The winning payoff is backed out of the measured EV/$ rather than from the
# price, so the venue fee the replay charged stays in:
#     EV = win * payoff - (1 - win)  ->  payoff = (EV + 1 - win) / win
# stake_mult is the arm's own: cross carries cross_stake_mult, model does not.
ARMS = {           # rate/day,  win,   EV/$,   stake_mult, per-day cap
    "model": (170.4, 0.526, 0.0452, 1.0, 0),
    "cross": (26.2,  0.595, 0.1968, S.cross_stake_mult, S.cross_max_per_day),
}
ARM_SETS = {
    "MODEL alone": ["model"],
    "CROSS alone": ["cross"],
    "BOTH":        ["model", "cross"],
}


def payoff_of(win: float, ev: float) -> float:
    return (ev + 1.0 - win) / win


def simulate_arms(names, days, paths, start, rng, depth_cap=0.0):
    rate = np.array([ARMS[n][0] for n in names])
    win = np.array([ARMS[n][1] for n in names])
    pay = np.array([payoff_of(ARMS[n][1], ARMS[n][2]) for n in names])
    mult = np.array([ARMS[n][3] for n in names])
    cap = np.array([ARMS[n][4] for n in names])
    bank = np.full(paths, float(start))
    trades = np.zeros(paths)
    for _ in range(days):
        day_start = bank.copy()
        halted = np.zeros(paths, dtype=bool)
        n_per = rng.poisson(rate[None, :], size=(paths, len(names)))
        for j in range(len(names)):
            if cap[j]:
                n_per[:, j] = np.minimum(n_per[:, j], cap[j])
        order = []
        for j in range(len(names)):
            order += [j] * int(n_per[:, j].max())
        rng.shuffle(order)
        for j in order:
            live = (~halted) & (n_per[:, j] > 0)
            if not live.any():
                continue
            n_per[live, j] -= 1
            stake = bank * S.stake_fraction * mult[j]
            stake = np.where(bank * S.stake_fraction >= S.min_order_usdt, stake, 0.0)
            if depth_cap:
                stake = np.minimum(stake, depth_cap)
            stake = np.minimum(stake, bank)
            live = live & (stake > 0)
            w = rng.random(paths) < win[j]
            pnl = np.where(w, stake * pay[j], -stake)
            bank = np.where(live, bank + pnl, bank)
            trades += live
            halted |= (bank < day_start * (1.0 - S.daily_loss_cap_pct))
        bank = np.maximum(bank, 0.0)
    return bank, trades


def simulate(names, days, paths, start, rng, depth_cap=0.0):
    rate = np.array([MARKETS[n][0] for n in names])
    win = np.array([MARKETS[n][1] for n in names])
    ask = np.array([MARKETS[n][2] for n in names])
    bank = np.full(paths, float(start))
    trades = np.zeros(paths)
    for _ in range(days):
        day_start = bank.copy()
        halted = np.zeros(paths, dtype=bool)
        # each market fires independently; interleave them through the day
        n_per = rng.poisson(rate[None, :], size=(paths, len(names)))
        n_per = np.minimum(n_per, S.cross_max_per_day)
        order = []
        for j in range(len(names)):
            order += [j] * int(n_per[:, j].max())
        rng.shuffle(order)
        for j in order:
            live = (~halted) & (n_per[:, j] > 0)
            if not live.any():
                continue
            n_per[live, j] -= 1
            # The deployed rule, verified against main._stake_for: fraction mode
            # applies NO tilt, and the binding floor is min_order_usdt ($1) --
            # min_stake_usdt ($5) belongs to the edge/kelly paths. Below the
            # floor stake_for_fraction SKIPS rather than rounding up.
            stake = bank * S.stake_fraction * S.cross_stake_mult
            stake = np.where(bank * S.stake_fraction >= S.min_order_usdt, stake, 0.0)
            if S.stake_cap_usdt:
                stake = np.minimum(stake, S.stake_cap_usdt)
            if depth_cap:
                # Depth at the touch is a real wall: median $55.55 over 19,452
                # recorded snapshots (FINDINGS). Above it the order sweeps into
                # worse levels, so an uncapped projection is compounding into
                # liquidity that is not there.
                stake = np.minimum(stake, depth_cap)
            stake = np.minimum(stake, bank)          # cannot bet what you lack
            live = live & (stake > 0)
            w = rng.random(paths) < win[j]
            pnl = np.where(w, stake * (1.0 - ask[j]) / ask[j], -stake)
            bank = np.where(live, bank + pnl, bank)
            trades += live
            halted |= (bank < day_start * (1.0 - S.daily_loss_cap_pct))
        bank = np.maximum(bank, 0.0)
    return bank, trades


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=50.0)
    ap.add_argument("--paths", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--depth-cap", type=float, default=0.0,
                    help="max fillable stake at the touch; 0 = unlimited")
    ap.add_argument("--arms", action="store_true",
                    help="project the MODEL/CROSS/BOTH arms instead of market sets")
    a = ap.parse_args()

    print(f"${a.start:.0f} start, compounding, {a.paths} paths | sizing "
          f"{S.stake_fraction:.0%} x {S.cross_stake_mult} "
          f"(skip under ${S.min_order_usdt:.0f}, cap "
          f"{'OFF' if not S.stake_cap_usdt else S.stake_cap_usdt}), "
          f"daily stop {S.daily_loss_cap_pct:.0%} | depth cap "
          f"{'none' if not a.depth_cap else f'${a.depth_cap:.0f}'}\n")
    print(f"{'arm' if a.arms else 'market set':24s} {'day':>4} {'median':>10} {'p10':>10} {'p90':>10} "
          f"{'$/day':>9} {'$/hour':>8} {'$/trade':>8}")
    sets, sim = ((ARM_SETS, simulate_arms) if a.arms else (SETS, simulate))
    for label, names in sets.items():
        for d in (1, 7, 30):
            rng = np.random.default_rng(a.seed)
            bank, trades = sim(names, d, a.paths, a.start, rng,
                               depth_cap=a.depth_cap)
            med = float(np.median(bank))
            prof = med - a.start
            tr = float(np.median(trades)) or 1.0
            print(f"{label if d==1 else '':24s} {d:4d} ${med:9.2f} "
                  f"${float(np.percentile(bank,10)):9.2f} "
                  f"${float(np.percentile(bank,90)):9.2f} "
                  f"{prof/d:+9.2f} {prof/(d*24):+8.3f} {prof/tr:+8.4f}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
