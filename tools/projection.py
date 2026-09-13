"""Forward projection WITH compounding: stake = 2% of the live bankroll.

The earlier flat projection (tools/pnl_report.py) held the stake constant, which
is wrong: the live rule is `sizing_mode=fraction`, `stake_fraction=0.02`, i.e.
the stake IS the account. This resamples the measured trades and grows/shrinks
the account day by day, so the size of every later bet follows the account.

Two things are deliberately NOT reduced to a summary:

  * The impulse arm is resampled from its ACTUAL (ask, won) pairs, because its
    returns cannot be rebuilt from a win rate: EV/$1 is +0.638 at a 66.7% win
    rate on asks averaging 0.473 -- and a win at 0.473 pays at most +1.07, so
    the winners must have been CHEAPER than the losers. Averaging that away
    would model a different strategy.
  * The ticket arm is a 2-point distribution (win +6.26 / lose -1.00 at
    32.14%), which reproduces all three of its measured moments: the win rate,
    the EV of +1.330, and the implied average ask of 0.135 -- i.e. it is the
    same distribution, not a convenient one.

The rails are modelled, because they are what the compounding runs into:
  * the ticket's own $3/day stop,
  * the guard's 20%-of-bankroll daily loss cap on the main arm.

Usage:  python -m tools.projection [--days 30] [--paths 4000] [--bankroll 53.49]
"""
from __future__ import annotations

import argparse
import random
import sqlite3
import statistics as st
from pathlib import Path

from btcpredict.config import settings
from tools.backtest_impulse_orders import (build_entries, sigma_1m_by_round)

# The ticket cell, as measured (`longshot_bands --split z div --min-z 0.8
# --min-div 0.05`, n=28): WR, the mean payoff per $1 on a win, and a loss.
TICKET_P = 0.3214
TICKET_WIN = 6.26
TICKET_TRADES_DAY = 5.5
TICKET_STAKE = 1.0
TICKET_STOP = 3.0

IMPULSE_TRADES_DAY = 12.0
DEPOSIT = 25.0          # venue order minimum for the main arm's floor
DAY_CAP_PCT = 0.20      # the guard's daily loss cap, as a fraction of bankroll


def impulse_samples(db: str) -> list[tuple[float, bool]]:
    """The live path's actual entries: (ask paid, won)."""
    con = sqlite3.connect(Path(db))
    rounds = con.execute(
        "SELECT slug, start_ms, end_ms, strike, winner FROM rounds "
        "WHERE symbol='BTCUSDT' AND winner IN ('Up','Down') AND strike > 0 "
        "ORDER BY start_ms").fetchall()
    sig = sigma_1m_by_round(con, rounds)
    fallback = st.median(sig.values()) if sig else 0.0
    ents = build_entries(con, rounds, sig, fallback, settings.impulse_window_s)
    margin = settings.limit_take_margin
    out = [(e["ask"], e["won"]) for e in ents
           if e["ask"] and e["fair"] is not None
           and e["ask"] <= e["fair"] - margin]
    con.close()
    return out


