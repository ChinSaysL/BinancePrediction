"""Balance projection under the sizing rule that is actually deployed.

Answers "if I start with $X today, what does the account look like in 1, 2, 7,
14 and 30 days?" for the live configuration: 4% of bankroll per trade, the |z|
tilt, a $25 stake cap, the $1 venue floor and a daily stop at 45% of bankroll,
compounded after every trade (main.Bot._refresh_bankroll now makes live do
exactly that).

Method, and its three honest limits:
  * the per-$1 return SHAPE comes from the recorded BTC rounds (replayed by
    tools.sizing_study). The LEVEL is an assumption: the recording is in-sample
    -- the same rounds shaped the gate thresholds -- so the edge is turned down
    by SCALING THE WINNING PAYOFFS and reporting the central (+0.25/$1) and
    conservative (+0.15/$1) cases alongside the raw recording;
  * scaling the payoff is deliberate, and better than shifting every trade down
    by a constant: a shift silently converts small winners into losses (the
    20% loss rate became 34%, which invented a 5-loss halt in 95% of months).
    Haircutting the wins keeps the loss rate and the full -$1 stake loss intact,
    so the drawdown side of the distribution -- the part being judged -- is not
    quietly inflated;
  * return and tilt are drawn INDEPENDENTLY per trade. Drawing them as recorded
    pairs replays the fitted tilt edge (+$5.51/trade coupled vs +$2.24
    decoupled), which is the thing under test rather than something to assume.

The 5-consecutive-loss halt is modelled faithfully. It is DAY-SCOPED: the
streak halt and the daily stop both clear at the UTC day boundary, so a halt
costs the rest of that day rather than the rest of the month. `--no-resume`
shows the old permanent-halt behaviour for comparison.

Usage:
    python -m tools.project_balance
    python -m tools.project_balance --bankroll 76.49 --paths 5000
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import random
import statistics as st

from btcpredict.config import settings
from btcpredict.edge.signal import stake_for_fraction, tilt_factor
from tools.sizing_study import load_trades

HORIZONS = (1, 2, 7, 14, 30)
# Live runs BTC 5m only: main.tick() passes trade=(prefix == primary), so ETH 5m
# is recorded and settled but deliberately never traded.
#
# 249/day is the SHIPPED config's replay rate (614 trades over 678 rounds, the
# honest pricing at a 0.01 bar, a position in ~90% of rounds). It replaces the
# 44/day measured under the old digital/0.12 gate, which no longer describes
# what runs. It is a FLOOR, not an estimate: live updates the persistence streak
# before the tau-window check, so a signal that has been acting during tau > 210
# counts toward the 2 polls and live fires on its first in-window tick, which
# the replay never does. --trades-per-day 0 uses the recording's own rate.
LIVE_TRADES_PER_DAY = 249.0
# The levels matter more than anything else here, because the projection
# compounds: at 4% per trade a 1%-per-trade difference is a 13x difference over
# a month. Listed highest first.
#   as realised, current era  +0.540/$1 -- the 5 live trades since the era cut
#                  (today 11:00 UTC). NOT USABLE as a forecast: se 0.367, so the
#                  95% interval is [-0.18, +1.26], and all 5 predate the tilt
#                  and the fraction switch.
#   as recorded    +0.43/$1 -- the 70-trade BTC replay. IN-SAMPLE: the same
#                  rounds chose the gate thresholds, and fills are assumed at
#                  the displayed ask.
#   central        +0.25/$1 -- the haircut this project has been using.
#   other venue    +0.17/$1 -- the independent Polymarket replication net of
#                  that venue's fees (1,292 trades, t=+5.65). The only number
#                  here produced outside this repo.
#   old build      +0.031/$1 -- the 99 live trades BEFORE the era cut, on
#                  superseded sizing ($1-$10.50 stakes, the broken configs).
#                  Pooling this with the current era is what produced the +0.06
#                  that was quoted before: 99 trades of +0.031 plus 5 of +0.540
#                  averages to +0.071, which describes no build that ever ran.
EDGES = [("as realised, current era (+0.540/$1)", 0.540),
         ("as recorded, in-sample (+0.43/$1)", None),
         ("central (+0.25/$1)", 0.25),
         ("other venue (+0.17/$1)", 0.17),
         ("old build only (+0.031/$1)", 0.031),
         ("NULL: no edge at all (0.00/$1)", 0.0)]

# What the fill costs, measured rather than assumed. On 51 live fills
# (2026-09-11) the EV per $1 measured at the quote the gate saw averaged
# +0.0431, and at the price actually paid +0.0300 -- the fill costs 0.0132/$1
# and was worse than the quote on 78% of fills. Every payoff in this file is
# built from QUOTES, so a projection that does not subtract this is about 30%
# optimistic about the edge it is sizing. Override with --slip 0 for the old
# gross numbers.
MEASURED_SLIP_PER_DOLLAR = 0.0132


def build_pools(market: str, slip: float = MEASURED_SLIP_PER_DOLLAR
                ) -> tuple[list[float], list[float], float]:
    """Per-$1 returns NET of entry cost, tilt multipliers, and the trade rate.

    The pool's payoffs are computed from the quote the gate saw; live pays the
    fill, 1-2s later, and pays UP on 78% of them. `slip` is that difference,
    charged to every trade because it is paid on entry whatever the outcome.
    Set it to 0 to see the gross pool, which is what earlier projections used.
    """
    trades, _ = load_trades(market)
    rets = [t["pnl"] - slip for t in trades]
    tilts = [tilt_factor(t["mz"], reference=settings.tilt_reference_z,
                         lo=settings.tilt_min, hi=settings.tilt_max)
             for t in trades]
    per_day = collections.Counter(
        dt.datetime.fromtimestamp(t["ts_ms"] / 1000, dt.timezone.utc).date()
        for t in trades)
    rate = st.median(per_day.values()) if per_day else 0.0
    return rets, tilts, float(rate)


def haircut(rets: list[float], edge: float | None) -> tuple[list[float], float]:
    """Scale the WINNING payoffs so the mean per-$1 return equals `edge`.

    Losses are left exactly as recorded (-$1 stake, at the venue's fee), so the
    win rate and the size of a loss are untouched and only the prize shrinks.
    Returns the adjusted pool and the multiplier applied, so the assumption is
    visible rather than buried.
    """
    if edge is None:
        return list(rets), 1.0
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    if not wins or not losses:
        return list(rets), 1.0
    wr = len(wins) / len(rets)
    need = (edge - st.mean(losses) * (1 - wr)) / wr
    k = need / st.mean(wins)
    return [r if r <= 0 else r * k for r in rets], k


def _base_stake(bank: float, frac: float, floor: float, cap: float) -> float:
    """main.Bot._base_stake_for, for the modes whose inputs this pool has.

    The mode has to be READ from settings rather than assumed. This tool kept
    modelling fraction mode after live moved to a flat edge band, i.e. it
    reported the numbers of a rule nobody was trading -- the same class of
    fidelity bug that made `break` stand in for `continue`. A mode whose inputs
    are not in the pool raises instead of quietly substituting another one.
    """
    if settings.sizing_mode == "fraction":
        # Returns 0 (skip) when the base cannot reach the venue minimum,
        # unless the floor is switched on -- in which case it returns the
        # minimum. Reading the setting matters: a tool that assumed SKIP would
        # project an idle bot while a floored one is trading every round.
        return stake_for_fraction(bank, fraction=frac, min_stake=floor, cap=cap,
                                  floor_to_minimum=(
                                      settings.stake_floor_to_minimum))
    if settings.sizing_mode == "edge":
        if settings.min_stake_usdt == settings.max_stake_usdt:
            # A flat band needs no per-trade edge: every accepted signal is
            # staked the same, at every bankroll -- including one too small for
            # the stake, which is precisely how a fixed stake ruins an account.
            return settings.min_stake_usdt
        raise NotImplementedError(
            "edge mode with a band needs a per-trade edge; this pool holds "
            "per-$1 returns only -- use a flat band (min == max) instead")
    raise NotImplementedError(
        f"sizing_mode={settings.sizing_mode!r} needs per-trade fair/quote, "
        "which this projection does not carry")


def _stake(bank: float, frac: float, tilts: list[float], floor: float,
           cap: float) -> float:
    """One trade's stake, exactly as main._base_stake_for + main._stake_for.

    The venue floor is enforced in the tilt's clamp-UP: a base that cannot
    reach the minimum is a skip (0.0) and never a smaller bet, while a tilted
    stake that falls below it is raised to it. A projection that models the
    skip but not the clamp-up measures a strategy that never runs.
    """
    base = _base_stake(bank, frac, floor, cap)
    if base <= 0:
        return 0.0
    if settings.stake_tilt == "inv_z" and tilts:
        t = tilt_factor(random.choice(tilts),
                        reference=settings.tilt_reference_z,
                        lo=settings.tilt_min, hi=settings.tilt_max)
        base = min(max(base * t, floor), cap)
    return base


def simulate(rets, tilts, *, bankroll, days, edge, tpd, paths,
             resume_after_halt, cap_pct=None, seed=20260910):
    """One entry per path per horizon plus the risk statistics."""
    pool, _k = haircut(rets, edge)
    rng = random.Random(seed)
    snaps: dict[int, list[float]] = {d: [] for d in HORIZONS}
    wr_snaps: dict[int, list[tuple[int, int]]] = {d: [] for d in HORIZONS}
    dds, halt_days, ruined = [], [], 0
    stopped_days, cap_days, worst_days = [], [], []
    frac = settings.stake_fraction
    cap = settings.stake_cap_usdt
    floor = settings.min_order_usdt
    cap_pct = settings.daily_loss_cap_pct if cap_pct is None else cap_pct
    # 0 = OFF, not a zero-dollar budget: `day_pnl <= -0 * bank` is true the
    # instant a day is flat, which would stop trading on the first tick.
    cap_on = cap_pct > 0
    max_streak = settings.max_consecutive_losses
    # 0 = off here too, or the very first check (`losses >= 0`) freezes every
    # path on day 1 and the tool reports a bot that never trades.
    streak_on = max_streak > 0

    for _ in range(paths):
        bank, peak, dd = bankroll, bankroll, 0.0
        losses, frozen, hit_halt = 0, False, False
        n_stopped = n_cap = 0
        worst_day = 0.0
        wins = trades = 0
        for day in range(1, days + 1):
            if frozen and resume_after_halt:
                frozen, losses = False, 0        # operator resumes the next day
            day_start = bank
            day_pnl = 0.0
            if not frozen:
                for _ in range(int(tpd)):
                    # the guard recomputes the cap against the CURRENT bankroll,
                    # so it tightens as the day goes badly
                    if cap_on and day_pnl <= -cap_pct * bank:
                        n_stopped += 1
                        n_cap += 1
                        break
                    if streak_on and losses >= max_streak:
                        n_stopped += 1
                        break
                    stake = _stake(bank, frac, tilts, floor, cap)
                    if stake <= 0:
                        # The BASE cannot reach the venue minimum: live skips
                        # this trade and keeps going, so this must be a
                        # `continue`. It used to `break`, which ended the whole
                        # day on one unlucky |z| draw and quietly reported a
                        # trade rate near zero.
                        continue
                    pnl = rng.choice(pool) * stake
                    bank += pnl
                    day_pnl += pnl
                    trades += 1
                    if pnl > 0:
                        wins += 1
                    losses = losses + 1 if pnl < 0 else 0
                    peak = max(peak, bank)
                    dd = min(dd, (bank - peak) / peak)
            if day_start > 0:
                worst_day = min(worst_day, (bank - day_start) / day_start)
            if streak_on and losses >= max_streak:
                if not hit_halt:                 # count PATHS, not episodes
                    hit_halt = True
                    halt_days.append(day)
                frozen = True
            if day in HORIZONS:
                snaps[day].append(bank)
                wr_snaps[day].append((wins, trades))
        dds.append(-dd * 100)
        stopped_days.append(n_stopped)
        cap_days.append(n_cap)
        worst_days.append(-worst_day * 100)
        if bank < 2 * floor:
            ruined += 1

    dds.sort()
    worst_days.sort()
    out = {"dds": dds,
           "p95_dd": dds[int(0.95 * len(dds))] if dds else 0.0,
           "med_dd": st.median(dds) if dds else 0.0,
           "halt_p": len(halt_days) / paths,
           "halt_day": st.median(halt_days) if halt_days else None,
           "ruin_p": ruined / paths,
           "stopped_days": st.mean(stopped_days),
           "cap_days": st.mean(cap_days),
           "med_worst_day": st.median(worst_days),
           "p95_worst_day": worst_days[int(0.95 * len(worst_days))]}
    for d in HORIZONS:
        xs = sorted(snaps[d])
        w = sum(a for a, _ in wr_snaps[d])
        t = sum(b for _, b in wr_snaps[d])
        out[d] = {"med": st.median(xs), "p10": xs[int(0.10 * len(xs))],
                  "p90": xs[int(0.90 * len(xs))],
                  "wr": w / t if t else 0.0, "trades": t / len(snaps[d]),
                  "down": sum(1 for x in xs if x < bankroll) / len(xs)}
    return out


def cap_curve(rets, tilts, *, bankroll, days, tpd, paths,
              edges=(("central", 0.25), ("other venue", 0.17),
                     ("small edge", 0.06), ("no edge", 0.0)),
              caps=(0.05, 0.10, 0.15, 0.20, 0.30, 0.45, 0.0)) -> None:
    """How the daily stop trades profit for tail protection.

    0.0 = no daily stop at all, which is the baseline the others are judged
    against. The cap is also expressed in STAKES (cap / stake_fraction), because
    that is the unit the decision is actually made in: 45% is eleven losing
    trades at 4% each.

    Run at several edge levels on purpose. How much a daily stop is worth
    depends almost entirely on whether the edge is real: at +0.25/$1 the
    average day GAINS ~44% of the bankroll, so a losing day is rare and the cap
    has almost nothing to do. At zero edge the same cap is a working brake.
    """
    for label, edge in edges:
        print(f"\ndaily stop curve  |  {days}-day horizon, {label} edge "
              f"{edge:+.2f}/$1, {tpd:.0f} trades/day, {paths} paths")
        print(f"  {'cap':>6}{'stakes':>7}{'med profit':>12}{'p10 profit':>12}"
              f"{'medDD':>8}{'p95DD':>8}{'days stopped':>14}{'p95 worst day':>15}")
        for c in caps:
            r = simulate(rets, tilts, bankroll=bankroll, days=days, edge=edge,
                         tpd=tpd, paths=paths, resume_after_halt=True,
                         cap_pct=c)
            label_c = "off" if c == 0.0 else f"{c:.0%}"
            stakes = ("-" if c == 0.0 or settings.sizing_mode != "fraction"
                      or settings.stake_fraction <= 0
                      else f"{c / settings.stake_fraction:.0f}")
            print(f"  {label_c:>6}{stakes:>7}${r[days]['med'] - bankroll:>+11.0f}"
                  f"${r[days]['p10']:>11.0f}{r['med_dd']:>7.1f}%"
                  f"{r['p95_dd']:>7.1f}%{r['stopped_days']:>9.1f}/{days:<3}"
                  f"{r['p95_worst_day']:>14.1f}%")
    print("\n  A daily stop cannot shrink a MONTHLY drawdown much: the cap resets\n"
          "  every UTC day, while a monthly drawdown accumulates ACROSS days, so\n"
          "  the only days it removes are the ones that would have started the\n"
          "  spiral. What it does buy is a bound on a single day -- worth having\n"
          "  as a circuit breaker against a runaway bug, not as a way to improve\n"
          "  the monthly distribution.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bankroll", type=float, default=76.49,
                    help="starting balance (default: the live wallet balance)")
    ap.add_argument("--paths", type=int, default=3000)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--market", default="btc-updown-5m")
    ap.add_argument("--trades-per-day", type=float, default=LIVE_TRADES_PER_DAY,
                    help="0 = use the rate measured in the recording instead")
    ap.add_argument("--no-resume", action="store_true",
                    help="leave the bot stopped after a 5-loss halt")
    ap.add_argument("--sweep-cap", action="store_true",
                    help="curve of the daily loss stop instead of the table")
    ap.add_argument("--edge", type=float, default=None,
                    help="single edge to use instead of the default set")
    ap.add_argument("--slip", type=float, default=MEASURED_SLIP_PER_DOLLAR,
                    help="entry cost per $1 staked, subtracted from every trade "
                         "(measured 0.0132 on 51 live fills; 0 = the gross pool)")
    args = ap.parse_args()

    rets, tilts, replay_tpd = build_pools(args.market, slip=args.slip)
    tpd = args.trades_per_day or replay_tpd
    b0 = args.bankroll
    losses = sum(1 for r in rets if r <= 0)

    if args.sweep_cap:
        edges = ([(f"{args.edge:+.2f}/$1", args.edge)] if args.edge is not None
                 else None)
        cap_curve(rets, tilts, bankroll=b0, days=args.days, tpd=tpd,
                  paths=args.paths, **({"edges": edges} if edges else {}))
        return

    print(f"replayed {len(rets)} recorded BTC trades  |  mean {st.mean(rets):+.4f}/$1 "
          f"net of a {args.slip * 100:.2f}c/$1 entry slip "
          f"({st.mean(rets) + args.slip:+.4f}/$1 gross)  "
          f"|  sd {st.pstdev(rets):.3f}  |  loss rate {losses / len(rets):.0%}")
    print(f"trade rate: {tpd:.0f}/day live measurement (replay alone gives "
          f"{replay_tpd:.0f}/day); live trades BTC 5m only")
    if settings.sizing_mode == "fraction":
        sizing = (f"{settings.stake_fraction:.0%} of bankroll x tilt, "
                  f"cap ${settings.stake_cap_usdt:.0f}")
    elif settings.sizing_mode == "edge":
        sizing = (f"edge band ${settings.min_stake_usdt:.0f}"
                  f"-${settings.max_stake_usdt:.0f}")
    else:
        sizing = settings.sizing_mode
    stop = ("OFF" if settings.daily_loss_cap_pct <= 0
            else f"{settings.daily_loss_cap_pct:.0%}")
    halt = ("off" if settings.max_consecutive_losses <= 0
            else f"after {settings.max_consecutive_losses} straight losses")
    print(f"live rule: {sizing}, floor ${settings.min_order_usdt:.2f}, "
          f"daily stop {stop}, halt {halt} "
          f"(day-scoped: trading resumes at the UTC day roll)")
    print(f"start ${b0:.2f}  |  {args.paths} paths"
          f"{'' if args.no_resume else ', resumed the day after a halt'}\n")

    for label, edge in EDGES:
        _pool, k = haircut(rets, edge)
        r = simulate(rets, tilts, bankroll=b0, days=args.days, edge=edge,
                     tpd=tpd, paths=args.paths,
                     resume_after_halt=not args.no_resume)
        print(f"=== {label}"
              + (f"  [win payoffs x{k:.2f}]" if edge else "  [no haircut]") + " ===")
        print(f"  {'horizon':<9}{'median':>10}{'p10':>10}{'p90':>10}"
              f"{'median P/L':>12}{'return':>10}{'chance down':>13}")
        for d in HORIZONS:
            s = r[d]
            print(f"  {str(d) + ' day' + ('s' if d > 1 else ''):<9}"
                  f"${s['med']:>9.2f}${s['p10']:>9.2f}${s['p90']:>9.2f}"
                  f"${s['med'] - b0:>+11.2f}{s['med'] / b0 - 1:>9.1%}"
                  f"{s['down']:>13.1%}")
        print(f"  median drawdown {r['med_dd']:.1f}%  |  p95 drawdown "
              f"{r['p95_dd']:.1f}%  |  P(5-loss halt) {r['halt_p']:.0%}"
              + (f" at median day {r['halt_day']:.0f}" if r["halt_day"] else "")
              + f"  |  P(bank < $2) {r['ruin_p']:.1%}\n")


if __name__ == "__main__":
    main()
