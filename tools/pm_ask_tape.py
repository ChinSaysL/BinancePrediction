"""THE ASK AT THE TRAIN TRIGGER, for the families whose live book was never recorded.

WHY THIS EXISTS. `tools/pm_ask_at_trigger.py` reads the ask out of the `quotes`
table, and that table only has btc/eth/bnb -- those are the families with a
strike source. sol/xrp/doge started writing quote rows at 2026-09-17 23:33 and
only got a strike at 03:34 (when the backfill shipped), so their live ask tape
is ~3 rounds deep and the sweep prints `triggers 0`: the ask is UNMEASURED,
which is why they were never sized.

There is exactly one other tape that carries their asks: `data/hf2/kartik`
(2026-07-17), the one-day Polymarket L2 capture -- 288 five-minute rounds for
EVERY family, sol/xrp/doge included -- paired with the Binance 1s bars of the
same day for the strike and the distance path. That is what this tool scores.

SAME GATES AS THE LIVE TOOL, ON PURPOSE. Two measurements of the same quantity
have to agree on a family we can check, or neither ranks anything:
  * strike  = `opens[start]` of the 1s bar at the round's start second, the
              proxy `pm_train_sweep.rounds_for()` already uses -- so this
              trigger is the trigger `TRAIN_P_WIN` was measured on;
  * trigger = the FIRST second with `60 <= tau <= 300` and `|d_bps| >= 15`,
              taken from `pm_train_sweep.triggers()` (imported, not re-derived);
  * side    = the sign of d_bps (the favourite -- what the train arm buys);
  * ask     = that side's top of book in the tape at that second;
  * fee     = 0.07*p*(1-p) per share, measured on a live fill; EV = (won - ask
              - fee)/ask;
  * winner  = `pm_tape.episode_winner()`, i.e. what the venue settled.
ONE ENTRY PER ROUND, first crossing only -- the row-level-inflation trap.

WHAT IT CANNOT DO -- READ BEFORE QUOTING A NUMBER
  * It is ONE day. The cluster column below is the UTC HOUR, not the day, and
    there is no second day to drop: `halves` is the same day's morning against
    its evening and `drop` drops the best hour. Nothing here passes the 4-check
    battery, it can only answer the ASK LEVEL question.
  * 2026-07-17 is two months old and this repo's rule is that live governs. Use
    it to price a family whose live asks do not exist yet, then confirm on the
    live tape once it has a day of rounds. btc prints every run for exactly
    that reason: its live answer is known, so btc measures how much the tape
    lies about the level.

Usage:
    python3 tools/pm_ask_tape.py                      # all six families
    python3 tools/pm_ask_tape.py --asset sol --limit 40
    python3 tools/pm_ask_tape.py --asset btc --thr 15 --lo 60 --hi 300
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import os
import statistics as st
import sys
from collections import defaultdict

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.pm_ask_at_trigger import (  # noqa: E402
    P_WIN, entry_ceiling, show)
from tools.pm_tape import KARTIK, episode_winner  # noqa: E402
from tools.pm_train_sweep import PERIODS, load_day, triggers  # noqa: E402

#: Only the columns this measurement touches. The episodes carry ~120 (the whole
#: 10-level ladder on both sides) and reading all of them per round is 10x the
#: work for none of the answer. `episode_winner` needs the ltp/bid/ask columns.
COLS = ["ts", "secs", "yes_ltp", "no_ltp", "yes_bid", "yes_ask", "no_bid",
        "no_ask"]

DAY = "2026-07-17"
#: How far the nearest tape snapshot may sit from the trigger second. The tape is
#: event-driven (median dt 1 ms), so anything past this is a hole in the capture,
#: not a price.
NEAR_MS = 5_000


def episodes(asset: str, interval: str = "5m", day: str = DAY) -> list[str]:
    pat = os.path.join(str(KARTIK), day, asset, interval, "*.parquet")
    return sorted(glob.glob(pat))


def book(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path, columns=COLS)
    return df.sort_values("ts", kind="stable").reset_index(drop=True)


def ask_at(df: pd.DataFrame, ts_ms: int, side: str) -> float | None:
    """The side's top-of-book ask at the trigger, NaN -> None (no book)."""
    ts = df["ts"].to_numpy()
    i = int(ts.searchsorted(ts_ms))
    j = min(max(i, 0), len(ts) - 1)
    if j > 0 and abs(int(ts[j - 1]) - ts_ms) < abs(int(ts[j]) - ts_ms):
        j = j - 1
    if abs(int(ts[j]) - ts_ms) > NEAR_MS:
        return None
    v = df["yes_ask" if side == "Up" else "no_ask"].to_numpy()[j]
    if v is None or pd.isna(v):
        return None
    return float(v)


