"""Where is the venue measurably wrong? By EXECUTABLE price, not by its display.

The calibration table (tools/venue_vs_spot) says the venue underprices the
underdog: it claims 0.030 for its cheapest bucket and 0.037 happens, claims 0.106
for the next and 0.143 happens, and claims 0.487 for a coin flip and 0.567
happens. But that was measured on the DISPLAYED price, and we pay the ASK -- so
the whole question is whether the edge survives the spread we actually cross.

This sweeps the executable ask (both sides, every round, at a fixed tau) and
reports, per price band: the win rate, the break-even rate that band's ask
demands, the EV per $1 after the venue's fee, and the significance tests that
have killed every previous claim here (pooled t, day-clustered t, halves,
drop-the-best-day).

A band is only real if the realised rate beats `ask / 0.98` and survives those.

Usage:  python -m tools.longshot_bands [--db data/btcpredict.db] [--tau 10]
"""
from __future__ import annotations

import argparse
import collections
import math
import sqlite3
import time
from pathlib import Path

from btcpredict.config import settings
from tools.backtest_impulse_orders import sigma_1m_by_round

BANDS = [(0.02, 0.03), (0.03, 0.05), (0.05, 0.08), (0.08, 0.12),
         (0.12, 0.20), (0.20, 0.35), (0.35, 0.50), (0.50, 0.70)]


def report(rows, label):
    """rows: (day, ask, won)."""
    if not rows:
        print(f"  {label:<16} none"); return
    n = len(rows)
    wr = sum(1 for _, _, w in rows if w) / n
    ask = sum(a for _, a, _ in rows) / n
    be = ask / 0.98                     # break-even win rate at this price
    ev = sum(((0.98 / a) - 1.0) if w else -1.0 for _, a, w in rows) / n
    vals = [((0.98 / a) - 1.0) if w else -1.0 for _, a, w in rows]
    m = sum(vals) / n
    sd = (sum((v - m) ** 2 for v in vals) / n) ** 0.5
    t = m / (sd / n ** 0.5) if sd else 0.0
    byday = collections.defaultdict(list)
    for (d, _, _), v in zip(rows, vals):
        byday[d].append(v)
    dm = [sum(v) / len(v) for v in byday.values()]
    mdd = sum(dm) / len(dm)
    sdd = (sum((x - mdd) ** 2 for x in dm) / len(dm)) ** 0.5
    td = mdd / (sdd / len(dm) ** 0.5) if sdd else 0.0
    half = n // 2
    h1 = sum(vals[:half]) / max(1, half)
    h2 = sum(vals[half:]) / max(1, n - half)
    drop1 = sorted(dm)[:-1]
    flag = ""
    if wr > be and td > 1.0 and h1 > 0 and h2 > 0 and len(drop1) > 0 \
            and sum(drop1) / len(drop1) > 0:
        flag = "  <== real" if t > 1.5 else "  <== real-ish"
    print(f"  {label:<16} n={n:<5} WR {100*wr:5.2f}% vs break-even "
          f"{100*be:5.2f}%  ({wr/be:4.2f}x)  EV{ev:+7.3f}  pooled-t{t:+5.2f}  "
          f"daily-t{td:+5.2f}  halves {h1:+.2f}/{h2:+.2f}  "
          f"drop1 {sum(drop1)/len(drop1):+.2f}{flag}")


