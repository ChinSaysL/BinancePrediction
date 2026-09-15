"""IDEA 1: does a STRICT limit (<= +1c) fix the execution leak, or worsen it?

The proposal. The arm derives `slippageBps` from its own ceiling
(`room = max_px/quote - 1`), so a 0.61 quote authorises a fill up to 0.63. The
claim is that this creates an inverted selection filter:

  * a DOMINANT cross explodes +10c..+35c instantly, blows through the ceiling and
    the order fails  -> we systematically reject our winners
  * a WEAK cross hesitates or drifts back, never trips the ceiling, and the venue
    fills us at the TOP of the band -> we are left holding the losers

The proposed fix is to cap the tolerance at <= +0.5c..+1.0c and kill the trade
otherwise: "taking fewer fills at true fair value beats 44 fills at 0.623".

That prediction is testable, and it makes a sharp falsifiable claim: if the
ceiling is what selects against us, then tightening it should select FOR us --
the strict-limit filled subset must win MORE often than the population. If it
wins LESS often, the ceiling is not the selection mechanism.

Two measurements, because the live tape is ~1s resolution and the venue's head
start is 249ms:

  A. TAPE. For the first cross trigger of each round in tau 5-60, take the ask A.
       arrive -- immediate limit, one shot: fills only if the NEXT quote's ask is
                 <= A+cap. ~1s on this tape is a CONSERVATIVE stand-in for 249ms,
                 so it overstates rejection and is kind to the proposal.
       rest   -- the limit rests `--wait` seconds and takes the first quote whose
                 ask is <= A+cap
  B. LOG. `cross fine` records the ask path at 50ms resolution for 1s. That is the
     exact 250ms test the venue latency calls for: fill iff
     ask(+250ms) <= ask(+50ms) + cap, priced at ask(+250ms).

    python3 tools/cross_limit_sim.py --db data/btcpredict_vps.db
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import itertools
import math
import re
import sqlite3
import statistics
import sys

sys.path.insert(0, ".")
from tools.cross_geometry import crossed_side  # noqa: E402

CAPS = [float("inf"), 0.05, 0.02, 0.01, 0.005, 0.0]


def day_t(rows):
    by = collections.defaultdict(list)
    for ts, ev in rows:
        d = dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).date()
        by[d].append(ev)
    means = [statistics.fmean(v) for v in by.values() if v]
    if len(means) < 2:
        return float("nan")
    sd = statistics.stdev(means)
    if sd == 0:
        return float("nan")
    return statistics.fmean(means) / (sd / math.sqrt(len(means)))


def show(rows_by_cap, n_trig, title, fee=True):
    print(f"\n=== {title} ===")
    hdr = (f"{'cap':>18s} {'fills':>6s} {'rate':>7s} {'paid':>7s} {'win':>7s} "
           f"{'net/sh':>8s} {'net/$1':>8s} {'day-t':>7s}")
    print(hdr)
    print("-" * len(hdr))
    for cap in CAPS:
        rs = rows_by_cap.get(cap) or []
        label = "inf (population)" if cap == float("inf") else f"+{cap*100:.1f}c"
        if not rs:
            print(f"{label:>18s} {0:6d} {0.0:7.1%}")
            continue
        paid = statistics.fmean(r[1] for r in rs)
        win = statistics.fmean(1.0 if r[2] else 0.0 for r in rs)
        net = statistics.fmean(
            (1.0 if r[2] else 0.0) - r[1] - (r[3] * r[1] if fee else 0.0) for r in rs)
        per1 = statistics.fmean(
            ((1.0 if r[2] else 0.0) - r[1] - (r[3] * r[1] if fee else 0.0)) / r[1]
            for r in rs)
        t = day_t([(r[0], (1.0 if r[2] else 0.0) - r[1]) for r in rs])
        print(f"{label:>18s} {len(rs):6d} {len(rs)/n_trig:7.1%} {paid:7.3f} "
              f"{win:7.1%} {net:+8.3f} {per1:+8.3f} {t:+7.2f}")


def tape(args):
    c = sqlite3.connect(args.db)
    rounds = {
        s: (k, w)
        for s, k, w in c.execute(
            "select slug, strike, winner from rounds "
            "where winner in ('Up','Down') and strike > 0")
    }
    sql = ("select slug, ts_ms, tau_s, spot, up_ask, down_ask, fee_rate_bps from quotes "
           "where up_ask is not null and down_ask is not null and spot > 0 "
           "order by slug, ts_ms")
    out = {"arrive": collections.defaultdict(list),
           "rest": collections.defaultdict(list)}
    n = 0
    for slug, grp in itertools.groupby(c.execute(sql), key=lambda r: r[0]):
        if slug not in rounds:
            continue
        strike, winner = rounds[slug]
        T = []
        for _s, ts, tau, spot, ua, da, fb in grp:
            if T and T[-1][0] == ts:
                continue
            T.append((ts, tau, spot, ua, da, fb or 0))
        if len(T) < 5:
            continue
        spots = [(t[0], t[2]) for t in T]
        trig = None
        for i in range(1, len(T)):
            side = crossed_side(spots, strike, i)
            if side is None:
                continue
            ts, tau, spot, ua, da, fb = T[i]
            if tau is None or not (args.tau_lo <= tau <= args.tau_hi):
                continue
            ask = ua if side == "Up" else da
            if not ask or ask <= 0.02 or ask >= 0.98:
                continue
            trig = (i, side, ask, ts, fb)
            break
        if trig is None:
            continue
        n += 1
        i, side, ask, ts, fb = trig
        won = winner == side
        fee = (fb or 0) / 1e4
        for cap in CAPS:
            lim = ask + (cap if cap != float("inf") else 1.0)
            for mode in ("arrive", "rest"):
                # 'arrive' looks at the NEXT quote only (one shot). 'rest' scans
                # forward up to --wait seconds. Neither may fill on the trigger
                # row itself: that ask is what we already knew before the venue's
                # 249ms head start.
                cand = ([i + 1] if mode == "arrive" else
                        [j for j in range(i + 1, len(T))
                         if T[j][0] - ts <= args.wait * 1000])
                fill = None
                for j in cand:
                    a2 = T[j][3] if side == "Up" else T[j][4]
                    if a2 and a2 <= lim + 1e-9:
                        fill = a2
                        break
                if fill is not None:
                    out[mode][cap].append((ts, fill, won, fee))
    return out, n


def logs(args):
    """The exact 250ms test, from the 50ms ask paths the arm records."""
    c = sqlite3.connect(args.db)
    rounds = {
        s: (k, w)
        for s, k, w in c.execute(
            "select slug, strike, winner from rounds "
            "where winner in ('Up','Down') and strike > 0")
    }
    txt = open(args.log, errors="ignore").read()
    pat = re.compile(r"cross fine (\S+) (Up|Down) (?:fire|refuse|trigger): (.*)")
    out = collections.defaultdict(list)
    n = 0
    for m in pat.finditer(txt):
        slug, side, rest = m.group(1), m.group(2), m.group(3)
        if slug not in rounds:
            continue
        pts = []
        for tok in rest.split():
            if "=" not in tok:
                continue
            k, v = tok.split("=", 1)
            try:
                pts.append((int(k), float(v)))
            except ValueError:
                pass
        pts.sort()
        if len(pts) < 3:
            continue
        won = rounds[slug][1] == side

        def at(pc):
            for k, v in pts:
                if k >= pc:
                    return v
            return None

        trig, f250 = at(5), at(25)
        if trig is None or f250 is None:
            continue
        n += 1
        for cap in CAPS:
            if f250 <= trig + (cap if cap != float("inf") else 1.0) + 1e-9:
                out[cap].append((0, f250, won, 0.0))
    return out, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict_vps.db")
    ap.add_argument("--log", default="/tmp/live_vps.log")
    ap.add_argument("--tau-lo", type=float, default=5.0)
    ap.add_argument("--tau-hi", type=float, default=60.0)
    ap.add_argument("--wait", type=float, default=6.0)
    args = ap.parse_args()

    out, n = tape(args)
    print(f"TAPE: first cross per round, tau {args.tau_lo:.0f}-{args.tau_hi:.0f}, "
          f"{n} triggers. Tape cadence ~1s against the venue's 249ms head start, "
          f"so 'arrive' overstates rejection.")
    show(out["arrive"], n, "A1. immediate limit, ONE shot (no resting)")
    show(out["rest"], n, f"A2. limit rests {args.wait:.0f}s (GTC)")

    outp, n2 = logs(args)
    print(f"\n\nLOG: {n2} cross triggers carrying a 50ms ask path.")
    print("  Fill at ask(+250ms) iff ask(+250ms) <= ask(+50ms) + cap.")
    print("  net/sh EXCLUDES the fee (~1c, so subtract ~0.010); read the WIN column.")
    show(outp, n2, "B. EXACT 250ms test from the recorded ask path", fee=False)

    print("\nVERDICT TEST: if the ceiling were selecting against us, the strict caps")
    print("would show a HIGHER win rate than 'inf (population)'. Read the direction.")


if __name__ == "__main__":
    main()
