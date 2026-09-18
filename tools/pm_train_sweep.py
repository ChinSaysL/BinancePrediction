"""Train-arm parameter sweep for POLYMARKET's own resolution rule.

WHY THE RULE COMES FIRST. The train trigger is a DISTANCE condition --
`|spot - K| >= thr` bps -- so everything it earns depends on how often a move of
that size actually decides the round. That is a property of the venue's TERMINAL
VARIABLE, and Polymarket's is not Binance's:

  * Binance wrapper: the POINT price at the bell vs the range's start.
  * Polymarket:      a **60-second TAIL TWAP** vs the range's start.

`tools/pm_rule_ident.py` fitted four candidates to gamma's own published winners
(286 rounds, 2026-09-13) and the tail-60 TWAP won at **89.9%**, against 84.3%
for a whole-range TWAP and 83.9% for the point rule. `cryptoMarketConfig`
agrees: `twapLookbackSeconds = 60`.

That matters by a FACTOR, not a hair: a 60 s mean of a diffusion has variance
`sigma^2*60/3` where the whole-range one has `sigma^2*T/3`. On a 5-minute round
that is **5x less dispersed**, so a threshold calibrated on the Binance cells is
on a different bet. This tool prices the trigger under BOTH rules side by side so
the size of that mistake is visible rather than asserted.

WHAT THIS CANNOT DO, AND SAYS SO. Polymarket's contract PRICES do not exist
historically -- only Binance's wrapper quotes are recorded -- so a per-$1 EV is
not computable here. What IS computable is the WIN RATE under PM's rule, and
therefore the **break-even ask**: `ask_be = win_rate - fee`. A cell is only
tradeable if the venue quotes below that, and the live `filled_px` says whether
it does. Every table below is a win-rate table with that reading attached.

Checks applied (the repo's battery, see /memories/backtest-artifacts.md):
  pooled z, day-clustered t, first/second half, drop-best-day. A cell that does
  not survive all four is not reported as good news.

Usage:
    python3 tools/pm_train_sweep.py --days 2026-09-01..2026-09-15 --symbol BTCUSDT
"""
from __future__ import annotations

import argparse
import csv
import io
import math
import os
import statistics as st
import time
import zipfile
from collections import defaultdict

BARS = "data/bars_1s/daily"
PERIODS = {"5m": 300, "15m": 900}
#: The tail window Polymarket averages, from its own cryptoMarketConfig.
TWAP_LOOKBACK_S = 60
#: PM's taker fee is 0.07*p*(1-p) per share; at the ~0.6 asks this arm pays that
#: is ~1.7c, which is what the break-even column subtracts.
FEE_AT_06 = 0.07 * 0.6 * 0.4


def load_day(symbol: str, day: str):
    """[(open, close)] per second, plus the first second's index."""
    path = os.path.join(BARS, "%s-1s-%s.zip" % (symbol, day))
    if not os.path.exists(path):
        return None
    opens: dict[int, float] = {}
    closes: dict[int, float] = {}
    with zipfile.ZipFile(path) as z:
        with z.open(z.namelist()[0]) as fh:
            for row in csv.reader(io.TextIOWrapper(fh, "utf-8")):
                if not row or not row[0].isdigit():
                    continue
                t = int(row[0])
                if t > 1e15:
                    t //= 1_000_000
                elif t > 1e11:
                    t //= 1_000
                try:
                    opens[t] = float(row[1])
                    closes[t] = float(row[4])
                except (ValueError, IndexError):
                    continue
    return opens, closes


