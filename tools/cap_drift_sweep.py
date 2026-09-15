"""Does the ceiling need to cover the drift, or is a wider cap just a wider cap?

The live evidence that prompted this: after the +2c cap shipped, all three order
attempts came back `bound == ceiling` -- `room == 0`. The ceiling is derived from
the TRIGGER ask, but the venue's quote (which anchors the FOK bound) arrives
~250ms later, and by then the ask has moved. Measured on the recorded 50ms ask
paths, the ask moves a median +0.010 and a p90 +0.070 inside 250ms. So a 2c
ceiling set from the pre-move ask leaves, on a typical cross, about 1c of usable
room -- and on the tail, none.

The proposal is therefore to set the ceiling at `trigger_ask + cap + drift`.

IMPORTANT: on the tape this is INDISTINGUISHABLE from simply widening the cap.
"Fill iff ask <= A + cap + drift" IS "fill iff ask <= A + (cap+drift)". So the
tape cannot test the drift story as a story -- it can only price the effective
cap. What the tape CANNOT do either is model the arrival at 250ms, because its
cadence is ~1s. That is what the log's 50ms paths are for.

So this tool runs both, at matching effective caps:

  TAPE  arrival = the next quote (~1s later). Overstates the drift, so it
        understates the fill rate at any given cap -- conservative.
  LOG   arrival = the actual 250ms / 500ms / 1000ms sample from `cross fine`.
        Exact, but n is small; the horizon sweep is the sensitivity check.

Read it as: for each effective cap, what fill rate do we get and what is the net
EV per share AT THE PRICE WE WOULD ACTUALLY PAY.

    python3 tools/cap_drift_sweep.py --db data/btcpredict_vps.db --log /tmp/live_vps.log
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


def crossed_side(spots, strike, i, look_s=1.0, slack_s=2.0):
    """The side that just crossed INTO the money at index i, or None.

    Inlined so this tool runs on the VPS without tool-to-tool imports. Mirrors
    btcpredict.main.crossed_side exactly: `spots` is [(ts_ms, spot), ...]
    chronological.
    """
    ts_now, px_now = spots[i]
    if not px_now or px_now <= 0 or not strike or strike <= 0:
        return None
    side_now = "Up" if px_now > strike else "Down"
    target = ts_now - int(look_s * 1000)
    oldest = ts_now - int((look_s + slack_s) * 1000)
    for j in range(i - 1, -1, -1):
        ts, px = spots[j]
        if ts > target:
            continue
        if not px or px <= 0 or ts < oldest:
            return None
        return side_now if ("Up" if px > strike else "Down") != side_now else None
    return None

CAPS = [float("inf"), 0.08, 0.06, 0.05, 0.04, 0.03, 0.02, 0.015, 0.01, 0.005, 0.0]


def day_t(rows):
    by = collections.defaultdict(list)
    for ts, ev in rows:
        d = dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).date()
        by[d].append(ev)
    means = [statistics.fmean(v) for v in by.values() if v]
    if len(means) < 2:
        return float("nan")
    sd = statistics.stdev(means)
    return float("nan") if sd == 0 else statistics.fmean(means) / (sd / math.sqrt(len(means)))


def show(title, by_cap, n_total, fee_frac, note=""):
    print(f"\n=== {title} ===")
    if note:
        print(f"    {note}")
    hdr = (f"{'cap':>8s} {'fills':>6s} {'rate':>7s} {'paid':>7s} {'win':>7s} "
           f"{'net/sh':>8s} {'net/$1':>8s} {'day-t':>7s}")
    print(hdr)
    print("-" * len(hdr))
    for cap in CAPS:
        rs = by_cap.get(cap) or []
        label = "no cap" if cap == float("inf") else f"+{cap*100:.1f}c"
        if not rs:
            print(f"{label:>8s} {0:6d}")
            continue
        paid = statistics.fmean(r[1] for r in rs)
        win = statistics.fmean(1.0 if r[2] else 0.0 for r in rs)
        net = statistics.fmean((1.0 if r[2] else 0.0) - r[1] - fee_frac * r[1] for r in rs)
        per1 = statistics.fmean(
            ((1.0 if r[2] else 0.0) - r[1] - fee_frac * r[1]) / r[1] for r in rs)
        t = day_t([(r[0], (1.0 if r[2] else 0.0) - r[1]) for r in rs])
        print(f"{label:>8s} {len(rs):6d} {len(rs)/n_total:7.1%} {paid:7.3f} {win:7.1%} "
              f"{net:+8.3f} {per1:+8.3f} {t:+7.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict_vps.db")
    ap.add_argument("--log", default="/tmp/live_vps.log")
    ap.add_argument("--tau-lo", type=float, default=5.0)
    ap.add_argument("--tau-hi", type=float, default=60.0)
    ap.add_argument("--fee-frac", type=float, default=None,
                    help="fee as a fraction of price; default = measured from fills")
    args = ap.parse_args()

    c = sqlite3.connect(args.db)

    fee_frac = args.fee_frac
    if fee_frac is None:
        fr = [f / (s * p) for f, s, p in
              c.execute("select fee_paid, shares, filled_px from fills "
                        "where shares > 0 and filled_px > 0 and fee_paid > 0")]
        fee_frac = statistics.median(fr) if fr else 0.010
    print(f"fee: {fee_frac:.4f} of price per share (measured median from live fills)")

    rounds = {s: (k, w) for s, k, w in c.execute(
        "select slug, strike, winner from rounds "
        "where winner in ('Up','Down') and strike > 0")}

    # ---------------- TAPE: arrival = next quote (~1s) ----------------
    sql = ("select slug, ts_ms, tau_s, spot, up_ask, down_ask from quotes "
           "where up_ask is not null and down_ask is not null and spot > 0 "
           "order by slug, ts_ms")
    tape = collections.defaultdict(list)
    n_tape = 0
    for slug, grp in itertools.groupby(c.execute(sql), key=lambda r: r[0]):
        if slug not in rounds:
            continue
        strike, winner = rounds[slug]
        T = []
        for _s, ts, tau, spot, ua, da in grp:
            if T and T[-1][0] == ts:
                continue
            T.append((ts, tau, spot, ua, da))
        if len(T) < 5:
            continue
        spots = [(t[0], t[2]) for t in T]
        for i in range(1, len(T)):
            side = crossed_side(spots, strike, i)
            if side is None:
                continue
            ts, tau, _sp, ua, da = T[i]
            if tau is None or not (args.tau_lo <= tau <= args.tau_hi):
                continue
            ask = ua if side == "Up" else da
            if not ask or ask <= 0.02 or ask >= 0.98:
                continue
            if i + 1 >= len(T):
                break
            arrived = T[i + 1][3] if side == "Up" else T[i + 1][4]
            if not arrived:
                break
            n_tape += 1
            won = winner == side
            for cap in CAPS:
                lim = ask + (cap if cap != float("inf") else 1.0)
                if arrived <= lim + 1e-9:
                    tape[cap].append((ts, arrived, won))
            break                                  # first trigger per round
    show("TAPE  arrival = next quote (~1s)", tape, n_tape, fee_frac,
         "overstates the drift vs the real 250ms, so the fill rates here are a FLOOR")

    # ---------------- LOG: arrival at the real 250/500/1000ms ----------------
    txt = open(args.log, errors="ignore").read()
    pat = re.compile(
        r"^(\S+ \S+),(\d+) .*?cross fine (\S+) (Up|Down) "
        r"(?:fire|refuse|trigger): (.*)", re.M)

    def line_tau(slug: str, stamp: str, msec: str) -> float | None:
        """`cross fine` does NOT carry tau, so the --tau-lo/--tau-hi filter was
        silently filtering only the TAPE path: a three-band run printed three
        IDENTICAL LOG blocks and each one looked like a tau result. tau is
        derivable instead -- the slug IS the round's start in unix seconds and
        the family names its length."""
        parts = slug.split("-")
        if len(parts) < 4:
            return None
        dur = {"5m": 300, "15m": 900, "1h": 3600, "1d": 86400}.get(parts[2])
        if dur is None:
            return None
        try:
            start = int(parts[3])
        except ValueError:
            return None
        t = dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=dt.timezone.utc).timestamp() + int(msec) / 1000.0
        return start + dur - t

    parsed = []
    skipped_tau = 0
    for m in pat.finditer(txt):
        stamp, msec, slug, side, rest = m.groups()
        if slug not in rounds:
            continue
        tau = line_tau(slug, stamp, msec)
        if tau is None or not (args.tau_lo <= tau <= args.tau_hi):
            skipped_tau += 1
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
        if len(pts) < 4:
            continue

        def at(pc):
            for k, v in pts:
                if k >= pc:
                    return v
            return None

        trig = at(5)
        if trig is None:
            continue
        parsed.append((slug, side, pts, trig, rounds[slug][1] == side))

    if skipped_tau:
        print(f"  [tau filter dropped {skipped_tau} of the log's cross fine "
              f"paths, tau {args.tau_lo:.0f}-{args.tau_hi:.0f}s]")

    for horizon_pc, hname in ((25, "250ms"), (50, "500ms"), (100, "1000ms")):
        by_cap = collections.defaultdict(list)
        n = 0
        for slug, side, pts, trig, won in parsed:
            arr = None
            for k, v in pts:
                if k >= horizon_pc:
                    arr = v
                    break
            if arr is None:
                continue
            n += 1
            for cap in CAPS:
                lim = trig + (cap if cap != float("inf") else 1.0)
                if arr <= lim + 1e-9:
                    by_cap[cap].append((0, arr, won))
        show(f"LOG  arrival = ask(+{hname}) from the recorded 50ms paths", by_cap, n,
             fee_frac, f"{n} triggers carrying a usable path")

    print("\nREAD: net/sh is at the price we would ACTUALLY pay, not the trigger ask.")
    print("If the drift story is right, the 250ms table should keep the tight cap's")
    print("net edge while recovering fill rate as the cap widens to cover ~1-7c.")


if __name__ == "__main__":
    main()