def sweep(asset: str, a) -> dict:
    """Every 5m round of the tape for one asset, scored at its own trigger."""
    period = PERIODS["5m"]
    got = load_day(asset.upper() + "USDT", DAY)
    rows: list[tuple] = []
    asks: list[float] = []
    ok: list[tuple] = []
    ok_asks: list[float] = []
    ent: list[tuple] = []                      # all priced triggers, with ask
    n_round = n_unsettled = n_nospot = n_nobook = n_noentry = 0
    n_unaligned = n_unknown = 0
    sym = asset
    p_win = P_WIN.get(sym) or P_WIN["btc"]     # main.py's own fallback
    ceil = entry_ceiling(a.static_max, p_win, a.margin)
    if got is None:
        return {"rows": [], "asks": [], "ok": [], "ok_asks": [], "ent": [],
                "ceil": ceil, "p_win": p_win, "sym": sym, "rounds": 0,
                "nospot": 0, "unsettled": 0, "nobook": 0, "noentry": 0,
                "unaligned": 0, "unknown": 0}
    opens, closes = got
    for path in episodes(asset):
        if a.limit and n_round >= a.limit:
            break
        n_round += 1
        df = book(path)
        if df.empty:
            continue
        # The tape's `secs` is the countdown, so ts + secs*1000 is the close --
        # and it lands ~0.5 s BEFORE the 5m grid the round actually closes on
        # (measured over all 288 episodes: 299492 % 300000). Round to the
        # NEAREST grid, never floor it: flooring moves the window a whole round
        # back and silently prices the previous round's walk against the
        # previous round's strike. Past the tolerance, refuse the round.
        close_ms = int(st.median((df["ts"] + df["secs"] * 1000).to_numpy()))
        grid_ms = int(round(close_ms / (period * 1000.0))) * period * 1000
        if abs(grid_ms - close_ms) > a.snap_tol_ms:
            n_unaligned += 1
            continue
        start = grid_ms // 1000 - period
        win = episode_winner(df)
        if win is None:
            n_unknown += 1
            continue
        k = opens.get(start) or closes.get(start)
        seq = [closes.get(start + i) for i in range(period)]
        if not k or any(p is None for p in seq):
            n_nospot += 1
            continue
        tr = triggers(seq, k, a.thr, a.lo, a.hi, period)
        if tr is None:                          # never crossed inside the window
            n_noentry += 1
            continue
        side, tau, _d = tr
        ts_ms = (start + period - int(tau)) * 1000
        ask = ask_at(df, ts_ms, side)
        if ask is None or ask <= 0 or ask >= 0.995:
            n_nobook += 1
            continue
        won = 1.0 if win == ("YES" if side == "Up" else "NO") else 0.0
        # One cluster per UTC hour: one day of tape has no days to cluster on,
        # so the "day" slot carries the hour and the labels say so.
        bucket = dt.datetime.fromtimestamp(ts_ms / 1000.0,
                                           dt.timezone.utc).strftime("%H")
        paid = min(ask + a.slippage, ceil if ceil else ask + a.slippage)
        rec = (bucket, won, (won - ask - fee_of(ask)) / ask,
               (won - paid - fee_of(paid)) / paid)
        rows.append(rec)
        asks.append(ask)
        ent.append((side, ask, won, ts_ms))
        if ceil is not None and ask <= ceil:
            ok.append(rec)
            ok_asks.append(ask)
    return {"rows": rows, "asks": asks, "ok": ok, "ok_asks": ok_asks, "ent": ent,
            "ceil": ceil, "p_win": p_win, "sym": sym, "rounds": n_round,
            "nospot": n_nospot, "unsettled": n_unsettled, "nobook": n_nobook,
            "noentry": n_noentry, "unaligned": n_unaligned,
            "unknown": n_unknown}


def fee_of(px: float) -> float:
    from tools.pm_ask_at_trigger import fee_per_share
    return fee_per_share(px)


