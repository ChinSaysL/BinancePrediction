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
from btcpredict.edge.signal import tilt_factor
from tools.sizing_study import load_trades

HORIZONS = (1, 2, 7, 14, 30)
# Live runs BTC 5m only: main.tick() passes trade=(prefix == primary), so ETH 5m
# is recorded and settled but deliberately never traded. Measured fill rate from
# the live DB is 38 and 51 fills on the two complete UTC days, against 22/day
# from the stricter replay (which needs recorded quotes AND signal persistence
# for every candle open). The live number is what a projection should use.
LIVE_TRADES_PER_DAY = 44.0
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


def build_pools(market: str) -> tuple[list[float], list[float], float]:
    """Per-$1 returns and tilt multipliers, plus the measured trade rate."""
    trades, _ = load_trades(market)
    rets = [t["pnl"] for t in trades]
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


def simulate(rets, tilts, *, bankroll, days, edge, tpd, paths,
             resume_after_halt, seed=20260910):
    """One entry per path per horizon plus the risk statistics."""
    pool, _k = haircut(rets, edge)
    rng = random.Random(seed)
    snaps: dict[int, list[float]] = {d: [] for d in HORIZONS}
    dds, halt_days, ruined = [], [], 0
    frac = settings.stake_fraction
    cap = settings.stake_cap_usdt
    floor = settings.min_order_usdt
    cap_pct = settings.daily_loss_cap_pct
    max_streak = settings.max_consecutive_losses

    for _ in range(paths):
        bank, peak, dd = bankroll, bankroll, 0.0
        losses, frozen, hit_halt = 0, False, False
        for day in range(1, days + 1):
            if frozen and resume_after_halt:
                frozen, losses = False, 0        # operator resumes the next day
            day_pnl = 0.0
            if not frozen:
                for _ in range(int(tpd)):
                    # the guard recomputes the cap against the CURRENT bankroll,
                    # so it tightens as the day goes badly
                    if day_pnl <= -cap_pct * bank or losses >= max_streak:
                        break
                    stake = min(bank * frac * rng.choice(tilts), cap)
                    if stake < floor:
                        break                    # cannot size: skip the trade
                    pnl = rng.choice(pool) * stake
                    bank += pnl
                    day_pnl += pnl
                    losses = losses + 1 if pnl < 0 else 0
                    peak = max(peak, bank)
                    dd = min(dd, (bank - peak) / peak)
            if losses >= max_streak:
                if not hit_halt:                 # count PATHS, not episodes
                    hit_halt = True
                    halt_days.append(day)
                frozen = True
            if day in HORIZONS:
                snaps[day].append(bank)
        dds.append(-dd * 100)
        if bank < 2 * floor:
            ruined += 1

    dds.sort()
    out = {"dds": dds,
           "p95_dd": dds[int(0.95 * len(dds))] if dds else 0.0,
           "med_dd": st.median(dds) if dds else 0.0,
           "halt_p": len(halt_days) / paths,
           "halt_day": st.median(halt_days) if halt_days else None,
           "ruin_p": ruined / paths}
    for d in HORIZONS:
        xs = sorted(snaps[d])
        out[d] = {"med": st.median(xs), "p10": xs[int(0.10 * len(xs))],
                  "p90": xs[int(0.90 * len(xs))],
                  "down": sum(1 for x in xs if x < bankroll) / len(xs)}
    return out


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
    args = ap.parse_args()

    rets, tilts, replay_tpd = build_pools(args.market)
    tpd = args.trades_per_day or replay_tpd
    b0 = args.bankroll
    losses = sum(1 for r in rets if r <= 0)

    print(f"replayed {len(rets)} recorded BTC trades  |  mean {st.mean(rets):+.4f}/$1  "
          f"sd {st.pstdev(rets):.3f}  |  loss rate {losses / len(rets):.0%}")
    print(f"trade rate: {tpd:.0f}/day live measurement (replay alone gives "
          f"{replay_tpd:.0f}/day); live trades BTC 5m only")
    print(f"live rule: {settings.stake_fraction:.0%} of bankroll x tilt, "
          f"cap ${settings.stake_cap_usdt:.0f}, floor ${settings.min_order_usdt:.2f}, "
          f"daily stop {settings.daily_loss_cap_pct:.0%}, "
          f"halt after {settings.max_consecutive_losses} straight losses "
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