def simulate_live(con, rounds, windows, band, min_tau):
    """The arm's ACTUAL rule: fire on the first quote whose CHEAP ask is inside
    the band, with tau below the window's ceiling.

    `--tau` picks the snapshot nearest a horizon, which is not what the arm does.
    The arm polls ~33/s and buys the instant the condition becomes true, so with
    a ceiling of 20s it always enters at tau~20 -- the earliest the window opens.
    This simulates that, so a ceiling can be moved on evidence rather than taste.
    """
    lo, hi_px = band
    quotes = {}
    for slug, start_ms, winner in rounds:
        rows = con.execute(
            "SELECT ts_ms, tau_s, up_ask, down_ask FROM quotes "
            "WHERE slug=? AND up_ask IS NOT NULL AND down_ask IS NOT NULL "
            "AND tau_s IS NOT NULL AND tau_s >= ? AND tau_s <= ? "
            "ORDER BY ts_ms ASC", (slug, min_tau, max(windows))).fetchall()
        if rows:
            quotes[slug] = (start_ms, winner, rows)
    print(f"=== THE LIVE RULE, simulated: first quote whose cheap ask is in "
          f"{lo:.2f}-{hi_px:.2f} (tau >= {min_tau:.0f}s) ===")
    for hi in windows:
        out, taus = [], []
        for _slug, (start_ms, winner, rows) in quotes.items():
            for _ts, tau_s, up_ask, dn_ask in rows:
                if tau_s > hi or not (0 < up_ask < 1 and 0 < dn_ask < 1):
                    continue
                cheap = min(up_ask, dn_ask)
                if lo <= cheap <= hi_px:
                    side = "Up" if up_ask < dn_ask else "Down"
                    out.append((start_ms // 86400000, cheap, winner == side))
                    taus.append(tau_s)
                    break
        report(out, f"tau<={hi:.0f}s")
        if taus:
            med = sorted(taus)[len(taus) // 2]
            print(f"                  entered at tau {min(taus):.0f}-{max(taus):.0f}s "
                  f"(median {med:.0f}s), {100*len(out)/len(quotes):.0f}% of rounds")
    print()


SPLIT_EDGES = {
    "tau": [(3, 5), (5, 6), (6, 7), (7, 9)],
    "ask": [(0.08, 0.10), (0.10, 0.13), (0.13, 0.16), (0.16, 0.20)],
    "z": [(0.0, 0.25), (0.25, 0.5), (0.5, 0.8), (0.8, 99.0)],
    "ret1": [(-1e9, -20), (-20, -5), (-5, 5), (5, 20), (20, 1e9)],
    "spread": [(0.0, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 2.0)],
    "div": [(-1.0, -0.02), (-0.02, 0.02), (0.02, 0.05), (0.05, 1.0)],
}


def simulate_splits(con, rounds, band, min_tau, max_tau, sigmas, which,
                    min_z: float = 0.0, min_div: float | None = None) -> None:
    """Which conditioner inside the SHIPPED cell lifts the win rate for free?

    A longshot's win rate is trivially buyable with price -- buy at 0.40 and you
    win 40% of the time -- so "raise the win rate" is only a real question when
    the price is held fixed. This takes every entry the live rule would take
    (first cheap ask inside the band, tau inside the window) and re-cuts that
    fixed population by one feature at a time, running the full battery on each
    bucket: a bucket that lifts the rate but not the EV, or that only works in
    one half of the sample, has not found anything.
    """
    lo, hi_px = band
    ents = []
    for slug, start_ms, winner in rounds:
        rows = con.execute(
            "SELECT ts_ms, tau_s, up_ask, down_ask, up_price, down_price, spot, "
            "strike FROM quotes WHERE slug=? AND up_ask IS NOT NULL "
            "AND down_ask IS NOT NULL AND tau_s IS NOT NULL "
            "AND tau_s >= ? AND tau_s <= ? ORDER BY ts_ms ASC",
            (slug, min_tau, max_tau)).fetchall()
        if not rows:
            continue
        sigma = sigmas.get(slug)
        for ts_ms, tau, ua, da, up_px, dn_px, spot, strike in rows:
            cheap = min(ua, da)
            if not (lo <= cheap <= hi_px):
                continue
            side = "Up" if ua < da else "Down"
            disp = up_px if side == "Up" else dn_px
            old = con.execute(
                "SELECT spot FROM quotes WHERE slug=? AND ts_ms<=? AND spot IS NOT "
                "NULL ORDER BY ts_ms DESC LIMIT 1", (slug, ts_ms - 60_000)).fetchone()
            ret1 = (1e4 * (spot - old[0]) / old[0]
                    if old and old[0] and spot and spot > 0 else None)
            z = (abs(math.log(spot / strike)) / (sigma * math.sqrt(tau / 60.0))
                 if sigma and sigma > 0 and spot and strike and tau > 0 else None)
            div = (cheap - disp) if disp else None
            # The live arm claims the round on the FIRST in-band quote, so a gate
            # that would refuse it means the round is skipped -- not that we wait
            # for a better quote later in the same round.
            if z is None or z < min_z:
                break
            if min_div is not None and (div is None or div < min_div):
                break
            ents.append({
                "day": start_ms // 86400000, "ask": cheap,
                "won": winner == side, "tau": float(tau), "ret1": ret1,
                "z": z, "spread": ua + da - 1.0,
                "div": div, "side": side})
            break                      # one entry per round, the first one
    if not ents:
        print("no entries in that cell")
        return
    print(f"=== conditioners INSIDE the shipped cell: cheap ask {lo:.2f}-{hi_px:.2f}, "
          f"tau {min_tau:.0f}-{max_tau:.0f}s"
          f"{f', z >= {min_z:g}' if min_z else ''}"
          f"{f', div >= {min_div:g}' if min_div is not None else ''}"
          f" -- {len(ents)} entries ===")
    report([(e["day"], e["ask"], e["won"]) for e in ents], "SHIPPED CELL")
    for feat in which:
        if feat == "side":
            print("\n  --- by side ---")
            for sd in ("Down", "Up"):
                sub = [e for e in ents if e["side"] == sd]
                report([(e["day"], e["ask"], e["won"]) for e in sub], sd)
            continue
        if feat not in SPLIT_EDGES:
            print(f"\n  (no buckets defined for {feat})")
            continue
        print(f"\n  --- by {feat} ---")
        for a, b in SPLIT_EDGES[feat]:
            sub = [e for e in ents
                   if e[feat] is not None and a <= e[feat] < b]
            report([(e["day"], e["ask"], e["won"]) for e in sub],
                   f"{a:g} to {b:g}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--tau", type=float, default=10.0)
    ap.add_argument("--dist", type=float, default=None,
                    help="only rows within this many bps of the strike")
    ap.add_argument("--band", type=float, nargs=2, default=None,
                    help="report a single ask band only, e.g. --band 0.08 0.12")
    ap.add_argument("--window", type=float, nargs="+", default=None,
                    help="simulate the LIVE rule (first qualifying quote) for "
                         "these tau ceilings, e.g. --window 20 15 12 10 8")
    ap.add_argument("--split", nargs="+", default=None,
                    help="re-cut the shipped cell by these conditioners "
                         "(tau ask z ret1 spread div side) and run the full "
                         "battery on each bucket")
    ap.add_argument("--min-z", type=float, default=0.0,
                    help="with --split: only rounds whose entry is this many "
                         "sigma from the strike (moneyness_sd)")
    ap.add_argument("--min-div", type=float, default=None,
                    help="with --split: only rounds whose book ask sits this far "
                         "ABOVE the venue's displayed price (the display lag)")
    args = ap.parse_args()
    con = sqlite3.connect(Path(args.db))
    rounds = con.execute(
        "SELECT slug, start_ms, winner FROM rounds "
        "WHERE symbol='BTCUSDT' AND winner IN ('Up','Down')").fetchall()
    print(f"rounds {len(rounds):,}, tau={args.tau:.0f}s, price = the ASK we pay\n")

    if args.window:
        simulate_live(con, rounds, sorted(args.window, reverse=True),
                      args.band or (settings.ticket_min_price,
                                    settings.ticket_max_price),
                      settings.ticket_min_tau_s)
        if not args.band:
            return

    if args.split:
        band = args.band or (settings.ticket_min_price, settings.ticket_max_price)
        simulate_splits(con, rounds, band, settings.ticket_min_tau_s,
                        settings.ticket_max_tau_s,
                        sigma_1m_by_round(con, rounds), args.split,
                        min_z=args.min_z, min_div=args.min_div)
        return

    data = []
    for slug, start_ms, winner in rounds:
        row = con.execute(
            "SELECT up_price, down_price, up_ask, down_ask, spot, strike "
            "FROM quotes WHERE slug=? "
            "AND up_ask IS NOT NULL AND down_ask IS NOT NULL AND tau_s IS NOT NULL "
            "ORDER BY ABS(tau_s - ?) LIMIT 1", (slug, args.tau)).fetchone()
        if not row:
            continue
        up_px, dn_px, up_ask, dn_ask, spot, strike = row
        if args.dist is not None and spot and strike:
            if abs(spot - strike) / strike * 1e4 > args.dist:
                continue
        day = start_ms // 86400000
        if up_ask and 0 < up_ask < 1:
            data.append(("Up", day, up_ask, up_px, winner == "Up"))
        if dn_ask and 0 < dn_ask < 1:
            data.append(("Down", day, dn_ask, dn_px, winner == "Down"))

    if args.band:
        lo, hi = args.band
        rows = [(d, a, w) for _, d, a, _, w in data if lo <= a < hi]
        report(rows, f"ask {lo:.2f}-{hi:.2f}")
        return
    print("=== every price band, priced at the ASK (both sides) ===")
    for lo, hi in BANDS:
        rows = [(d, a, w) for _, d, a, _, w in data if lo <= a < hi]
        report(rows, f"ask {lo:.2f}-{hi:.2f}")

    print("\n=== the same, but only when the DISPLAY is above the ask")
    print("    (the venue's display has not caught up: it says the side is worth")
    print("     more than its own book is charging for it)")
    for lo, hi in BANDS:
        rows = [(d, a, w) for _, d, a, p, w in data
                if lo <= a < hi and (p or 0) > a + 0.01]
        report(rows, f"stale {lo:.2f}-{hi:.2f}")

    print("\n=== and the band the ticket actually traded (ask 0.02-0.05) split by")
    print("    whether the display agreed with the book ===")
    for lo, hi in ((0.02, 0.03), (0.03, 0.05)):
        both = [(d, a, w) for _, d, a, p, w in data if lo <= a < hi]
        stale = [(d, a, w) for _, d, a, p, w in data
                 if lo <= a < hi and (p or 0) > a + 0.01]
        fresh = [(d, a, w) for _, d, a, p, w in data
                 if lo <= a < hi and (p or 0) <= a + 0.01]
        report(both, f"{lo}-{hi} all")
        report(stale, f"{lo}-{hi} display high")
        report(fresh, f"{lo}-{hi} display<=ask")


if __name__ == "__main__":
    main()
