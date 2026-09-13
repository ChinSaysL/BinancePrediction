"""30-day compounding projection for the CONFIG THAT IS ACTUALLY RUNNING.

Arms: model + cross only (impulse, double, stale_maker and probe are off).
Sizing is the deployed rule, read off the live box rather than the repo
defaults, which differ:
    sizing_mode      fraction
    stake_fraction   0.0224      (NOT the config default 0.04)
    cross_stake_mult 1.5
    min_order_usdt   1.00, and stake_floor_to_minimum=True -- so a stake below
                     the venue floor is RAISED to $1 rather than skipped
    stake_cap_usdt   0  (off)      daily_loss_cap_pct 0.20
    cross_max_per_day 20
`max_stake_usdt` is inert in fraction mode (stake_for_fraction takes `cap`,
which is stake_cap_usdt), so it is not modelled.

Arm inputs, both from the 30-day corpus with the venue's REAL Chainlink strike
and REAL settlement:
  cross  gate 5.0bps, tau 10-60: 1413 crossings / 31 days. Priced on the 7 days
         that carry quotes: ask 0.623, win 0.690, EV/$1 +0.314.
  model  tools/arms_backtest.py on the 8-day priced corpus: 170.4 fills/day,
         win 0.526, px 0.500, EV/$1 +0.0452.
The winning payoff is backed out of the measured EV so the venue fee the replay
charged stays in: payoff = (EV + 1 - win) / win.

Depth is capped at the measured $55 median resting at the touch. Uncapped, a
$46 account compounds into liquidity that is not there.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# live values, read from the box on 2026-09-13
FRACTION = 0.0224
CROSS_MULT = 1.5
MIN_ORDER = 1.00
FLOOR_TO_MIN = True
STAKE_CAP = 0.0
DAILY_STOP = 0.20
CROSS_MAX_DAY = 0  # cap disabled 2026-09-13, see config.cross_max_per_day

ARMS = {                 # rate/day, win, EV/$1, stake multiplier, per-day cap
    "model": (170.4, 0.526, 0.0452, 1.0, 0),
    "cross": (45.6,  0.690, 0.3140, CROSS_MULT, CROSS_MAX_DAY),
}


def payoff_of(win, ev):
    return (ev + 1.0 - win) / win


def simulate(names, days, paths, start, rng, depth_cap=55.0):
    rate = np.array([ARMS[n][0] for n in names])
    win = np.array([ARMS[n][1] for n in names])
    pay = np.array([payoff_of(ARMS[n][1], ARMS[n][2]) for n in names])
    mult = np.array([ARMS[n][3] for n in names])
    cap = np.array([ARMS[n][4] for n in names])

    bank = np.full(paths, float(start))
    trades = np.zeros((paths, len(names)))
    pnl_by_arm = np.zeros((paths, len(names)))

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
            stake = bank * FRACTION * mult[j]
            if FLOOR_TO_MIN:
                stake = np.maximum(stake, MIN_ORDER)
            else:
                stake = np.where(bank * FRACTION >= MIN_ORDER, stake, 0.0)
            if STAKE_CAP:
                stake = np.minimum(stake, STAKE_CAP)
            if depth_cap:
                stake = np.minimum(stake, depth_cap)
            stake = np.minimum(stake, bank)
            live = live & (stake > 0)
            w = rng.random(paths) < win[j]
            pnl = np.where(w, stake * pay[j], -stake)
            pnl = np.where(live, pnl, 0.0)
            bank = bank + pnl
            pnl_by_arm[:, j] += pnl
            trades[:, j] += live
            halted |= (bank < day_start * (1.0 - DAILY_STOP))
        bank = np.maximum(bank, 0.0)
    return bank, trades, pnl_by_arm


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=46.43)   # live balance
    ap.add_argument("--paths", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--depth-cap", type=float, default=55.0)
    ap.add_argument("--cross-ev", type=float, default=None,
                    help="override the cross arm's EV/$1. The shipped +0.314 is "
                         "measured on 7 priced corpus days at a mean ask of "
                         "0.623; the first live fill paid 0.676. This is the "
                         "assumption most likely to be wrong, so it is worth "
                         "being able to price being wrong about it.")
    a = ap.parse_args()

    print(f"LIVE CONFIG: model + cross | ${a.start:.2f} start | compounding | "
          f"{a.paths} paths")
    print(f"sizing {FRACTION:.4f} x bankroll (cross x{CROSS_MULT}), floor "
          f"${MIN_ORDER:.2f}, cap {'OFF' if not STAKE_CAP else STAKE_CAP}, "
          f"daily stop {DAILY_STOP:.0%}, cross cap {CROSS_MAX_DAY}/day, "
          f"depth cap ${a.depth_cap:.0f}\n")
    for n, (r, w, ev, m, c) in ARMS.items():
        print(f"  {n:6s} {r:6.1f} trades/day  win {w:.3f}  EV/$1 {ev:+.4f}  "
              f"payoff {payoff_of(w, ev):.3f}")

    if a.cross_ev is not None:
        r, w, _ev, m, c = ARMS["cross"]
        ARMS["cross"] = (r, w, a.cross_ev, m, c)
        print(f"\n  [cross EV overridden to {a.cross_ev:+.4f}/$1]")

    sets = {"MODEL alone": ["model"], "CROSS alone": ["cross"],
            "BOTH": ["model", "cross"]}
    print(f"\n{'arm':13s} {'day':>4} {'median':>10} {'p10':>10} {'p90':>10} "
          f"{'$/day':>9} {'$/hour':>8} {'$/trade':>8} {'trades':>7}")
    for label, names in sets.items():
        for d in (1, 7, 30):
            rng = np.random.default_rng(a.seed)
            bank, trades, _ = simulate(names, d, a.paths, a.start, rng, a.depth_cap)
            med = float(np.median(bank))
            prof = med - a.start
            tr = float(np.median(trades.sum(axis=1))) or 1.0
            print(f"{label if d == 1 else '':13s} {d:4d} ${med:9.2f} "
                  f"${float(np.percentile(bank, 10)):9.2f} "
                  f"${float(np.percentile(bank, 90)):9.2f} "
                  f"{prof/d:+9.2f} {prof/(d*24):+8.3f} {prof/tr:+8.4f} {tr:7.0f}")
        print()

    print("BOTH -- profit split by arm (median path contribution)")
    print(f"{'day':>4} {'model $':>11} {'cross $':>11} {'total $':>11} "
          f"{'model tr':>9} {'cross tr':>9}")
    for d in (1, 7, 30):
        rng = np.random.default_rng(a.seed)
        bank, trades, by = simulate(["model", "cross"], d, a.paths, a.start,
                                    rng, a.depth_cap)
        print(f"{d:4d} ${np.median(by[:, 0]):10.2f} ${np.median(by[:, 1]):10.2f} "
              f"${np.median(bank) - a.start:10.2f} "
              f"{np.median(trades[:, 0]):9.0f} {np.median(trades[:, 1]):9.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
