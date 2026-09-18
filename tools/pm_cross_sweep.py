"""Full parameter sweep of the CROSS arm, with the battery applied per cell.

WHY THIS EXISTS
The cross arm was rejected on a single parameterisation plus a latency sweep.
That is not a sweep of the strategy: `look_s`, the tau window, the price gate
and the crossing margin were all pinned at one cell. "The win rate is flat
across entry lags" is a statement about TIMING and says nothing about whether
some parameter region has a win rate that beats its own break-even.

So this sweeps the real knobs and, for every cell, applies the checks that
decide whether a result is real:
    pooled EV and t, BOTH halves separately, and n.
and then the check that actually matters for a sweep (see
backtest-artifacts.md #5): a PARAMETER must rank the SAME WAY in both halves.
Otherwise the sweep is fitting noise.

SPEED, AND WHY IT IS SAFE
The trigger only changes state when a new 1-second spot bar completes, so the
fire side is a property of the SECOND, not of the book row. Precomputing one
label per second makes a 360-cell grid feasible. That is an optimisation of
the measurement, so it is VERIFIED against the real `crossed_side` from
btcpredict.main before anything is reported (`--verify`, on by default).

KNOWN LIMITATION, STATED UP FRONT
With a 1-second spot grid, look_s below 1.0 s is not resolvable: the reference
tick becomes the newest tick itself, the two sides always agree, and the
trigger goes silent. The live arm sees ~114 websocket ticks/s where look_s
< 1 s is meaningful. So the look_s sweep starts AT 1.0 and the low end is
simply not testable on this data.
"""
from __future__ import annotations

import argparse
import itertools
import math
import statistics as st
import sys

import numpy as np

from tools.pm_cross_backtest import (
    PM_FEE_RATE, breakeven_win_rate, load_spot, round_window, tape_date,
)
from tools.pm_tape import episodes, episode_winner, load
from btcpredict.main import crossed_side


