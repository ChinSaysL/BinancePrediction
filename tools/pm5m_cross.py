"""The CROSS arm over 11 independent days, with a DAY-CLUSTERED test.

The verdict on this arm could not be settled on one day. `pm_cross_backtest`
and `pm_cross_sweep` both worked on the kartik tape (2026-07-17 only), so the
only split available was within-day -- far weaker than a day split, and the
day-clustered t that promotes or kills a parameter was impossible.

`data/btc5m` fixes that: 11 days x 288 rounds = 3,168 rounds, each with 1-second
Binance spot and the round's real Polymarket trades.

THE FOUR CHECKS, all now runnable:
    1. pooled t
    2. DAY-CLUSTERED t   (per-day mean, then t across days) <- the new one
    3. halves split BY DAY, not by trade
    4. drop the best day

WHAT THE ENTRY PRICE IS, AND IS NOT:
there is no book in this dataset, so the entry is the LAST TRADE price at or
before the entry instant. A taker pays the ask, at or above the last trade, so
this is OPTIMISTIC by roughly half the spread. `--penalty` adds a fixed amount
to the entry price and the default run reports the sensitivity rather than
hiding it.

SETTLEMENT RULE: the market description for these rounds is explicit --
"resolve to Up if the Bitcoin price at the END of the time range is greater than
or equal to the price at the BEGINNING". That is a POINT rule, the same shape
as the Binance wrapper's, NOT the 60 s Chainlink TWAP the venue package
docstring claims. Measured on 864 rounds: last-close-vs-strike matches PM's own
resolution 96.1% of the time, TWAP-vs-strike only 77.7%.
"""
from __future__ import annotations

import argparse
import math
import statistics as st
from collections import defaultdict

import numpy as np

from tools.pm5m_tape import ROUND_S, dates, meta, price_at, price_series, rounds, spot, trades

FEE_RATE = 0.07


def load_round(rd, looks):
    """Compact per-round record: spot opens + the two last-trade price series."""
    m = meta(rd)
    if not m:
        return None
    s = spot(rd)
    if s is None or len(s) < 200:
        return None
    opens = s["open"].to_numpy(dtype="float64")
    if len(opens) < 200 or not np.isfinite(opens[0]) or opens[0] <= 0:
        return None
    strike = float(opens[0])
    start = int(rd.name)

    # fires are a property of the SECOND, not of a book row
    fires = {}
    for L in looks:
        Ls = max(1, int(round(L)))
        f = np.full(len(opens), None, dtype=object)
        for k in range(Ls, len(opens)):
            now = "Up" if opens[k] > strike else "Down"
            ref = "Up" if opens[k - Ls] > strike else "Down"
            if now != ref:
                f[k] = now
        fires[L] = f

    t = trades(rd)
    series = None
    if t is not None and len(t):
        series = {"Up": price_series(t, m["up_token"]),
                  "Down": price_series(t, m["down_token"])}
        if series["Up"].empty and series["Down"].empty:
            series = None
    return {
        "day": rd.parent.name, "start": start, "winner": m["winner"],
        "strike": strike, "opens": opens, "fires": fires,
        "series": series, "slug": m["slug"],
    }


def run_cell(R, *, look, min_tau, max_tau, max_px, min_px, lag_s,
             penalty, min_size, spread_adj):
    """One entry per round. Returns [(day, ev, won, px)]."""
    out = []
    n_sec = ROUND_S
    Ls = max(1, int(round(look)))
    for sec in range(Ls, n_sec):
        tau = n_sec - (sec + 1)
        if tau < min_tau or tau > max_tau:
            continue
        side = R["fires"][look][sec]
        if side is None:
            continue
        sr = R["series"]
        if sr is None:
            return out
        s = sr[side]
        if s.empty:
            return out
        ts = np.array([R["start"] + sec + lag_s], dtype="int64")
        px = float(price_at(s, ts)[0])
        if not np.isfinite(px) or px <= 0 or px >= 1:
            return out
        px = min(0.99, px + penalty)
        if px < min_px or px > max_px:
            return out
        won = 1.0 if side == R["winner"] else 0.0
        fee = FEE_RATE * (1.0 - px)          # per $1 staked
        out.append((R["day"], won * (1 - px) / px - (1 - won) - fee, won, px))
        break
    return out


