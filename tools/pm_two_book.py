"""A replay that models BOTH books -- the gate's and the venue's.

WHY THIS HAD TO EXIST. Every other replay here prices on `quotes.*_ask`, and
that column tracks the VENUE (verified: quotes-minus-venue median 0.0000 with 43
exact matches, quotes-minus-ours median +0.0050 with 23). So the tape knows what
we PAY and not what the gate SEES -- and the whole venue-re-base question is
about the difference between them. A one-book replay cannot arbitrate it, which
is why it sat unresolved.

THE BRIDGE is the `shadow book` log line, which prints both books at the same
instant. 305 snapshots give the gap directly:
    venue - ours : median +0.0400, mean +0.0883, p10 -0.0200, p90 +0.2700
    ours is BELOW the venue 67% of the time, equal 15%, above 18%
So the model is: the tape's ask IS the venue; our ladder is that minus a gap
drawn from this empirical distribution. The gate then decides on the ladder, and
the order pays the venue -- which is what actually happens.

WHAT IT ARBITRATES. `_place_order` re-bases the cap on the venue's ask:
    cap = self._take_cap_for(r, venue_ask, fill_cap)      # REPLACES
When the venue sits above our ladder this ENLARGES the ceiling the decision
authorised. The alternative is a ratchet -- `min(decision_cap, venue_cap)` --
which turns those into misses instead of dearer fills. Live evidence could not
separate them: raw, re-based fills look worse (n=79 -0.1048/fill against n=101
-0.0568), but controlling for the paid price the buckets disagree (+0.1307 vs
-0.2266 at 0.5; -0.1175 vs -0.0007 at 0.6).

    python3 tools/pm_two_book.py
"""
from __future__ import annotations

import datetime as dt, json, math, os, sys
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tools"))
from pm_full_chain import LIVE  # noqa
from pm_exit_chain import build  # noqa
from pm_exit_models import exit_at  # noqa

FEE = lambda p: 0.07 * p * (1 - p)
GAPS = np.array([g for g, _age in json.load(
    open(os.path.join(ROOT, "data", ".ladder-gaps.json")))])


def take_cap(base, *, buf_inv, buf_abs, max_price, fill_cap):
    """main.py's `_take_cap_for` / `_cross_tick` ceiling arithmetic."""
    buf = max(0.01, min(buf_abs, buf_inv * (1.0 - base)))
    cap = min(max_price, round(base + buf, 2))
    return min(cap, fill_cap) if fill_cap > 0 else cap


def sim(R, *, rebase, days=None, bankroll=22.6, expo=0.50, stake=1.0, seed=7,
        px_lo=None, px_hi=None, fill_cap=None):
    px_lo = LIVE["cross_min_price"] if px_lo is None else px_lo
    px_hi = LIVE["cross_max_price"] if px_hi is None else px_hi
    fill_cap = LIVE["cross_max_fill_px"] if fill_cap is None else fill_cap
    bi, ba = LIVE["cross_limit_buffer_inv"], LIVE["cross_limit_buffer"]
    rng = np.random.default_rng(seed)
    ev = []
    for end, cands, ticks, slug in R:
        for c in cands:
            (ts, tau, side, am, ask, won, age, dok, dbps, flip, gap, ti, rng_) = c
            if not (LIVE["cross_min_tau_s"] <= tau <= LIVE["cross_max_tau_s"]):
                continue
            if age > LIVE["max_spot_age_s"] or am < LIVE["cross_min_margin_bps"]:
                continue
            if not dok:
                continue
            md = LIVE["cross_max_depth_bps"]
            if md > 0 and dbps > md:
                continue
            venue = float(ask)                       # the tape IS the venue
            ladder = round(venue - float(rng.choice(GAPS)), 2)
            if not (px_lo <= ladder <= px_hi):       # the GATE sees the ladder
                continue
            ev.append((ts, end, side, won, venue, ladder, ticks, ti))
            break
    ev.sort(key=lambda x: (x[0], x[1]))
    slots, tot, n, wins, miss, per, dd = [], 0.0, 0, 0, 0, [], []
    for ts, end, side, won, venue, ladder, ticks, ti in ev:
        d = dt.datetime.fromtimestamp(ts / 1000, dt.UTC).strftime("%m-%d")
        if days is not None and d not in days:
            continue
        slots = [e for e in slots if e > ts]
        s = stake * len(slots)
        if s > 0 and s + stake > expo * bankroll:
            continue
        dec_cap = take_cap(ladder, buf_inv=bi, buf_abs=ba,
                           max_price=px_hi, fill_cap=fill_cap)
        ven_cap = take_cap(venue, buf_inv=bi, buf_abs=ba,
                           max_price=px_hi, fill_cap=fill_cap)
        cap = ven_cap if rebase else min(dec_cap, ven_cap)
        if venue > cap:                              # FAK finds nothing
            miss += 1
            continue
        pay = min(max(venue, 0.01), 0.99)
        sh = stake / pay
        x = exit_at(ticks, ti, side, pay, "margin", LIVE["cross_exit_margin_bps"],
                    LIVE["cross_min_margin_bps"], LIVE["cross_exit_slip"],
                    LIVE["cross_exit_min_tau_s"])
        r = ((x[1] - pay) - FEE(pay)) / pay if x else \
            ((1.0 if won else 0.0) - pay - FEE(pay)) / pay
        tot += r * sh; per.append(r); wins += r > 0; n += 1
        slots.append(x[0] if x else end); dd.append(d)
    if n < 20:
        return None
    v, dv = np.array(per), np.array(dd)
    sd = v.std(ddof=1)
    u = np.unique(dv); dm = np.array([v[dv == x].mean() for x in u])
    return dict(n=n, miss=miss, win=100 * wins / n, tot=tot, per=v.mean(),
                t=v.mean() / (sd / math.sqrt(n)) if sd else 0.0,
                dayt=dm.mean() / (dm.std(ddof=1) / math.sqrt(len(dm)))
                if len(dm) > 1 and dm.std(ddof=1) else 0.0)


def main():
    print("  gap model: n=%d  median %+.4f  mean %+.4f\n"
          % (len(GAPS), float(np.median(GAPS)), float(GAPS.mean())))
    for look in (LIVE["cross_look_s"], 2.0):
        R = build(look, LIVE["cross_hold_s"], 10)
        days = sorted({dt.datetime.fromtimestamp(c[0] / 1000, dt.UTC).strftime("%m-%d")
                       for _, cs, _, _ in R for c in cs})
        TE = set(days[len(days) // 2:])
        print("  === look %.1f ===" % look)
        print("  %-26s %-34s %s" % ("", "FULL TAPE", "TEST (unseen)"))
        for rebase, lab in ((True, "re-base ON (ships)"), (False, "ratchet (min)")):
            accs = []
            for sd_ in (7, 11, 13, 17, 23):          # average over gap draws
                f = sim(R, rebase=rebase, seed=sd_)
                t = sim(R, rebase=rebase, seed=sd_, days=TE)
                if f and t:
                    accs.append((f, t))
            if not accs:
                print("  %-26s n<20" % lab); continue
            m = lambda k, i: float(np.mean([a[i][k] for a in accs]))
            print("  %-26s n=%4.0f miss=%4.0f win%5.1f%% $%+7.2f t%+5.2f   "
                  "n=%4.0f win%5.1f%% $%+7.2f"
                  % (lab, m("n", 0), m("miss", 0), m("win", 0), m("tot", 0),
                     m("t", 0), m("n", 1), m("win", 1), m("tot", 1)))
        print()


if __name__ == "__main__":
    main()