def rounds_for(day: str, symbols: list[str], tf: str):
    """Yield (symbol, day, start, K, pm_up, point_up, closes) per round."""
    period = PERIODS[tf]
    for sym in symbols:
        got = load_day(sym, day)
        if got is None:
            continue
        opens, closes = got
        if not opens:
            continue
        lo, hi = min(opens), max(closes)
        start = (lo // period) * period
        while start + period <= hi:
            px = [closes.get(start + i) for i in range(period)]
            k = opens.get(start)
            if k and all(p is not None for p in px):
                tail = px[-TWAP_LOOKBACK_S:]
                pm_up = (sum(tail) / len(tail)) >= k      # RULE C: tail TWAP
                pt_up = px[-1] >= k                       # RULE A: point (control)
                yield sym, day, start, k, pm_up, pt_up, px
            start += period


def triggers(px: list[float], k: float, thr: float,
             min_tau: float, max_tau: float, period: int):
    """The FIRST second the distance threshold is met inside the tau window.

    Mirrors the live trigger: a LEVEL (`|dist| >= thr`) evaluated inside a tau
    band, taken at the first second it is true -- which is also what makes the
    live `train_fresh_s` freshness clause pass. Returns (side, tau, dist_bps).
    """
    for i, p in enumerate(px):
        tau = period - i
        if tau > max_tau:
            continue
        if tau < min_tau:
            return None
        d = 1e4 * (p - k) / k
        if abs(d) >= thr:
            return ("Up" if d > 0 else "Down"), tau, d
    return None


def report(name: str, rows: list[tuple], key: str) -> dict:
    """rows = [(day, won_bool)]. Prints the four-check battery."""
    n = len(rows)
    if n < 20:
        print("  %-26s n=%-5d (too thin to read)" % (name, n))
        return {}
    wins = sum(1 for _, w in rows if w)
    wr = wins / n
    se = math.sqrt(max(wr * (1 - wr), 1e-9) / n)
    z = (wr - 0.5) / se
    by_day = defaultdict(list)
    for d, w in rows:
        by_day[d].append(1.0 if w else 0.0)
    order = sorted(by_day)
    dm = [sum(by_day[d]) / len(by_day[d]) for d in order]
    dt = (st.mean(dm) / (st.stdev(dm) / math.sqrt(len(dm)))
          if len(dm) > 1 and st.stdev(dm) > 0 else float("nan"))
    half = len(order) // 2
    h1 = [w for d in order[:half] for w in by_day[d]] if half else []
    h2 = [w for d in order[half:] for w in by_day[d]] if half else []
    wr1 = sum(h1) / len(h1) if h1 else float("nan")
    wr2 = sum(h2) / len(h2) if h2 else float("nan")
    best = max(order, key=lambda d: sum(by_day[d]) / len(by_day[d]))
    drop = [w for d in order if d != best for w in by_day[d]]
    wrd = sum(drop) / len(drop) if drop else float("nan")
    print("  %-26s n=%-5d win %5.1f%%  z %+5.2f  day-t %+5.2f  "
          "halves %5.1f/%5.1f  drop-best %5.1f  be-ask %5.3f"
          % (name, n, 100 * wr, z, dt, 100 * wr1, 100 * wr2, 100 * wrd,
             wr - FEE_AT_06))
    return {"n": n, "wr": wr, "dt": dt, "wr1": wr1, "wr2": wr2, "wrd": wrd}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", required=True, help="YYYY-MM-DD..YYYY-MM-DD")
    ap.add_argument("--symbol", default="BTCUSDT,ETHUSDT,BNBUSDT")
    ap.add_argument("--tf", default="5m,15m")
    a = ap.parse_args()

    lo_d, hi_d = a.days.split("..")
    days = []
    t = time.mktime(time.strptime(lo_d, "%Y-%m-%d"))
    hi_t = time.mktime(time.strptime(hi_d, "%Y-%m-%d"))
    while t <= hi_t:
        days.append(time.strftime("%Y-%m-%d", time.gmtime(t)))
        t += 86400
    symbols = [s.strip() for s in a.symbol.split(",")]
    tfs = [s.strip() for s in a.tf.split(",")]
    print("days %s..%s (%d)  symbols %s  tf %s\n"
          % (days[0], days[-1], len(days), ",".join(symbols), ",".join(tfs)))

    # LOAD EVERYTHING ONCE. `rounds_for` decompresses a day's zip, so calling
    # it per grid cell would re-read every file 35 times for nothing.
    cache: dict[str, list] = {}
    for tf in tfs:
        rows = []
        for day in days:
            rows.extend(list(rounds_for(day, symbols, tf)))
        cache[tf] = rows
        print("loaded %-4s %5d rounds" % (tf, len(rows)))
    print()

    # ---- the live setting first: what is running right now -----------------
    print("=== the LIVE setting: thr 15 bps, tau 60..unbounded, all families ===")
    for rule in ("pm", "point"):
        rows = []
        for sym, d, start, k, pm_up, pt_up, px in cache["5m"]:
            hit = triggers(px, k, 15.0, 60.0, 1e9, PERIODS["5m"])
            if hit is None:
                continue
            side, tau, dist = hit
            truth = pm_up if rule == "pm" else pt_up
            rows.append((d, (side == "Up") == truth))
        report("rule %s" % ("PM (tail-60 TWAP)" if rule == "pm"
                            else "Binance (point)"), rows, rule)

    # ---- the grids ---------------------------------------------------------
    for tf in tfs:
        period = PERIODS[tf]
        print("\n=== %s sweep, PM rule (tail-60 TWAP) ===" % tf)
        for min_tau, max_tau in ((60, 300), (60, 180), (60, 120),
                                 (120, 300), (30, 300)):
            for thr in (5.0, 7.5, 10.0, 12.5, 15.0, 20.0, 30.0):
                rows = []
                for sym, d, start, k, pm_up, pt_up, px in cache[tf]:
                    hit = triggers(px, k, thr, min_tau, max_tau, period)
                    if hit is None:
                        continue
                    side, tau, dist = hit
                    rows.append((d, (side == "Up") == pm_up))
                report("thr %5.1f  tau %3d-%3d" % (thr, min_tau, max_tau),
                       rows, "%s|%s|%s" % (tf, thr, min_tau))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
