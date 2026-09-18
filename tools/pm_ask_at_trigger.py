"""THE ASK AT THE TRIGGER, measured retrospectively from the recorded book.

WHY THIS IS THE ONLY MEASUREMENT THAT DECIDES ANYTHING.

`pm_train_sweep.py` ranks the SIGNAL: it answers "when spot is >= 15bps through
the strike with 60-300s left, how often does it hold?" It never sees the
venue's ask, and the arm earns `win - ask - fee`. A family can win 87% and still
lose money if the market charges 0.90 for it -- and the first unconditional
probe (`pm_ask_probe.py`) showed exactly that shape: once a side is decided its
ask runs 0.90-0.98, above every candidate's EV ceiling.

The trap is to read those unconditional samples as the answer. They are not:
the train arm fires on a CONDITION, and that condition is precisely the moment
the favourite gets expensive. The ask has to be read AT THE TRIGGER.

WHY IT IS NOT A LIVE PROBE. `tools/pm_trigger_watch.py` waits for the trigger in
real time, which costs one sample per round per family. But the bot has been
recording the top of book into `quotes` (up_ask/down_ask) next to `spot`,
`strike` and `tau_s` on every poll -- so every round it has ever seen can be
scored after the fact, with no waiting. Same measurement the live probe would
make, with n = rounds instead of n = minutes.

PER ROUND, FIRST CROSSING ONLY (so there is exactly one entry per round -- the
row-level-inflation trap this repo has already been bitten by):

  * strike  = the round's recorded reference (the Binance wrapper startPrice,
              the same proxy the sweep uses: `opens[start]` of the 1m bar at
              the round's start second);
  * d_bps   = 1e4 * (spot - strike) / strike, row by row;
  * trigger = the first row with `60 <= tau <= 300` and `|d_bps| >= 15`, tau
              counted from the round start exactly as `pm_train_sweep.triggers()`
              counts it (past the window -> the round has no entry);
  * side    = the sign of d_bps (the favourite -- what the train arm buys);
  * ask     = the recorded ask on THAT side at that row: the price it pays.

Then the four-check battery on the win rate, and -- the decisive column -- the
same battery restricted to triggers whose ask the live EV ceiling would ALLOW,
because those are the only ones the running config actually trades.

READ THE SIGN. The ceiling column is the finding, not the win rate: a family
whose triggers all sit above its ceiling is a family the arm never trades, and
its sweep rank is irrelevant to P&L.

Usage:
    .venv/bin/python tools/pm_ask_at_trigger.py
    .venv/bin/python tools/pm_ask_at_trigger.py --family btc-updown-5m --thr 15
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sqlite3
import statistics as st
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.venues.polymarket.broker import (  # noqa: E402
    TAKER_FEE_RATE, entry_ceiling)

#: Measured per symbol -- `TRAIN_P_WIN` in the live config. A family missing
#: here falls back to btc's number, exactly as `main.py` does
#: (`TRAIN_P_WIN.get(sym) or settings.train_p_win`), so the ceiling tested here
#: is the ceiling actually running. The fallback is loose for a family whose own
#: win rate is lower -- it overpays -- so the printed ceiling flags it.
P_WIN = {"btc": 0.914, "sol": 0.893, "xrp": 0.878, "doge": 0.869,
         "eth": None, "bnb": None}


def fee_per_share(px: float) -> float:
    """Taker fee, dollars per share. Measured on a live fill, not published."""
    return TAKER_FEE_RATE * px * (1.0 - px)


def battery(recs: list[tuple], basis: int) -> dict:
    """recs = [(day, won 0/1, pnl_at_ask, pnl_at_paid)]. `basis` picks which
    P&L the four checks run on: 2 = the recorded book ask, 3 = what live pays
    (`min(ask + slippage, ceiling)`) -- and only 3 is what the arm earns."""
    n = len(recs)
    if n < 8:
        return {"n": n}
    by_day: dict[str, list[float]] = defaultdict(list)
    for r in recs:
        by_day[r[0]].append(r[basis])
    order = sorted(by_day)
    pnls = [r[basis] for r in recs]
    wr = st.fmean(r[1] for r in recs)
    mean, sd = st.fmean(pnls), (st.stdev(pnls) if n > 1 else 0.0)
    se = math.sqrt(max(wr * (1 - wr), 1e-9) / n)
    dm = [st.fmean(by_day[d]) for d in order]
    half = len(order) // 2
    h1 = [p for d in order[:half] for p in by_day[d]]
    h2 = [p for d in order[half:] for p in by_day[d]]
    best = max(order, key=lambda d: st.fmean(by_day[d]))
    drop = [p for d in order if d != best for p in by_day[d]]
    return {"n": n, "wr": wr, "z": (wr - 0.5) / se, "mean": mean,
            "t_pool": mean / (sd / math.sqrt(n)) if sd else float("nan"),
            "t_day": (st.fmean(dm) / (st.stdev(dm) / math.sqrt(len(dm)))
                      if len(dm) > 1 and st.stdev(dm) > 0 else float("nan")),
            "h1": st.fmean(h1) if h1 else float("nan"),
            "h2": st.fmean(h2) if h2 else float("nan"),
            "drop": st.fmean(drop) if drop else float("nan"),
            "days": len(order)}


def calibration(recs: list[tuple], asks: list[float]) -> None:
    """The decisive table: win rate against the price charged, by bucket.

    If the win rate tracks the ask, the market already knows what the trigger
    knows and there is no price at which this is +EV. A bucket where the win
    rate is above `ask + fee` is the only place an edge could live.
    """
    print("\n--- win rate by ask bucket (is ANY price +EV?) ---")
    print("   %-12s %5s %7s %7s %9s   %s"
          % ("ask bucket", "n", "win %", "mean ask", "edge/sh",
             "vs break-even"))
    for lo, hi in ((0.0, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.85),
                   (0.85, 0.9), (0.9, 0.95), (0.95, 1.01)):
        sub = [(r, a) for r, a in zip(recs, asks) if lo <= a < hi]
        if len(sub) < 8:
            continue
        wr = st.fmean(r[1] for r, _ in sub)
        ma = st.fmean(a for _, a in sub)
        edge = wr - ma - fee_per_share(ma)
        print("   %.2f-%.2f    %5d %6.1f%% %7.3f %+9.4f   %s"
              % (lo, hi, len(sub), 100 * wr, ma, edge,
                 ("above" if edge > 0 else "below by %.1fc" % (-100 * edge))))


def bands(recs: list[tuple], asks: list[float], slip: float,
          ceiling: float) -> None:
    """Candidate ask bands, scored the way live would trade them.

    Only bands at or below `ceiling` are real: above it the arm refuses, so a
    band there cannot be traded no matter how good it looks. The P&L is on the
    PAID basis (`min(ask + slippage, ceiling)`), never on the ask -- pricing
    the entry at the displayed price is the first fake-winner in this repo's
    list and it is exactly what makes a losing band look positive.
    """
    print("\n--- candidate ask bands (paid basis, ceiling %.3f) ---" % ceiling)
    print("   %-13s %5s %7s %9s %9s %8s %8s   %s"
          % ("band", "n", "win %", "edge/sh", "$/1 paid", "t", "day-t",
             "halves / drop-best"))
    for lo, hi in ((0.50, 0.70), (0.60, 0.70), (0.70, 0.80), (0.80, 0.85),
                   (0.85, 0.877), (0.60, 0.80), (0.80, 0.877),
                   (0.60, 0.877), (0.55, 0.877), (0.877, 1.01)):
        sub = [(r, a) for r, a in zip(recs, asks) if lo <= a < hi]
        if len(sub) < 8:
            print("   %.2f-%.2f       %5d  (too thin to read)" % (lo, hi,
                                                                  len(sub)))
            continue
        wr = st.fmean(r[1] for r, _ in sub)
        ma = st.fmean(a for _, a in sub)
        edge = wr - ma - fee_per_share(ma)
        if lo >= ceiling:
            # NO paid number here. Every member of this band sits above the
            # ceiling, so `min(ask + slippage, ceiling)` would book every fill
            # at the ceiling -- a price that is not on the book. That artifact
            # printed t +8.73 on an otherwise ordinary band, which is precisely
            # the first fake-winner in this repo's list. Report the ask basis
            # and mark it untradeable.
            print("   %.2f-%.2f     %5d %6.1f%% %+9.4f %9s %8s %8s   %s"
                  % (lo, hi, len(sub), 100 * wr, edge, "n/a", "n/a", "n/a",
                     "live REFUSES this band (ask > ceiling)"))
            continue
        b = battery([r for r, _ in sub], 3)
        print("   %.2f-%.2f     %5d %6.1f%% %+9.4f %+9.4f %+8.2f %+8.2f   "
              "%+.4f/%+.4f"
              % (lo, hi, b["n"], 100 * b["wr"], edge, b["mean"], b["t_pool"],
                 b["t_day"], b["h1"], b["drop"]))


def show(tag: str, recs: list[tuple], asks: list[float],
         basis: int = 2) -> None:
    """`basis` 2 = P&L at the recorded ask, 3 = as paid. Report the ask basis
    for the unfiltered set (live cannot pay above the ceiling, so a paid basis
    there would invent fills at the ceiling) and the paid basis once the
    ceiling has done its selecting."""
    b = battery(recs, basis)
    if b["n"] < 8:
        print("   %-22s n=%-4d (too thin to read)" % (tag, b["n"]))
        return
    print("   %-22s n=%-4d win %5.1f%% z %+5.2f  ask med %.3f [%.2f-%.2f]  "
          "$/1 %s  t %+5.2f  day-t %+5.2f  halves %+.4f/%+.4f  drop %+.4f"
          % (tag, b["n"], 100 * b["wr"], b["z"], st.median(asks), min(asks),
             max(asks),
             ("at ask %+.4f  as PAID %+.4f" % (st.fmean(r[2] for r in recs),
                                               st.fmean(r[3] for r in recs)))
             if basis == 3 else ("at ask %+.4f (live pays more: ask+slippage)"
                                 % st.fmean(r[2] for r in recs)),
             b["t_pool"], b["t_day"], b["h1"], b["h2"], b["drop"]))


def score_family(con, fam: str, winners: dict, a) -> dict:
    """One pass over the family's recorded book, grouped by round."""
    period = 900 if "15m" in fam else 300
    rows: list[tuple[str, float, float]] = []
    asks: list[float] = []
    ok: list[tuple[str, float, float]] = []
    ok_asks: list[float] = []
    sym = fam.split("-")[0]
    p_win = P_WIN.get(sym) or P_WIN["btc"]          # main.py's own fallback
    ceil = entry_ceiling(a.static_max, p_win, a.margin)
    n_trig = n_unsettled = n_nobook = 0
    cur, found, pending = None, False, None

    def flush() -> None:
        if pending is None:
            return
        slug, side, day, ask = pending
        won = 1.0 if winners[slug] == side else 0.0
        # What live pays is NOT the recorded ask: `broker.plan()` reaches
        # `min(ask + slippage, ceiling)`, so the ask is the best case only.
        paid = min(ask + a.slippage, ceil if ceil else ask + a.slippage)
        rec = (day, won,
               (won - ask - fee_per_share(ask)) / ask,
               (won - paid - fee_per_share(paid)) / paid)
        rows.append(rec)
        asks.append(ask)
        if ceil is not None and ask <= ceil:
            ok.append(rec)
            ok_asks.append(ask)

    for slug, ts_ms, spot, ua, da, sk in con.execute(
            "select slug, ts_ms, spot, up_ask, down_ask, strike from quotes "
            "where slug like ? order by slug, ts_ms", (fam + "-%",)):
        if slug != cur:                       # round boundary
            flush()
            pending, cur, found = None, slug, False
        if found or not sk or sk <= 0 or not spot or spot <= 0:
            continue
        try:
            start = int(slug.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            continue
        tau = start + period - ts_ms / 1000.0
        if tau > a.hi:
            continue
        if tau < a.lo:                        # window closed: no entry
            found = True
            continue
        d = 1e4 * (spot - sk) / sk
        if abs(d) < a.thr:
            continue
        found = True                          # first crossing: decided
        up = d > 0
        ask = ua if up else da
        if not ask or ask <= 0 or ask >= 0.995:
            n_nobook += 1                     # triggered, nothing to price
            continue
        if slug not in winners:               # not settled yet
            n_unsettled += 1
            continue
        n_trig += 1
        pending = (slug, "Up" if up else "Down", dt.datetime.fromtimestamp(
            ts_ms / 1000.0, dt.timezone.utc).strftime("%m-%d"), float(ask))
    flush()
    return {"rows": rows, "asks": asks, "ok": ok, "ok_asks": ok_asks,
            "ceil": ceil, "sym": sym, "trig": n_trig, "p_win": p_win,
            "fallback": P_WIN.get(sym) is None,
            "unsettled": n_unsettled, "nobook": n_nobook}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--thr", type=float, default=15.0, help="train bps gate")
    ap.add_argument("--lo", type=float, default=60.0, help="min tau")
    ap.add_argument("--hi", type=float, default=300.0, help="max tau")
    ap.add_argument("--static-max", type=float, default=0.890,
                    help="the live cross_train_max_ask")
    ap.add_argument("--margin", type=float, default=0.03,
                    help="the live ev_ceiling_margin")
    ap.add_argument("--slippage", type=float, default=0.02,
                    help="the live pm_slippage, which plan() adds above the ask")
    ap.add_argument("--family", default="", help="one prefix, else all found")
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    winners = {s: w for s, w in con.execute(
        "select slug, winner from rounds where winner in ('Up','Down')")}
    fams = [r[0] for r in con.execute(
        "select distinct substr(slug,1,length(slug)-11) f from quotes order by f")]
    if a.family:
        fams = [f for f in fams if f == a.family]

    print("=== the ASK at the train trigger (from the recorded book) ===")
    print("    gates |d| >= %.0fbps, tau %.0f-%.0fs, FIRST crossing per round;"
          " fee/share = %.2f*p*(1-p) measured"
          % (a.thr, a.lo, a.hi, TAKER_FEE_RATE))

    grand: list[dict] = []
    for fam in fams:
        g = score_family(con, fam, winners, a)
        grand.append(g)
        print("\n%-18s triggers %-5d (no book at trigger %d, unsettled %d)"
              % (fam, g["trig"], g["nobook"], g["unsettled"]))
        if g["ceil"] is not None:
            print("   ceiling %.3f (p_win %.3f%s) -> %d of %d priced triggers "
                  "affordable" % (g["ceil"], g["p_win"],
                                  ", BTC FALLBACK, never swept"
                                  if g["fallback"] else "",
                                  len(g["ok"]), len(g["rows"])))
        show("all triggers", g["rows"], g["asks"])
        if g["ok"]:
            show("AFTER the EV ceiling", g["ok"], g["ok_asks"])

    print("\n=== pooled ===")
    show("every trigger", [r for g in grand for r in g["rows"]],
         [x for g in grand for x in g["asks"]])
    pooled_ok = [r for g in grand for r in g["ok"]]
    pooled_ok_asks = [x for g in grand for x in g["ok_asks"]]
    if pooled_ok:
        show("after the ceiling", pooled_ok, pooled_ok_asks, 3)

    all_recs = [r for g in grand for r in g["rows"]]
    all_asks = [x for g in grand for x in g["asks"]]
    if all_recs:
        calibration(all_recs, all_asks)
        bands(all_recs, all_asks, a.slippage,
              entry_ceiling(a.static_max, P_WIN["btc"], a.margin))

    tot = sum(len(g["rows"]) for g in grand)
    if tot:
        print("\n   the EV ceiling keeps %d of %d priced triggers (%.0f%%)"
              % (len(pooled_ok), tot, 100.0 * len(pooled_ok) / tot))
    else:
        print("\n   NOTE: no family has a priced ask at the trigger yet -- the "
              "book feed only covers what `cross_ws_follow_all` subscribes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