def build_round(path, bars, interval_s, looks, max_spot_ticks=12):
    """Everything one round needs, with the trigger reduced to per-second."""
    d = load(path)
    if d.empty:
        return None
    winner = episode_winner(d)
    if winner is None:
        return None
    start_s, end_s = round_window(d, interval_s)
    if start_s not in bars.index or end_s > bars.index.max() + 1:
        return None
    first_s = int(d["ts"].iloc[0] // 1000)
    if not (start_s - 1 <= first_s <= start_s + 3):
        return None
    seg = bars.loc[start_s:end_s - 1]
    if len(seg) < interval_s // 2:
        return None

    strike = float(bars.loc[start_s, "o"])
    opens = seg["o"].to_numpy(dtype="float64")
    tick_ms = (seg.index.to_numpy() * 1000).astype("int64")

    # per-second fire label for each look_s: which side did spot cross INTO.
    # Tie convention MUST match crossed_side exactly: it uses
    # `"Up" if px > strike else "Down"`, so px == strike counts as DOWN. A
    # `b >= strike` test looks equivalent and is not -- with 2-decimal prices
    # on a 1-second grid the equality case fired 296 times out of 24000.
    def side_of(px: float) -> str:
        return "Up" if px > strike else "Down"

    fires: dict[float, np.ndarray] = {}
    for L in looks:
        Ls = max(1, int(round(L)))
        f = np.empty(len(opens), dtype=object)
        f[:] = None
        for k in range(Ls, len(opens)):
            now, ref = side_of(opens[k]), side_of(opens[k - Ls])
            if now != ref:
                f[k] = now
        fires[L] = f

    ts = d["ts"].to_numpy()
    sec = ((ts // 1000) - start_s).astype("int64")

    # Row indices where a fire is visible. Scanning all ~98k rows per cell made
    # a 360-cell grid impossible; only rows whose SECOND carries a fire can
    # ever trade, and there are a few dozen of those per round.
    fire_rows: dict[float, np.ndarray] = {}
    ok = (sec >= 0) & (sec < len(opens))
    for L in looks:
        has = np.zeros(len(opens), dtype=bool)
        for k in range(len(opens)):
            has[k] = fires[L][k] is not None
        m = np.zeros(len(ts), dtype=bool)
        m[ok] = has[sec[ok]]
        fire_rows[L] = np.nonzero(m)[0]

    return {
        "winner": "Up" if winner == "YES" else "Down",
        "strike": strike, "start_s": start_s, "end_s": end_s,
        "opens": opens, "tick_ms": tick_ms, "fires": fires,
        "fire_rows": fire_rows,
        "ts": ts, "tau": d["tau_s"].to_numpy(dtype="float64"),
        "sec": sec,
        "ask": {
            "Up": d["yes_ask"].to_numpy(dtype="float64"),
            "Down": d["no_ask"].to_numpy(dtype="float64"),
        },
        "sz": {
            "Up": d["top_1_yes_ask_size"].to_numpy(dtype="float64"),
            "Down": d["top_1_no_ask_size"].to_numpy(dtype="float64"),
        },
    }


def verify(rounds, looks, limit=6, verbose=True):
    """Assert the per-second label reproduces `crossed_side` row by row."""
    bad = tot = 0
    for R in rounds[:limit]:
        ts = R["ts"]
        for L in looks:
            # ptr MUST restart per look_s: `i` restarts at 0 each time, so a
            # carried-over ptr would hand crossed_side ticks that are in the
            # FUTURE of ts[i]. That bug made the verifier disagree with itself.
            ptr = 0
            f = R["fires"][L]
            tick_ms, opens = R["tick_ms"], R["opens"]
            for i in range(0, len(ts), 97):        # sample rows, not all
                while ptr < len(tick_ms) and tick_ms[ptr] <= ts[i]:
                    ptr += 1
                lo = max(0, ptr - 12)
                ticks = list(zip(tick_ms[lo:ptr].tolist(),
                                 opens[lo:ptr].tolist()))
                real = crossed_side(ticks, R["strike"], float(L), 2.0)
                k = int(R["sec"][i])
                fast = f[k] if 0 <= k < len(f) else None
                tot += 1
                if real != fast:
                    bad += 1
                    if verbose and bad <= 5:
                        print(f"   MISMATCH sec={k} real={real} fast={fast}")
    print(f"  fast-path check: {tot - bad}/{tot} rows agree with "
          f"crossed_side  ({'OK' if bad == 0 else 'FAILED'})")
    return bad == 0


def cell(rounds, *, look, min_tau, max_tau, max_px, min_px, lag_ms,
         min_size, interval_s):
    """Every trade this parameter cell would take. One entry per round."""
    out = []
    for R in rounds:
        f = R["fires"][look]
        ts, tau = R["ts"], R["tau"]
        for i in R["fire_rows"][look]:
            t = tau[i]
            if t < min_tau or t > max_tau:
                continue
            side = f[R["sec"][i]]
            if side is None:
                continue
            i0 = int(np.searchsorted(ts, ts[i] + lag_ms, side="right")) - 1 \
                if lag_ms else i
            if i0 < i:
                i0 = i
            px = R["ask"][side][i0]
            if not (px == px) or px <= 0 or px >= 1:
                continue
            if px < min_px or px > max_px:
                continue
            if R["sz"][side][i0] < min_size:
                continue
            won = 1.0 if side == R["winner"] else 0.0
            out.append((R["start_s"], px, won,
                        won * (1 - px) / px - (1 - won) - PM_FEE_RATE * (1 - px)))
            break
    out.sort()
    return out


def stats(trades):
    if not trades:
        return dict(n=0, ev=float("nan"), t=float("nan"), win=float("nan"),
                    be=float("nan"), ev1=float("nan"), ev2=float("nan"))
    ev = [x[3] for x in trades]
    n = len(ev)
    mean = st.fmean(ev)
    sd = st.stdev(ev) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n else 0.0
    h = n // 2
    e1 = st.fmean(x[3] for x in trades[:h]) if h else float("nan")
    e2 = st.fmean(x[3] for x in trades[h:]) if n - h else float("nan")
    return dict(n=n, ev=mean, t=(mean / se if se else float("nan")),
                win=st.fmean(x[2] for x in trades),
                be=st.fmean(breakeven_win_rate(x[1]) for x in trades),
                ev1=e1, ev2=e2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="btc")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--lag-ms", type=int, default=300,
                    help="our measured hot path is 280-300 ms")
    ap.add_argument("--min-size", type=float, default=5.0)
    ap.add_argument("--min-n", type=int, default=25,
                    help="cells thinner than this are not read")
    ap.add_argument("--top", type=int, default=18)
    ap.add_argument("--no-verify", action="store_true")
    a = ap.parse_args()

    interval_s = 300 if a.interval == "5m" else 900
    day = tape_date(a.asset, a.interval)
    bars = load_spot(a.asset, day)
    paths = episodes(a.asset, a.interval)

    grid = dict(
        look=[1.0, 2.0, 3.0, 5.0],
        min_tau=[5.0, 15.0, 30.0],
        max_tau=[30.0, 60.0, 120.0],
        max_px=[0.45, 0.55, 0.65, 0.75, 0.90],
        min_px=[0.10, 0.25],
    )
    keys = list(grid)
    cells = list(itertools.product(*(grid[k] for k in keys)))
    print(f"CROSS sweep  {a.asset}/{a.interval} {day}   lag {a.lag_ms} ms")
    print(f"  {len(paths)} episodes   {len(cells)} cells   "
          f"min_n {a.min_n}")

    rounds = []
    for p in paths:
        R = build_round(p, bars, interval_s, grid["look"])
        if R:
            rounds.append(R)
    print(f"  usable rounds: {len(rounds)}")
    if not a.no_verify and not verify(rounds, grid["look"]):
        print("  fast path does not match the real trigger; refusing to sweep")
        return 1

    results = []
    for combo in cells:
        kw = dict(zip(keys, combo))
        tr = cell(rounds, look=kw["look"], min_tau=kw["min_tau"],
                  max_tau=kw["max_tau"], max_px=kw["max_px"],
                  min_px=kw["min_px"], lag_ms=a.lag_ms,
                  min_size=a.min_size, interval_s=interval_s)
        if len(tr) < a.min_n:
            continue
        s = stats(tr)
        s.update(kw)
        results.append(s)

    if not results:
        print("  no cell reached min_n")
        return 0

    print(f"\n  cells reaching min_n {a.min_n}: {len(results)}")
    pos = [r for r in results if r["ev"] > 0]
    both = [r for r in results if r["ev1"] > 0 and r["ev2"] > 0]
    print(f"  cells with EV > 0            : {len(pos)}/{len(results)} "
          f"({100*len(pos)/len(results):.0f}%)")
    print(f"  cells positive in BOTH halves: {len(both)}/{len(results)} "
          f"({100*len(both)/len(results):.0f}%)")
    print(f"  (a coin flip would give ~25% in both halves, and ~50% positive)")
    sig = [r for r in results if abs(r["t"]) >= 2.0]
    print(f"  cells with |t| >= 2          : {len(sig)} "
          f"({sum(1 for r in sig if r['ev'] > 0)} positive, "
          f"{sum(1 for r in sig if r['ev'] < 0)} negative)")

    print(f"\n  TOP {a.top} CELLS BY EV")
    print("    look min_tau max_tau max_px min_px   n    EV/$1     t     "
          "win%   be%   1st half  2nd half")
    for r in sorted(results, key=lambda x: -x["ev"])[:a.top]:
        print("   %5.1f %6.0f %7.0f %6.2f %6.2f %4d  %+8.4f %+6.2f  %5.1f  "
              "%5.1f  %+8.4f %+8.4f"
              % (r["look"], r["min_tau"], r["max_tau"], r["max_px"],
                 r["min_px"], r["n"], r["ev"], r["t"], 100 * r["win"],
                 100 * r["be"], r["ev1"], r["ev2"]))

    # ---- check #5: does the parameter RANK the same way in both halves? ----
    print("\n  PARAMETER RANKING, first half vs second half")
    print("    (a real parameter keeps its order; a fitted one flips)")
    for k in keys:
        vals = sorted(set(r[k] for r in results))
        if len(vals) < 2:
            continue
        rows = []
        for v in vals:
            sub = [r for r in results if r[k] == v]
            if not sub:
                continue
            rows.append((v, st.fmean(r["ev1"] for r in sub),
                         st.fmean(r["ev2"] for r in sub),
                         st.fmean(r["ev"] for r in sub)))
        if len(rows) < 2:
            continue
        o1 = [r[0] for r in sorted(rows, key=lambda x: -x[1])]
        o2 = [r[0] for r in sorted(rows, key=lambda x: -x[2])]
        same = o1 == o2
        print(f"\n    {k}: best-in-1st={o1[0]}  best-in-2nd={o2[0]}  "
              f"order {'SAME' if same else 'FLIPS'}")
        for v, e1, e2, e in rows:
            print(f"       {str(v):>6}  1st {e1:+.4f}   2nd {e2:+.4f}   "
                  f"pooled {e:+.4f}")

    print("\n  BASELINE (the shipped cell: look 1, tau 5-60, cap 0.65, min 0.10)")
    base = [r for r in results if r["look"] == 1.0 and r["min_tau"] == 5.0
            and r["max_tau"] == 60.0 and r["max_px"] == 0.65
            and r["min_px"] == 0.10]
    if base:
        b = base[0]
        print(f"    n={b['n']}  EV {b['ev']:+.4f}  t {b['t']:+.2f}  "
              f"win {100*b['win']:.1f}%  be {100*b['be']:.1f}%  "
              f"halves {b['ev1']:+.4f} / {b['ev2']:+.4f}")
    else:
        print("    baseline cell did not reach min_n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