def cluster(trades):
    """pooled t and day-clustered t."""
    if not trades:
        return dict(n=0, ev=float("nan"), t=float("nan"),
                    t_day=float("nan"), days=0, win=float("nan"),
                    px=float("nan"))
    ev = [x[1] for x in trades]
    n = len(ev)
    mean = st.fmean(ev)
    sd = st.stdev(ev) if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd else float("nan")
    by = defaultdict(list)
    for x in trades:
        by[x[0]].append(x[1])
    dm = [st.fmean(v) for v in by.values()]
    nd = len(dm)
    if nd > 1 and st.stdev(dm) > 0:
        t_day = st.fmean(dm) / (st.stdev(dm) / math.sqrt(nd))
    else:
        t_day = float("nan")
    return dict(n=n, ev=mean, t=t, t_day=t_day, days=nd,
                win=st.fmean(x[2] for x in trades),
                px=st.fmean(x[3] for x in trades))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="0 = all trade-days")
    ap.add_argument("--look", type=float, default=1.0)
    ap.add_argument("--min-tau", type=float, default=5.0)
    ap.add_argument("--max-tau", type=float, default=60.0)
    ap.add_argument("--max-px", type=float, default=0.65)
    ap.add_argument("--min-px", type=float, default=0.10)
    ap.add_argument("--lag-s", type=int, default=0)
    ap.add_argument("--min-size", type=float, default=5.0)
    ap.add_argument("--sweep", action="store_true")
    a = ap.parse_args()

    all_days = dates()
    trade_days = [d for d in all_days if any(
        (rd / "trades_raw.json").exists() for rd in rounds(d)[:1])]
    if a.days:
        trade_days = trade_days[:a.days]
    looks = sorted({1.0, 2.0, 3.0, 5.0, a.look})
    print(f"CROSS on data/btc5m   {len(trade_days)} trade-days: "
          f"{trade_days[0]} .. {trade_days[-1]}")
    print(f"  entry = last PM trade price at the fire second (+{a.lag_s}s lag), "
          f"no book in this dataset")

    data = []
    for d in trade_days:
        got = 0
        for rd in rounds(d):
            R = load_round(rd, looks)
            if R and R["series"] is not None:
                data.append(R)
                got += 1
        print(f"  {d}  rounds with trades+spot: {got}")

    pen = [0.0, 0.005, 0.01]
    print()
    print("=== THE SHIPPED CELL, with spread-penalty sensitivity ===")
    print("  penalty   n     EV/$1    pooled t   DAY-clustered t   days   win%   px")
    base = None
    for p in pen:
        tr = []
        for R in data:
            tr += run_cell(R, look=a.look, min_tau=a.min_tau, max_tau=a.max_tau,
                           max_px=a.max_px, min_px=a.min_px, lag_s=a.lag_s,
                           penalty=p, min_size=a.min_size, spread_adj=True)
        s = cluster(tr)
        if p == 0.0:
            base = (s, tr)
        print("  %+6.3f  %4d  %+8.4f  %+8.2f  %+14.2f  %5d  %5.1f  %6.4f"
              % (p, s["n"], s["ev"], s["t"], s["t_day"], s["days"],
                 100 * s["win"], s["px"]))

    if base:
        s, tr = base
        print()
        print("=== THE FOUR CHECKS at penalty 0 (best case) ===")
        by = defaultdict(list)
        for x in tr:
            by[x[0]].append(x[1])
        days = sorted(by)
        print("  per-day:")
        for d in days:
            v = by[d]
            print("    %s  n=%3d  EV %+8.4f  win %5.1f%%"
                  % (d, len(v), st.fmean(v),
                     100 * st.fmean([x[2] for x in tr if x[0] == d])))
        dm = [(d, st.fmean(by[d])) for d in days]
        dm.sort(key=lambda x: x[1])
        half = len(days) // 2
        h1 = st.fmean([v for _, v in dm[:half]])
        h2 = st.fmean([v for _, v in dm[half:]])
        print(f"\n  1. pooled t          {s['t']:+.2f}")
        print(f"  2. DAY-clustered t   {s['t_day']:+.2f}   "
              f"({s['days']} days)")
        print(f"  3. halves BY DAY     {h1:+.4f} / {h2:+.4f}   "
              f"({'both same sign' if h1 * h2 > 0 else 'OPPOSITE SIGNS'})")
        worst = dm[0]
        print(f"  4. drop best day     EV {st.fmean([v for _, v in dm[:-1]]):+.4f}"
              f"   (best day {dm[-1][0]} {dm[-1][1]:+.4f}, "
              f"worst {worst[0]} {worst[1]:+.4f})")

    if a.sweep:
        print()
        print("=== SWEEP, ranked by DAY-CLUSTERED t ===")
        print("  look min_tau max_tau max_px   n    EV/$1   pooled t  DAY t   days")
        rows = []
        for look in (1.0, 2.0, 3.0, 5.0):
            for mn in (5.0, 15.0, 30.0):
                for mx in (60.0, 120.0):
                    for mp in (0.45, 0.55, 0.65, 0.90):
                        tr = []
                        for R in data:
                            tr += run_cell(R, look=look, min_tau=mn,
                                           max_tau=mx, max_px=mp,
                                           min_px=a.min_px, lag_s=a.lag_s,
                                           penalty=0.0,
                                           min_size=a.min_size,
                                           spread_adj=True)
                        if len(tr) < 50:
                            continue
                        s = cluster(tr)
                        rows.append((look, mn, mx, mp, s))
        rows.sort(key=lambda r: -abs(r[4]["t_day"] if np.isfinite(r[4]["t_day"])
                                     else 0.0))
        for look, mn, mx, mp, s in rows[:20]:
            print("  %5.1f %6.0f %7.0f %6.2f %4d  %+8.4f  %+8.2f  %+7.2f  %5d"
                  % (look, mn, mx, mp, s["n"], s["ev"], s["t"], s["t_day"],
                     s["days"]))
        strong = [r for r in rows
                  if np.isfinite(r[4]["t_day"]) and abs(r[4]["t_day"]) >= 2.0]
        print(f"\n  cells with n>=50: {len(rows)}   "
              f"|DAY-clustered t| >= 2: {len(strong)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
