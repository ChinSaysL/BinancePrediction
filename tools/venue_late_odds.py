#!/usr/bin/env python3
"""Is the venue's LATE-ROUND odds a tradeable edge? (read-only)

Asked after the "deterministic TWAP lock" and "fade the lottery ticket" ideas:
buy the near-certain winner with 5-15s left at 0.88-0.92 and collect the
spread, because a 1s execution delay is <1% of the holding period. The claim
needs three things to be TRUE and all three are measurable on the live DB:

  1. settlement must be a WINDOW AVERAGE (so the winner locks before the bell)
  2. the late favourite must be priced below its realized frequency
  3. that price must be the price we can actually BUY at

Sections below answer them in order. Section 3 is the one that decides: the
`up_price`/`down_price` columns are the DISPLAYED odds, and they bracket a
book whose executable ask sits materially higher.

Usage:
  python3 tools/venue_late_odds.py [--db data/btcpredict_vps.db]
                                   [--tau-lo 5] [--tau-hi 15]
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import statistics as st
import time
import zipfile

BARS = "data/bars_1s/daily"


def slug_day(slug: str) -> str:
    try:
        return time.strftime("%m-%d", time.gmtime(int(slug.rsplit("-", 1)[1])))
    except (ValueError, IndexError):
        return "?"


def late_quotes(conn, tau_lo: float, tau_hi: float):
    """ONE row per round: the last quote inside the [tau_lo, tau_hi] window."""
    return conn.execute("""
      SELECT q.slug, q.up_price p, q.up_bid ub, q.up_ask ua,
             q.down_bid dbk, q.down_ask dak, q.spot, q.strike, q.tau_s,
             q.up_chance ch, r.winner w
        FROM quotes q JOIN rounds r ON r.slug = q.slug
       WHERE r.winner IN ('Up','Down') AND q.tau_s BETWEEN ? AND ?
         AND q.up_price > 0 AND q.up_price < 1
       GROUP BY q.slug HAVING q.ts_ms = MAX(q.ts_ms)
    """, (tau_lo, tau_hi)).fetchall()


def day_t(rows, value, key):
    """Day-clustered t of `value` -- the honest statistic (see FINDINGS)."""
    d: dict[str, list[float]] = {}
    for r in rows:
        d.setdefault(slug_day(r["slug"]), []).append(value(r))
    if len(d) < 2:
        return 0.0, ""
    dm = [st.mean(v) for v in d.values()]
    sd = st.stdev(dm)
    t = st.mean(dm) / (sd / len(dm) ** 0.5) if sd else 0.0
    return t, "".join("+" if x > 0 else "-" for x in
                      [st.mean(d[k]) for k in sorted(d)])


def pnl(p: float, won: bool) -> float:
    return (1 - p) / p if won else -1.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict_vps.db")
    ap.add_argument("--tau-lo", type=float, default=5.0)
    ap.add_argument("--tau-hi", type=float, default=15.0)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = late_quotes(conn, args.tau_lo, args.tau_hi)
    print(f"rounds with a late quote (tau {args.tau_lo:.0f}-{args.tau_hi:.0f}s): "
          f"{len(rows)}")

    # ---- 1. settlement: snapshot or window average? ------------------------
    print("\n=== 1. SETTLEMENT RULE: snapshot, or a window we could front-run? ===")
    bars: dict[str, dict[int, float]] = {}
    for path in glob.glob(f"{BARS}/*-1s-2026-*.zip"):
        sym = os.path.basename(path).split("-1s-")[0]
        with zipfile.ZipFile(path) as z:
            for line in z.open(z.namelist()[0]):
                f = line.decode().split(",")
                if len(f) < 5:
                    continue
                try:                      # 1s klines carry MICROSECOND open_time
                    bars.setdefault(sym, {})[int(f[0]) // 10 ** 6] = float(f[4])
                except ValueError:
                    continue
    res = []
    for r in conn.execute("select symbol, end_ms, end_price from rounds "
                          "where end_price > 0 and end_ms > 0"):
        d = bars.get(r["symbol"])
        if not d:
            continue
        t = r["end_ms"] // 1000
        pt = d.get(t) or d.get(t - 1)
        if not pt:
            continue
        w30 = [x for x in (d.get(t - k) for k in range(30)) if x]
        w60 = [x for x in (d.get(t - k) for k in range(60)) if x]
        res.append((abs(r["end_price"] - pt) / pt * 1e4,
                    abs(r["end_price"] - st.mean(w30)) / pt * 1e4,
                    abs(r["end_price"] - st.mean(w60)) / pt * 1e4
                    if len(w60) > 10 else None,
                    (pt - r["end_price"]) / pt * 1e4))
    if res:
        t60 = [x[2] for x in res if x[2] is not None]
        print(f"   n={len(res)} rounds joined to 1s bars")
        print(f"   |end_price - close at end|  median {st.median([x[0] for x in res]):5.2f} bps")
        print(f"   |end_price - 30s mean|      median {st.median([x[1] for x in res]):5.2f} bps")
        print(f"   |end_price - 60s mean|      median {st.median(t60):5.2f} bps")
        print(f"   signed (close - end_price): median {st.median([x[3] for x in res]):+5.2f} bps")
        print("   -> smallest residual wins; ~0 signed means no lag to exploit")
    else:
        print("   (no 1s bars on disk; skipping)")

    # ---- 2. calibration of the DISPLAYED price ----------------------------
    print("\n=== 2. REALIZED FREQUENCY vs THE DISPLAYED up_price ===")
    print(f"{'band':>12} {'n':>6} {'priced':>8} {'realized':>9} {'gap':>8} {'day-t':>7}")
    for lo, hi in ((0, .1), (.1, .3), (.3, .5), (.5, .7), (.7, .85),
                   (.85, .9), (.9, .95), (.95, .99), (.99, 1.01)):
        g = [r for r in rows if lo < r["p"] <= hi]
        if len(g) < 8:
            continue
        p = st.mean([r["p"] for r in g])
        w = st.mean([1.0 if r["w"] == "Up" else 0.0 for r in g])
        ev = st.mean([pnl(r["p"], r["w"] == "Up") for r in g])
        t, _ = day_t(g, lambda r: pnl(r["p"], r["w"] == "Up"), "p")
        print(f"{lo:5.2f}-{hi:5.2f} {len(g):>6} {p:8.3f} {w:9.3f} {w-p:+8.3f} {t:+7.2f}")

    # ---- 3. the edge that matters: |spot - strike|, an independent variable
    print("\n=== 3. BY |spot-strike| AT THE SAME INSTANT (independent of the odds) ===")
    rows = [r for r in rows if r["spot"] and r["strike"]]
    print(f"{'spot-strike':>14} {'n':>5} {'realized':>9} {'priced':>8} {'gap':>8} "
          f"{'day-t':>7} {'EV_fav':>8}")
    for lo, hi in ((-99, -5), (-5, -2), (-2, -0.5), (-0.5, 0.5),
                   (0.5, 2), (2, 5), (5, 99)):
        g = [r for r in rows
             if lo <= (r["spot"] - r["strike"]) / r["strike"] * 1e4 < hi]
        if len(g) < 10:
            continue
        # the FAVOURITE side = the side the spot already favours
        fav_won = [((r["spot"] > r["strike"]) == (r["w"] == "Up")) for r in g]
        fav_px = [r["p"] if r["spot"] > r["strike"] else 1 - r["p"] for r in g]
        ev = [pnl(p, won) for p, won in zip(fav_px, fav_won)]
        t, _ = day_t(g, lambda r: pnl(r["p"] if r["spot"] > r["strike"]
                                      else 1 - r["p"],
                                      (r["spot"] > r["strike"]) == (r["w"] == "Up")),
                     "p")
        print(f"{lo:+5.1f}..{hi:+5.1f} {len(g):>5} {st.mean(fav_won):9.3f} "
              f"{st.mean(fav_px):8.3f} {st.mean(fav_won)-st.mean(fav_px):+8.3f} "
              f"{t:+7.2f} {st.mean(ev):+8.3f}")

    # ---- 4. THE DECIDER: is that price buyable? ---------------------------
    print("\n=== 4. EXECUTABILITY: displayed price vs the recorded book ===")
    big = [r for r in rows if abs((r["spot"] - r["strike"]) / r["strike"] * 1e4) >= 2]

    def view(r):
        up = r["spot"] > r["strike"]
        return ((r["p"], r["ub"], r["ua"]) if up
                else (1 - r["p"], r["dbk"], r["dak"]))

    have = [r for r in big if view(r)[1] and view(r)[2]]
    print(f"   |dist| >= 2bps: n={len(big)}, with bid+ask recorded: {len(have)}")
    if not have:
        print("   (no book records in this window -- cannot decide executability)")
        return 0
    print(f"   FAVOURITE side: mean displayed {st.mean([view(r)[0] for r in have]):.4f}  "
          f"mean bid {st.mean([view(r)[1] for r in have]):.4f}  "
          f"mean ask {st.mean([view(r)[2] for r in have]):.4f}")
    sane = [r for r in have if view(r)[1] <= view(r)[0] <= view(r)[2]]
    print(f"   book coherent (bid <= price <= ask): {len(sane)}/{len(have)} "
          f"= {100*len(sane)/len(have):.1f}%")
    if not sane:
        return 0
    won = [((r["spot"] > r["strike"]) == (r["w"] == "Up")) for r in sane]
    px = [view(r)[0] for r in sane]
    ask = [view(r)[2] for r in sane]
    print(f"   coherent subset n={len(sane)}: priced {st.mean(px):.4f}  "
          f"ASK {st.mean(ask):.4f}  realized {st.mean(won):.4f}")
    for label, cost in (("at the DISPLAYED price", px), ("at the ASK", ask)):
        ev = st.mean([pnl(p, w) for p, w in zip(cost, won)])
        print(f"     EV/$1 {label:22}: {ev:+.4f}   after ~96bps fee: {ev-0.0096:+.4f}")
    print("   -> if the ASK column is the real cost, the displayed-price edge is a mirage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
