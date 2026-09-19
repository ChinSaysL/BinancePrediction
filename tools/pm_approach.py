"""ENTER ON APPROACH, NOT ON THE CROSSING -- does the edge survive the misses?

WHY THIS EXISTS. Measured on 266 live cross-arm entries, the ask on the side we
buy climbs through the seconds before our fill:

    -20.0s 0.3930 (+10.7 pts edge)   -3.0s 0.4566 (+4.3)
    -10.0s 0.4022  (+9.8)            -2.0s 0.4823 (+1.8)
     -5.0s 0.4276  (+7.2)            -1.5s 0.4987 (+0.1)  <- break-even
                                      fill 0.5719  (-7.2)

The signal is real; the venue prices it before we act. `crossed_side` needs
`look_s` (1-2s) of confirmation and the order needs ~1s, so the arm cannot
arrive before -1.5s and is a taker of a public print by construction.

⛔ THE OBJECTION THIS TOOL EXISTS TO ANSWER. That +10.7 is measured ONLY on
approaches that COMPLETED -- every one of those rounds had a crossing, because
that is how the entry got into the sample. Entering on approach also buys the
approaches that stall and reverse, and they are absent from the live record by
construction. The whole question is whether the cheaper price pays for them.

    python3 tools/pm_approach.py
"""
from __future__ import annotations

import datetime as dt, math, os, sys
import numpy as np
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tools"))
import pm_cross_replay as base                                          # noqa
from pm_exit_chain import load                                          # noqa
from pm_full_chain import LIVE                                          # noqa

FEE = lambda p: 0.07 * p * (1 - p)
_R = _Q = None


def _init(r, q):
    global _R, _Q
    _R, _Q = r, q


def _one(args):
    """Every APPROACH and every CROSSING in one round, priced at the ask we
    could actually have PAID -- `delay_ms` after the signal, not at it.

    ⛔ THE FIRST VERSION OF THIS TOOL PRICED AT THE SIGNAL TICK, which is the
    exact error it was written to investigate. The whole finding is that the
    venue reprices the crossing before we can act: the side's ask climbs from
    0.393 to 0.572 in the seconds before a live fill lands. Paying the tick's
    ask buys the pre-repricing price the arm demonstrably CANNOT get, and makes
    any early-entry rule look profitable by construction.
    Measured live: decision -> filled is 0.97s median, 1.92s p90.
    """
    idx, look, band, delay_ms = args
    row = _R.iloc[idx]
    pre = base.precompute(_Q, row, look, 0.0, 1.0, 0.0, 1e9)
    if pre is None:
        return None
    lvl, src, src_ts = pre["level"], pre["fc"], pre["fc_ts"]
    if not src or not lvl:
        return None
    rows = pre["rows"]
    winner = str(row.winner)
    # The ask ladder we will price INTO, indexed by time.
    q_ts = rows.ts_ms.to_numpy()
    q_ua = rows.up_ask.to_numpy(dtype=float)
    q_da = rows.down_ask.to_numpy(dtype=float)

    def ask_after(ts, side):
        """The first ask at or after `ts + delay_ms` -- what we would pay."""
        k = int(np.searchsorted(q_ts, int(ts) + delay_ms, side="left"))
        arr = q_ua if side == "Up" else q_da
        while k < len(arr):
            v = float(arr[k])
            if np.isfinite(v) and 0 < v < 1:
                return v
            k += 1
        return None
    out_app, out_crs = [], []
    prev_m = None
    fired_app = fired_crs = False
    for ts, tau, ua, da in zip(rows.ts_ms, rows.tau_s, rows.up_ask, rows.down_ask):
        if not (LIVE["cross_min_tau_s"] <= tau <= LIVE["cross_max_tau_s"]):
            continue
        j = int(np.searchsorted(src_ts, int(ts), side="right"))
        if j < 4:
            continue
        sig = src[j - 1][1]
        m = 1e4 * (sig - lvl) / lvl
        # ---- APPROACH: inside the band and SHRINKING toward the level.
        # The side bought is the one the signal is heading INTO, which is the
        # far side of the level from where it sits now.
        if (not fired_app and prev_m is not None and m != 0
                and abs(m) <= band and abs(m) < abs(prev_m)):
            side = "Up" if m < 0 else "Down"
            ask = ask_after(ts, side)
            if ask is not None:
                out_app.append((int(ts), side, ask, winner == side, abs(m), float(tau)))
                fired_app = True
        # ---- CROSSING: the arm as it runs today, same tape, same pricing.
        if not fired_crs and prev_m is not None and (prev_m < 0) != (m < 0):
            side = "Up" if m > 0 else "Down"
            ask = ask_after(ts, side)
            if ask is not None:
                out_crs.append((int(ts), side, ask, winner == side, abs(m), float(tau)))
                fired_crs = True
        prev_m = m
    return (out_app, out_crs)


def score(ev, lab, stake=1.0):
    if not ev:
        print("  %-28s no entries" % lab); return
    ask = np.array([e[2] for e in ev], float)
    won = np.array([e[3] for e in ev], bool)
    sh = stake / np.clip(ask, 0.01, 0.99)
    pnl = sh * (won.astype(float) - ask - FEE(ask))
    n = len(ev); se = math.sqrt(0.25 / n); gap = won.mean() - ask.mean()
    t = pnl.mean() / (pnl.std(ddof=1) / math.sqrt(n)) if n > 1 and pnl.std(ddof=1) else 0.0
    print("  %-28s n=%4d  ask %.3f  win %5.1f%%  gap %+5.1f (%+.2f sd)  $%+8.2f  t%+.2f"
          % (lab, n, ask.mean(), 100 * won.mean(), 100 * gap, gap / se, pnl.sum(), t))
    return pnl, won, ask


def main():
    r, q = load()
    print("rounds: %d   quotes: %d\n" % (len(r), len(q)))
    look = LIVE["cross_look_s"]
    # 0ms is the OPTIMISTIC fiction (the price at the signal). 1000ms is the
    # measured live decision->fill. 2000ms is its p90. All three are shown so
    # the reader can see how much of any edge is just the fiction.
    for delay in (0, 1000, 2000):
        print("#" * 78)
        print("# FILL PRICED %d ms AFTER THE SIGNAL%s" % (delay,
              "   <- OPTIMISTIC FICTION, the arm cannot get this" if delay == 0
              else ("   <- the measured live decision->fill" if delay == 1000
                    else "   <- live p90")))
        print("#" * 78)
        for band in (0.5, 1.0, 2.0, 4.0, 8.0):
            with ProcessPoolExecutor(4, initializer=_init, initargs=(r, q)) as ex:
                res = list(ex.map(_one, [(i, look, band, delay) for i in range(len(r))],
                                  chunksize=24))
            app = [e for x in res if x for e in x[0]]
            crs = [e for x in res if x for e in x[1]]
            print("=== approach band %.1f bps ===" % band)
            score(app, "  APPROACH (enter early)")
            if band == 0.5:
                score(crs, "  CROSSING (today's arm)")
            print()


if __name__ == "__main__":
    main()