def report(g: dict) -> None:
    print("\n%-6s rounds %d  TRIGGERS %d   (never crossed %d, no spot %d, "
          "unknown winner %d)" % (g["sym"], g["rounds"], len(g["rows"]),
                                  g["noentry"], g["nospot"], g["unknown"]))
    if g["nobook"] or g["unaligned"]:
        print("       no book at the trigger %d, round refused (not on the 5m "
              "grid) %d" % (g["nobook"], g["unaligned"]))
    if not g["rows"]:
        print("       no priced trigger -- nothing to read")
        return
    print("       ceiling %.3f (p_win %.3f%s)   ask med %.3f"
          % (g["ceil"], g["p_win"],
             ", BTC FALLBACK, never swept" if P_WIN.get(g["sym"]) is None
             else "", st.median(g["asks"])))
    try:
        show("all triggers", g["rows"], g["asks"])
    except st.StatisticsError:
        print("   all triggers           n=%-4d win %5.1f%%  $/1 %+.4f  "
              "(single hour: no cluster check)" % (
                  len(g["rows"]), 100 * st.fmean(r[1] for r in g["rows"]),
                  st.fmean(r[2] for r in g["rows"])))
    if g["ok"]:
        try:
            show("AFTER the EV ceiling", g["ok"], g["ok_asks"], 3)
        except st.StatisticsError:
            print("   AFTER the EV ceiling   n=%-4d win %5.1f%%  $/1 %+.4f  "
                  "(single hour)" % (
                      len(g["ok"]), 100 * st.fmean(r[1] for r in g["ok"]),
                      st.fmean(r[3] for r in g["ok"])))
    else:
        print("   AFTER the EV ceiling   n=0  -- every trigger costs more than "
              "p_win - fee can pay")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="",
                    help="one asset or a comma list, else all six")
    ap.add_argument("--limit", type=int, default=0, help="cap rounds per asset")
    ap.add_argument("--thr", type=float, default=15.0, help="train bps gate")
    ap.add_argument("--lo", type=float, default=60.0, help="min tau")
    ap.add_argument("--hi", type=float, default=300.0, help="max tau")
    ap.add_argument("--static-max", type=float, default=0.890,
                    help="the live cross_train_max_ask")
    ap.add_argument("--margin", type=float, default=0.03,
                    help="the live ev_ceiling_margin")
    ap.add_argument("--slippage", type=float, default=0.02,
                    help="the live pm_slippage, added above the ask")
    ap.add_argument("--snap-tol-ms", type=float, default=60_000.0,
                    help="how far the tape's own close may sit from the 5m grid")
    a = ap.parse_args()

    assets = ([s.strip() for s in a.asset.split(",")] if a.asset
              else ["btc", "eth", "bnb", "sol", "xrp", "doge"])
    print("=== the ASK at the train trigger, from the 2026-07-17 kartik L2 "
          "tape ===")
    print("    gates |d| >= %.0fbps, tau %.0f-%.0fs, FIRST crossing per round, "
          "one entry per round;" % (a.thr, a.lo, a.hi))
    print("    fee/share = 0.07*p*(1-p); paid basis = min(ask+%.2f, ceiling)"
          % a.slippage)
    print("    ONE DAY: the cluster column is the UTC HOUR, not the day -- "
          "this cannot pass the 4-check battery.")
    grand: list[dict] = []
    for asset in assets:
        g = sweep(asset, a)
        grand.append(g)
        report(g)

    print("\n=== the finding: what each family costs at its own trigger ===")
    print("%-6s %6s %7s %8s %7s %8s %8s %10s" % (
        "fam", "trig", "win%", "ask med", "ceil", "adm/trig", "adm win%",
        "adm $/1 paid"))
    for g in grand:
        if not g["rows"]:
            print("%-6s %6d %7s %8s %7.3f %8s %8s %10s" % (
                g["sym"], 0, "-", "-", g["ceil"], "0", "-", "-"))
            continue
        aw = 100 * st.fmean(r[1] for r in g["ok"]) if g["ok"] else float("nan")
        ap_ = st.fmean(r[3] for r in g["ok"]) if g["ok"] else float("nan")
        print("%-6s %6d %6.1f%% %8.3f %7.3f %8s %7.1f%% %+10.4f" % (
            g["sym"], len(g["rows"]),
            100 * st.fmean(r[1] for r in g["rows"]), st.median(g["asks"]),
            g["ceil"], "%d/%d" % (len(g["ok"]), len(g["rows"])), aw, ap_))
    print("\n   adm = triggers the live ceiling would actually allow "
          "(p_win of the family, entry_ceiling(%.3f, p_win, %.2f))."
          % (a.static_max, a.margin))
    print("   A family whose adm/trig is 0 is a family the arm never trades, "
          "whatever its sweep rank.")
    print("   btc is the control: its LIVE ask tape is also known, so compare "
          "btc here against `pm_ask_at_trigger.py` before trusting the other "
          "levels.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