def run(paths: int, days: int, bankroll: float, impulse: list[tuple[float, bool]],
        seed: int = 7, stake_cap: float | None = None) -> dict:
    """Compound the account day by day over `paths` simulated runs.

    Each trade's stake is re-read from the CURRENT balance, exactly as
    `sizing_mode=fraction` does, so a good day makes tomorrow's bets bigger and
    a bad day makes them smaller. The two rails are applied per day: the guard's
    loss cap stops the main arm, the ticket arm's own stop stops the tickets.
    """
    rnd = random.Random(seed)
    tilt = (settings.tilt_min + settings.tilt_max) / 2.0
    cap = settings.stake_cap_usdt if stake_cap is None else stake_cap
    ends, losing, busts, worst_dd = [], 0, 0, 0.0
    n_imp = int(IMPULSE_TRADES_DAY)
    n_tk = int(TICKET_TRADES_DAY) + (1 if TICKET_TRADES_DAY % 1 else 0)
    for _ in range(paths):
        bal, peak = bankroll, bankroll
        for _day in range(days):
            day_start = bal
            imp_pnl = 0.0
            for _t in range(n_imp):
                stake = min(max(settings.min_order_usdt,
                                settings.stake_fraction * bal * tilt),
                            cap)
                if stake > bal:
                    break
                ask, won = rnd.choice(impulse)
                pnl = stake * ((0.98 / ask - 1.0) if won else -1.0)
                imp_pnl += pnl
                bal += pnl
                if imp_pnl <= -DAY_CAP_PCT * day_start:
                    break                     # the guard halts the main arm
            tk_pnl = 0.0
            for _t in range(n_tk):
                if bal < TICKET_STAKE:
                    break
                pnl = TICKET_STAKE * (TICKET_WIN if rnd.random() < TICKET_P
                                      else -1.0)
                tk_pnl += pnl
                bal += pnl
                if tk_pnl <= -TICKET_STOP:
                    break                     # the ticket arm's own $3 stop
            peak = max(peak, bal)
            if peak > 0:
                worst_dd = max(worst_dd, (peak - bal) / peak)
        ends.append(bal)
        if bal < 1.0:
            busts += 1
        if bal < bankroll:
            losing += 1
    ends.sort()
    return {
        "days": days, "paths": paths, "start": bankroll, "tilt": tilt,
        "median": ends[len(ends) // 2], "p05": ends[int(0.05 * len(ends))],
        "p95": ends[int(0.95 * len(ends))], "mean": sum(ends) / len(ends),
        "losing": losing / paths, "busts": busts / paths,
        "worst_dd": worst_dd, "n_imp": len(impulse),
    }


def show(r: dict) -> None:
    print(f"  {r['days']:>3} days  start ${r['start']:.2f}  ->  "
          f"median ${r['median']:.2f}  (p05 ${r['p05']:.2f}, "
          f"p95 ${r['p95']:.2f})   x{r['median'] / r['start']:.2f}   "
          f"P(loss) {100 * r['losing']:.1f}%   P(account < $1) "
          f"{100 * r['busts']:.1f}%   worst drawdown {100 * r['worst_dd']:.0f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--bankroll", type=float, default=None,
                    help="default: read the last balance the bot logged")
    ap.add_argument("--paths", type=int, default=4000)
    ap.add_argument("--max-stake", type=float, default=None,
                    help="cap the per-trade stake (liquidity assumption); "
                         "default is settings.stake_cap_usdt")
    args = ap.parse_args()

    bankroll = args.bankroll
    if bankroll is None:
        import re
        log = Path("data/live.log")
        found = re.findall(r"balance \$([0-9.]+)", log.read_text()[-200_000:]) \
            if log.exists() else []
        bankroll = float(found[-1]) if found else 53.49
    imp = impulse_samples(args.db)
    print(f"compounding projection: stake = {settings.stake_fraction:.0%} of the "
          f"bankroll x tilt {((settings.tilt_min + settings.tilt_max) / 2):.2f}, "
          f"floored at ${settings.min_order_usdt:.2f}")
    print(f"impulse resampled from {len(imp)} real entries "
          f"({IMPULSE_TRADES_DAY:.0f}/day); ticket {TICKET_P:.2%} WR, "
          f"+{TICKET_WIN} / -1 on ${TICKET_STAKE:.2f} "
          f"({TICKET_TRADES_DAY:.1f}/day, ${TICKET_STOP:.0f}/day stop)")
    print(f"rails: guard halts the main arm at -{DAY_CAP_PCT:.0%} of the "
          f"day-start bankroll\n")
    print(f"  {'window':<8} {'median':>9} {'p05':>8} {'p95':>9} {'x':>6} "
          f"{'P(loss)':>8} {'P(<$1)':>7} {'worst DD':>9}")
    for days in (1, 7, 30, 90):
        r = run(args.paths, days, bankroll, imp, stake_cap=args.max_stake)
        print(f"  {days:>3} days  ${r['median']:>8.2f} ${r['p05']:>7.2f} "
              f"${r['p95']:>8.2f} x{r['median'] / bankroll:>5.2f} "
              f"{100 * r['losing']:>7.1f}% {100 * r['busts']:>6.1f}% "
              f"{100 * r['worst_dd']:>8.0f}%")
    print("\n  (median is the middle path; the p05 column is the one to plan")
    print("   for. P(<$1) is the share of paths that cannot place another order.)")


if __name__ == "__main__":
    main()
