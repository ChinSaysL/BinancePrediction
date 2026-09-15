#!/usr/bin/env python3
"""Cross-asset lead-lag test: does a BTC break make the ETH/BNB contract tradeable?

THE CLAIM (third-party): cross-market risk engines run on independent worker
threads, so when BTC breaks, the ETH and BNB prediction contracts lag updating
their odds relative to their OWN spot feeds. Exploit: watch the BTC spot, and
fire on the ETH/BNB contract while it is still stale.

The claim is specifically about the CROSS signal carrying information the
contract's own spot has not yet imparted. So the test is not "does momentum
predict" -- that is tested elsewhere and is negative. The test is:

    does BTC's move predict the ETH CONTRACT's future price move
    BEYOND what ETH's own move already says?

and the decisive form of that question is a P&L comparison, not a regression
coefficient, because a lag of 200-500 ms is worthless if our own venue ack and
fill take longer than that. So every cell is priced twice: at the signal second
and at +1s / +2s (our realistic entry).

Design, deliberately matched so the comparison is clean:
  signal  in {btc, eth, bnb} spot move over --lookback seconds
  traded  the {btc, eth, bnb} 5-minute contract, at that side's ASK
  side    the sign of the signal's move
  one row per ROUND (never per quote -- row inflation fakes the day-t)
  edge    (1 if that side won else 0) - ask - fee      PER SHARE
  day-clustered t across the available days

The interesting cells are the OFF-DIAGONAL ones (signal btc -> trade eth/bnb).
The DIAGONAL cells are the control: if the own-asset version does not work
either, there is nothing for a cross-asset version to add.

Read-only.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import sqlite3
import statistics as st

MKT = ("btc", "eth", "bnb")


def load_spot(c, sym, lo_ms, hi_ms):
    """Per-symbol spot series from the quotes table: sorted ts, spots."""
    rows = c.execute(
        "SELECT ts_ms, spot FROM quotes WHERE slug LIKE ? AND spot IS NOT NULL "
        "AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms", (f"{sym}-updown-5m-%", lo_ms, hi_ms))
    ts, sp = [], []
    for t, s in rows:
        ts.append(t)
        sp.append(s)
    return ts, sp


def move_bps(ts, sp, t_ms, lookback_s):
    """Spot return over the last `lookback_s` seconds, in bps, or None."""
    if not ts:
        return None
    i = bisect.bisect_right(ts, t_ms) - 1
    if i < 0:
        return None
    j = bisect.bisect_left(ts, t_ms - lookback_s * 1000)
    if j >= len(ts) or j > i:
        return None
    if ts[i] - ts[j] < lookback_s * 1000 * 0.5:   # insist on real coverage
        return None
    p0, p1 = sp[j], sp[i]
    if not p0:
        return None
    return (p1 - p0) / p0 * 1e4


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict_vps.db")
    ap.add_argument("--lookback", type=float, default=5.0)
    ap.add_argument("--min-mom-bps", type=float, default=3.0)
    ap.add_argument("--tau-lo", type=float, default=60.0)
    ap.add_argument("--tau-hi", type=float, default=240.0)
    ap.add_argument("--lag-ms", type=int, nargs="+", default=[0, 1000, 2000])
    ap.add_argument("--fee-bps", type=float, default=96.0)
    args = ap.parse_args()

    c = sqlite3.connect(args.db)
    c.row_factory = sqlite3.Row
    r = c.execute("SELECT MIN(ts_ms) a, MAX(ts_ms) b FROM quotes").fetchone()
    lo, hi = r["a"], r["b"]

    spot = {s: load_spot(c, s, lo, hi) for s in MKT}
    for s in MKT:
        print(f"  {s} spot series: {len(spot[s][0]):,} marks")

    win = {}
    for x in c.execute("SELECT slug, winner, start_ms FROM rounds WHERE winner IS NOT NULL "
                       "AND slug LIKE '%-updown-5m-%'"):
        win[x["slug"]] = (x["winner"], x["start_ms"])

    # contract quotes, grouped by slug
    q = collections.defaultdict(list)
    for x in c.execute(
            "SELECT slug, ts_ms, tau_s, up_ask, down_ask FROM quotes "
            "WHERE slug LIKE '%-updown-5m-%' AND ts_ms BETWEEN ? AND ?", (lo, hi)):
        q[x["slug"]].append((x["ts_ms"], x["tau_s"], x["up_ask"], x["down_ask"]))

    fee = args.fee_bps / 1e4
    out = collections.defaultdict(list)   # (sig, trade, lag) -> rows

    for slug, rows in q.items():
        if slug not in win:
            continue
        trade = slug.split("-")[0]
        if trade not in MKT:
            continue
        rows.sort()
        for sig in MKT:
            sts, ssp = spot[sig]
            for lag in args.lag_ms:
                pick = None
                for t_ms, tau, ua, da in rows:
                    if tau is None or not (args.tau_lo <= tau <= args.tau_hi):
                        continue
                    m = move_bps(sts, ssp, t_ms - lag, args.lookback)
                    if m is None or abs(m) < args.min_mom_bps:
                        continue
                    side_up = m > 0
                    px = ua if side_up else da
                    if not px or not (0.05 <= px <= 0.95):
                        continue
                    pick = (side_up, px)
                    break          # ONE entry per round
                if not pick:
                    continue
                side_up, px = pick
                w, start = win[slug]
                won = (w == "Up") if side_up else (w == "Down")
                day = dt.datetime.fromtimestamp(start / 1000, dt.UTC).strftime("%m-%d")
                out[(sig, trade, lag)].append(
                    dict(day=day, px=px, won=1.0 if won else 0.0,
                         edge=(1.0 if won else 0.0) - px - fee * px))

    print(f"\ncross-asset lead-lag, one entry/round, lookback {args.lookback:.0f}s, "
          f"|move|>={args.min_mom_bps}bps, tau {args.tau_lo:.0f}-{args.tau_hi:.0f}s\n")
    hdr = f"  {'signal':<8}{'traded':<8}{'lag':>6}{'n':>6}{'ask':>8}{'realized':>10}{'edge/sh':>9}{'day-t':>8}{'days+':>7}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for lag in args.lag_ms:
        for sig in MKT:
            for trade in MKT:
                g = out.get((sig, trade, lag))
                if not g or len(g) < 25:
                    continue
                bd = collections.defaultdict(list)
                for x in g:
                    bd[x["day"]].append(x["edge"])
                dm = [st.mean(v) for v in bd.values()]
                sd = st.stdev(dm) if len(dm) > 1 else 0.0
                t = st.mean(dm) / (sd / len(dm) ** 0.5) if sd else 0.0
                tag = "  <-- cross" if sig != trade else ""
                print(f"  {sig:<8}{trade:<8}{lag:>6}{len(g):>6}{st.mean([x['px'] for x in g]):>8.4f}"
                      f"{st.mean([x['won'] for x in g]):>10.4f}{st.mean([x['edge'] for x in g]):>+9.4f}"
                      f"{t:>+8.2f}{sum(1 for v in dm if v > 0):>4}/{len(dm):<2}{tag}")
            print()

    # the specific claim: does the CROSS signal add anything over the OWN signal?
    print("  the claim's own test -- off-diagonal (cross) vs diagonal (own):")
    for lag in args.lag_ms:
        for trade in MKT:
            own = out.get((trade, trade, lag))
            crs = [out.get((s, trade, lag)) for s in MKT if s != trade]
            crs = [x for x in crs if x]
            if not own or not crs:
                continue
            om = st.mean([x["edge"] for x in own])
            cm = st.mean([x["edge"] for g in crs for x in g])
            print(f"    trade {trade:<4} lag {lag:>5}ms : own {om:+.4f} (n={len(own)})"
                  f"   cross {cm:+.4f} (n={sum(len(g) for g in crs)})"
                  f"   {'cross is BETTER' if cm > om else 'cross is NOT better'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
