"""TRAIN PARAMETER SWEEP -- one DB read, then OFAT + a coarse joint search.

Reuses the EXACT methodology of tools/momentum_continuation.py, because that is
the tool the shipped spec came from, and a sweep scored differently from the
spec would be measuring the scorer rather than the strategy:

  * COHERENT asks only (bid <= displayed <= ask). FINDINGS records that only
    47.2% of rows are coherent and that scoring the incoherent ones manufactured
    the +119%/$1 table and VEL_INV's day-t +6.79. Non-negotiable.
  * ONE entry per round, taken at the FIRST quote where |spot-K| >= X (the event
    anchor the live trigger must reproduce -- the 20:49 fill bug was anchoring at
    nothing).
  * PER-SHARE net = hit - px - fee*px, fee = median(fee_paid/(shares*filled_px)).
  * Reported at ask + --pay-up, because 69% of live fills land exactly ON the
    limit. The ask is NOT what this arm pays.
  * DAY-CLUSTERED t over days, not pooled.

READ THE `n` COLUMN BEFORE THE `day-t` COLUMN. With ~60 cells in a sweep,
|t| > 2 appears by chance a few times; a cell is only interesting if n is
adequate AND the sign is consistent across days AND the response is monotone in
the knob. The BEST table prints days+ for exactly this reason.

    PYTHONPATH=/root/BinancePrediction .venv/bin/python tools/train_sweep.py \
        --db data/btcpredict.db
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import itertools
import math
import sqlite3
import statistics

DUR = {"5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


def day_t(rows):
    by = collections.defaultdict(list)
    for ts, ev in rows:
        by[dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).date()].append(ev)
    means = [statistics.fmean(v) for v in by.values() if v]
    if len(means) < 2:
        return float("nan"), len(means), 0
    sd = statistics.stdev(means)
    t = (float("nan") if sd == 0
         else statistics.fmean(means) / (sd / math.sqrt(len(means))))
    return t, len(means), sum(1 for m in means if m > 0)


def report(name, rows, fee, pay_up, min_n=25):
    """rows: (ts, ask, hit, slug). Priced at ask + pay_up."""
    n = len(rows)
    if n < min_n:
        return None
    px = statistics.fmean(r[1] + pay_up for r in rows)
    hit = statistics.fmean(1.0 if r[2] else 0.0 for r in rows)
    net = statistics.fmean((1.0 if r[2] else 0.0) - (r[1] + pay_up)
                           - fee * (r[1] + pay_up) for r in rows)
    t, days, pos = day_t([(r[0], (1.0 if r[2] else 0.0) - (r[1] + pay_up))
                          for r in rows])
    line = (f"  {name:38s} n={n:5d} px {px:6.3f} hit {hit:6.1%} "
            f"net {net:+7.3f} day-t {t:+5.2f} {days}d {pos}/{days}+")
    return (net, t, days, pos, n, line)


def simulate(series, ranges, cfg, pay_up, fee, side_mode="cont"):
    """One config over every round. Returns rows = (ts, ask, hit, slug)."""
    out = []
    X = cfg["X"]
    D = cfg["D"]
    for slug, T in series.items():
        K, winner = ranges[slug]
        parts = slug.split("-")
        fam = parts[2] if len(parts) > 2 else ""
        if cfg["family"] != "all" and fam != cfg["family"]:
            continue
        # ---- the breakout: FIRST quote at/after the tau floor past X --------
        floor = max(3.0, cfg["min_tau"])
        bi = None
        for i, row in enumerate(T):
            tau, spot = row[1], row[2]
            if tau is None or tau < floor:
                continue
            if cfg["max_tau"] and tau > cfg["max_tau"]:
                continue
            if abs((spot - K) / K * 1e4) >= X:
                bi = i
                break
        if bi is None:
            continue
        t0 = T[bi][0]
        # ---- momentum consistency: |d| must be RISING vs `rise_s` earlier ----
        if cfg["rise_s"] > 0:
            prev = None
            for k in range(bi, -1, -1):
                if T[k][0] <= t0 - cfg["rise_s"] * 1000:
                    prev = T[k]
                    break
            if prev is None or prev[1] is None:
                continue
            if abs((T[bi][2] - K) / K * 1e4) <= abs((prev[2] - K) / K * 1e4):
                continue
        # ---- settle at the entry quote ---------------------------------------
        j = next((i for i in range(bi, len(T))
                  if T[i][0] >= t0 + D * 1000), None)
        if j is None:
            continue
        row = T[j]
        ts, spot = row[0], row[2]
        lead = "Up" if spot >= K else "Down"
        side = lead if side_mode == "cont" else ("Down" if lead == "Up" else "Up")
        ask = pick(side, row, cfg)
        if ask is None:
            continue
        # ---- round range to the entry (the cross arm's 10bps gate) ----------
        if cfg["max_range_bps"] > 0:
            rr = round_range_bps(slug, T, K, ts)
            if rr is None or rr > cfg["max_range_bps"]:
                continue
        out.append((ts, ask, winner == side, slug))
    return out


def pick(side, row, cfg):
    _ts, _tau, _sp, ua, da, ub, db, upx, dpx = row
    ask = ua if side == "Up" else da
    if not ask or ask <= 0.02 or ask >= 0.98:
        return None
    if ask > cfg["max_ask"] or ask < cfg["min_ask"]:
        return None
    bid = ub if side == "Up" else db
    px = upx if side == "Up" else dpx
    if not bid or not px or bid <= 0 or px <= 0:
        return None
    if not (bid - 1e-9 <= px <= ask + 1e-9):
        return None                      # INCOHERENT -- see the docstring
    return ask


def round_range_bps(slug, T, K, upto_ms):
    parts = slug.split("-")
    dur = DUR.get(parts[2] if len(parts) > 2 else "")
    if dur is None or K <= 0:
        return None
    try:
        start = int(parts[3]) * 1000
    except (ValueError, IndexError):
        return None
    seen = [r[2] for r in T if start <= r[0] <= upto_ms and r[2] > 0]
    if len(seen) < 2:
        return None
    return 1e4 * (max(seen) - min(seen)) / K


BASE = dict(X=10.0, D=0, min_tau=60.0, max_tau=0.0, max_ask=0.89,
            min_ask=0.0, family="all", rise_s=0.0, max_range_bps=0.0)

# What is LIVE as of 2026-09-15. The ceiling is OFF: shipping max_tau=180 was a
# MISREAD of the joint table's `t>=180` column, which is MIN_TAU, not a ceiling.
SHIPPED = dict(BASE, X=15.0, max_tau=0.0)

SWEEP = {
    "X (distance bps)":       [("X", v) for v in (5, 7.5, 10, 12.5, 15, 20, 25)],
    "D (delay s)":            [("D", v) for v in (0, 1, 2, 5)],
    "min_tau (s)":            [("min_tau", v) for v in (60, 90, 120, 180, 240)],
    "max_tau (s, 0=off)":     [("max_tau", v) for v in (0, 120, 180, 240, 300)],
    "max_ask (price cap)":    [("max_ask", v) for v in (0.80, 0.85, 0.89, 0.92, 0.95)],
    "min_ask (price floor)":  [("min_ask", v) for v in (0.0, 0.70, 0.75, 0.80)],
    "family":                 [("family", v) for v in ("all", "5m", "15m")],
    "rise_s (momentum)":      [("rise_s", v) for v in (0, 3, 5, 10)],
    "max_range_bps":          [("max_range_bps", v) for v in (0, 10, 15, 20, 30)],
}


def portfolio_sweep(series, ranges, cfg, pay_up, fee):
    """THE EXPOSURE KNOBS -- which the signal sweep above does not touch.

    A signal sweep answers "is this entry good". It CANNOT answer "how many of
    them should be open at once", and for this trigger that is not a secondary
    question: it fires in BURSTS (btc/eth/bnb all cross the threshold inside the
    same ten seconds), so a concurrency cap removes the most CORRELATED legs
    first. Dropping a leg is therefore not a random subsample of the entrants --
    it is a selection, and it can help or hurt independently of each leg's EV.

    Simulated on the real clock: a position is open from its entry ts until its
    round settles (slug start + family length) and consumes one slot. The peak
    column is the true high-water mark of concurrent positions.
    """
    ents = []
    for ts, ask, hit, slug in simulate(series, ranges, cfg, pay_up, fee):
        p = slug.split("-")
        dur = DUR.get(p[2] if len(p) > 2 else "", 0)
        try:
            start = int(p[3]) * 1000
        except (ValueError, IndexError):
            continue
        ents.append((ts, ask, hit, slug, p[0], start + dur * 1000))
    ents.sort(key=lambda e: e[0])
    print(f"\n  entrants under the SHIPPED signal config: {len(ents)}")

    def sim(max_open, max_day, max_sym, gap_s):
        op, taken, per_day, per_sym = [], [], collections.Counter(), {}
        peak, last = 0, None
        for ts, ask, hit, slug, sym, settle in ents:
            op = [s for s in op if s > ts]           # settle first
            if max_open and len(op) >= max_open:
                continue
            d = dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).date()
            if max_day and per_day[d] >= max_day:
                continue
            if max_sym and per_sym.get(sym, 0) >= max_sym:
                continue
            if gap_s and last is not None and ts - last < gap_s * 1000:
                continue
            taken.append((ts, ask, hit, slug))
            op.append(settle)
            peak = max(peak, len(op))
            per_day[d] += 1
            per_sym[sym] = per_sym.get(sym, 0) + 1
            last = ts
        return taken, peak

    def line(name, taken, peak):
        if not taken:
            print(f"  {name:34s} -- nothing taken")
            return
        money = sum((1.0 if r[2] else 0.0) - (r[1] + pay_up)
                    - fee * (r[1] + pay_up) for r in taken)
        got = report(name, taken, fee, pay_up, 1)
        print(f"{got[5]}  peak {peak:2d} open  ${money:+6.2f} total")

    for mo in (0, 1, 2, 3, 5, 10):
        t, pk = sim(mo, 0, 0, 0)
        line(f"max_open={mo or 'off'}", t, pk)
    for md in (0, 5, 10, 20, 50):
        t, pk = sim(0, md, 0, 0)
        line(f"max_per_day={md or 'off'}", t, pk)
    for ms in (0, 1, 2):
        t, pk = sim(0, 0, ms, 0)
        line(f"max_per_symbol={ms or 'off'}", t, pk)
    for g in (0, 30, 60, 120, 300):
        t, pk = sim(0, 0, 0, g)
        line(f"global_gap={g}s", t, pk)
    for mo, ms in ((2, 1), (3, 1), (1, 1)):
        t, pk = sim(mo, 0, ms, 0)
        line(f"max_open={mo}+per_symbol={ms}", t, pk)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--pay-up", type=float, default=0.015)
    ap.add_argument("--min-n", type=int, default=25)
    args = ap.parse_args()

    c = sqlite3.connect(args.db)
    fee = 0.0117
    fr = [f / (s * p) for f, s, p in
          c.execute("select fee_paid, shares, filled_px from fills "
                    "where shares > 0 and filled_px > 0 and fee_paid > 0")]
    if fr:
        fee = statistics.median(fr)
    ranges = {s: (k, w) for s, k, w in c.execute(
        "select slug, strike, winner from rounds "
        "where winner in ('Up','Down') and strike > 0")}
    print(f"rounds with a winner: {len(ranges):,}   fee {fee:.4f} of price   "
          f"paying ask+{args.pay_up:.3f}")

    sql = ("select slug, ts_ms, tau_s, spot, up_ask, down_ask, "
           "up_bid, down_bid, up_price, down_price from quotes "
           "where up_ask is not null and down_ask is not null and spot > 0 "
           "order by slug, ts_ms")
    series = {}
    for slug, grp in itertools.groupby(c.execute(sql), key=lambda r: r[0]):
        if slug not in ranges:
            continue
        T = []
        for row in grp:
            if T and T[-1][0] == row[1]:
                continue
            T.append((row[1], row[2], row[3], row[4], row[5], row[6], row[7],
                      row[8], row[9]))
        if len(T) >= 5:
            series[slug] = T
    print(f"rounds with a usable coherent series: {len(series):,}\n")

    def go(cfg, side_mode="cont"):
        rows = simulate(series, ranges, cfg, args.pay_up, fee, side_mode)
        return rows, report("", rows, fee, args.pay_up, 1)

    base_rows, _ = go(BASE)
    print("=" * 78)
    print("BASELINE (the shipped config, priced at the limit)")
    r = report("X=10 D=0 tau>=60 ask<=0.89 fam=all", base_rows, fee, args.pay_up, 1)
    print(r[5] if r else "  too few")
    ctrl = simulate(series, ranges, BASE, args.pay_up, fee, "opp")
    cr = report("  OPPOSITE side (must be NEGATIVE)", ctrl, fee, args.pay_up, 1)
    print(cr[5] if cr else "  too few")

    print("\n" + "=" * 78)
    print("ONE FACTOR AT A TIME around the baseline")
    for title, knobs in SWEEP.items():
        print(f"\n-- {title} --")
        for key, val in knobs:
            cfg = dict(BASE)
            cfg[key] = val
            rows = simulate(series, ranges, cfg, args.pay_up, fee)
            got = report(f"{key}={val}", rows, fee, args.pay_up, args.min_n)
            print(got[5] if got else f"  {key}={val:<12} n={len(rows):5d}  -- too few")

    print("\n" + "=" * 78)
    print("JOINT SEARCH over the five that matter (min n=%d, priced at the limit)"
          % args.min_n)
    print("  CAREFUL: this is %d-ish cells. |t|>2 is EXPECTED by chance."
          % (7 * 4 * 5 * 3 * 5))
    cells = []
    for X, D, mt, fam, ma in itertools.product(
            (7.5, 10.0, 12.5, 15.0), (0, 1, 2), (60.0, 120.0, 180.0),
            ("all", "5m"), (0.85, 0.89, 0.92)):
        cfg = dict(BASE)
        cfg.update(X=X, D=D, min_tau=mt, family=fam, max_ask=ma)
        rows = simulate(series, ranges, cfg, args.pay_up, fee)
        got = report(f"X={X} D={D} t>={mt:.0f} {fam} ask<={ma}",
                     rows, fee, args.pay_up, args.min_n)
        if got:
            cells.append(got)
    cells.sort(key=lambda g: (g[1] if g[1] == g[1] else -99), reverse=True)
    print(f"  {len(cells)} cells with n>={args.min_n}; top 14 by day-t:")
    for got in cells[:14]:
        print(got[5])
    print("  ... and the bottom 5, which must look bad for the top to mean "
          "anything:")
    for got in cells[-5:]:
        print(got[5])

    print("\n" + "=" * 78)
    print("PORTFOLIO / CONCURRENCY -- the knobs the signal sweep does NOT touch")
    print("  A cap drops the most CORRELATED legs first, so it selects; it is not")
    print("  a random subsample. `$` is the total on these entries at $1 a leg.")
    portfolio_sweep(series, ranges, SHIPPED, args.pay_up, fee)


if __name__ == "__main__":
    main()
