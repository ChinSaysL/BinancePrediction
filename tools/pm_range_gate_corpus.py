"""The round-range gate, swept on 8 days of OUT-OF-SAMPLE spring data.

WHY THIS EXISTS. `tools/pm_range_gate_sweep.py` ran on the live PM record and
could only rank `btc-updown-5m` (n=28 at gate 6) and `eth-updown-5m` (n=15); the
other seven families had 6-16 triggers, and the exact argmax flipped in 3 of the
4 families that cleared 20. That is the sample size, not the gate: the live DB
holds only ~198 venue-quoted rounds per 5m family and ~64 per 15m family, and a
trigger needs a crossing inside tau 5-60s (17% of rounds) on top of that.

The 53-day `data/btc5m/` corpus (2026-04-29..06-20) does not have that problem
for BTC 5m -- but it is a Binance-tape corpus with NO venue prices, and the whole
question is EV = P(win) - ask - fee. `data/btc5m_prices.parquet` closes it: per
SECOND Polymarket prices for 2,240 windows, `up_buy_px`/`dn_buy_px` = the ask.
Joined to `metadata.json` (`priceToBeat` = the Chainlink strike, `finalPrice` =
settlement) and `ticks_dense.parquet` (1 s tape) that is a COMPLETE replay:

    windows with all of it   2,240   (8 days: 04-29, 05-03, 05-07, 05-11,
                                      05-15, 05-19, 05-23, 05-27)
    live DB, same family       198   -> 11x

AND IT IS A DIFFERENT REGIME. The live sweep is September 2026 on Polymarket;
this is April/May 2026, a different fee era (min order $5, tick 0.001 vs 0.01)
and a different venue book. That makes it a real HELD-OUT test of the one thing
the live sweep could actually claim: the DIRECTION. `btc-updown-5m` wanted a
TIGHTER gate than the shipped 20.0 in that sweep. If that repeats here it is a
finding; if it flips, the September number was the grid edge on n=28.

WHAT IS AND IS NOT FAITHFUL (read this before quoting a level):
  * `crossed_side` is IMPORTED from `btcpredict.main`, not reimplemented -- the
    same discipline as the live sweep. Ticks are the 1 s `ticks_dense` closes.
  * The round range is the bot's own definition,
        range = 1e4 * (max(spot) - min(spot)) / strike   over [open, trigger]
    reproduced from `_round_range_bps`.
  * Gates applied: tau in [5, 60], ask in [min_price, max_price] and <=
    `cross_max_fill_px`, and the crossing depth <= `cross_max_depth_bps`.
  * APPROXIMATED, and it does not bias the RANKING because every cell of the
    sweep shares it: `cross_min_depth_ticks` (the live arm counts ticks through
    the strike at poll rate; here it is 1 Hz) and `cross_max_depth_bps` is
    computed from the 1 s close rather than the trigger sub-second.
  * The venue rows start at tau >= 10 for some windows, so the tau 5-10 slice of
    the window is missing from THOSE windows. Reported, not hidden.
  * ONE ENTRY PER ROUND: the first crossing in the window. Row-level inflation is
    the #1 way a losing strategy looks profitable in this repo.

Usage:
    python3 tools/pm_range_gate_corpus.py
    ... --entry-lag-ms 500        # the realistic one; read the decay across lags
    ... --sweep floor
"""
from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import time

import numpy as np
import pandas as pd

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))          # tools/, for the shared battery
sys.path.insert(0, str(_HERE.parents[1]))      # repo root, for btcpredict

from btcpredict.main import crossed_side                    # noqa: E402
from pm_live_band_sweep import battery, fee_per_share       # noqa: E402

RANGE_GRID = [4, 6, 8, 10, 12, 13, 14, 16, 18, 20, 22, 26, 99]
FLOOR_GRID = [0.10, 0.20, 0.30, 0.40, 0.50, 0.55, 0.60, 0.65]


def load_corpus(root: str, prices: str, look_s: float, min_tau: float,
                max_tau: float, min_price: float, max_price: float,
                max_fill_px: float, max_depth_bps: float,
                entry_lag_ms: int, max_book_age_ms: int) -> tuple[list[dict], dict]:
    p = pd.read_parquet(prices)
    p["day"] = p.window_id.str.split("/").str[0]
    p["epoch"] = p.window_id.str.split("/").str[1].astype(int)
    by_win = {e: g.sort_values("sec") for e, g in p.groupby("epoch")}
    rootp = pathlib.Path(root)

    rows: list[dict] = []
    skipped = {"no_prices": 0, "no_meta": 0, "no_tape": 0, "no_trigger": 0,
               "no_ask_after_lag": 0, "outside_band": 0, "too_deep": 0,
               "no_range": 0}
    windows = 0
    for epoch, g in by_win.items():
        d = rootp / str(g.iloc[0]["day"]) / str(epoch)
        if not (d / "metadata.json").exists():
            skipped["no_meta"] += 1
            continue
        if not (d / "ticks_dense.parquet").exists():
            skipped["no_tape"] += 1
            continue
        try:
            m = json.loads((d / "metadata.json").read_text())
            ev = (m.get("events") or [{}])[0].get("eventMetadata") or {}
            strike, final = float(ev["priceToBeat"]), float(ev["finalPrice"])
        except Exception:
            skipped["no_meta"] += 1
            continue
        if not (strike > 0 and final > 0):
            skipped["no_meta"] += 1
            continue
        winner = "Up" if final >= strike else "Down"
        t = pd.read_parquet(d / "ticks_dense.parquet",
                            columns=["timestamp", "close"])
        t = t[t.close > 0].sort_values("timestamp")
        if len(t) < 2:
            skipped["no_tape"] += 1
            continue
        windows += 1
        ticks = [(int(ts) * 1000, float(c))
                 for ts, c in zip(t.timestamp, t.close)]

        pr = g[["sec", "tau_s", "up_buy_px", "dn_buy_px"]].dropna(
            subset=["up_buy_px", "dn_buy_px"], how="all").sort_values("sec")
        pr_ts = (int(epoch) * 1000 + pr.sec.astype(int) * 1000).to_numpy()
        pr_up = pr.up_buy_px.to_numpy()
        pr_dn = pr.dn_buy_px.to_numpy()

        # ⛔ EVALUATE AT EVERY SECOND, NOT ONLY AT SECONDS WITH A VENUE ROW.
        # The venue record is patchy (~166 of 300 seconds), and `crossed_side`
        # needs a FRESH cross -- the newest tick on the opposite side of the
        # strike from the tick `look_s` earlier. A crossing that happens inside a
        # gap and persists is no longer fresh by the next recorded row, so
        # testing only at recorded seconds silently DROPS it. Measured: doing it
        # that way found 156 triggers in 2,240 windows (7%) where the arm's own
        # 1 Hz poll cadence finds ~17%. The arm polls every second, so poll every
        # second here and go and FETCH the book; that is what `book_age_ms`
        # models on the live side.
        sec_lo, sec_hi = int(300 - max_tau), int(300 - min_tau)
        hit = None
        k = 0
        for sec in range(sec_lo, sec_hi + 1):
            ts_ms = int(epoch) * 1000 + sec * 1000
            while k < len(ticks) and ticks[k][0] <= ts_ms:
                k += 1
            seen = ticks[:k]
            if len(seen) < 2:
                continue
            side = crossed_side(seen, strike, look_s)
            if side is None:
                continue
            want = ts_ms + entry_lag_ms
            j0 = int(np.searchsorted(pr_ts, ts_ms, side="left"))
            px_trig = None
            if j0 < len(pr_ts):
                v = pr_up[j0] if side == "Up" else pr_dn[j0]
                px_trig = float(v) if v is not None and float(v) > 0 else None
            j = int(np.searchsorted(pr_ts, want, side="left"))
            if j >= len(pr_ts):
                skipped["no_ask_after_lag"] += 1
                continue
            age = int(pr_ts[j]) - want
            if max_book_age_ms and age > max_book_age_ms:
                skipped["no_ask_after_lag"] += 1
                continue
            px = pr_up[j] if side == "Up" else pr_dn[j]
            if px is None or not (float(px) > 0):
                skipped["no_ask_after_lag"] += 1
                continue
            hit = (ts_ms, side, float(px), seen, age, px_trig)
            break
        if hit is None:
            skipped["no_trigger"] += 1
            continue

        ts_ms, side, px, seen, age, px_trig = hit
        if not (min_price <= px <= max_price) or px > max_fill_px:
            skipped["outside_band"] += 1
            continue
        spot_now = seen[-1][1]
        depth = 1e4 * abs(spot_now - strike) / strike
        if max_depth_bps and depth > max_depth_bps:
            skipped["too_deep"] += 1
            continue
        won = 1.0 if winner == side else 0.0
        # THE VENUE'S OWN STATISTIC. Settlement is the mean of the LAST 60 s, not
        # the close, so record the trailing 60 s mean at the trigger. Measured on
        # 1,102 settled rounds from the live DB: applying the venue's rule to OUR
        # tape reproduces its winner 93.1% of the time vs 89.7% for the close
        # (it wins 56 rounds the close gets wrong and loses 19).
        trail = [c for ts, c in seen if ts >= ts_ms - 60000]
        trail_gap = (1e4 * (sum(trail) / len(trail) - strike) / strike
                     if trail else float("nan"))
        rows.append({
            "fam": "btc-updown-5m", "slug": f"<epoch {epoch}>", "ts_ms": ts_ms,
            "side": side, "px": px, "px_trig": px_trig, "won": won,
            "rng": 1e4 * (max(c for _, c in seen) - min(c for _, c in seen)) / strike,
            "depth": depth, "age_ms": age, "fee": fee_per_share(px),
            "trail_gap": trail_gap, "tau": 300.0 - (ts_ms / 1000.0 - epoch),
            "day": time.strftime("%Y-%m-%d", time.gmtime(ts_ms / 1000)),
            "pnl": won - px - fee_per_share(px)})
    return rows, skipped | {"windows": windows}


def _ev(rows) -> float:
    return sum(r["pnl"] for r in rows) / len(rows) if rows else float("nan")


HDR = (f"  {'value':>7} {'n':>4} {'win':>6} {'be':>6} {'EV/sh':>8} {'t':>6} "
       f"{'day-t':>6} {'half1':>8} {'half2':>8} {'drop-best':>9}")


def line(v: float, rows: list) -> str:
    b = battery(rows)
    if not b or b["n"] < 8:
        return f"  {v:>7.2f} {(b or {}).get('n', 0):>4}   (too few)"
    return (f"  {v:>7.2f} {b['n']:>4} {100*b['win']:>5.1f}% {b['be']:>6.3f} "
            f"{b['ev']:>+8.4f} {b['t']:>+6.2f} {b['day_t']:>+6.2f} "
            f"{b['h1']:>+8.4f} {b['h2']:>+8.4f} {b['dropbest']:>+9.4f}")


def verdict(rows: list, values: list, keep) -> None:
    """Rank the values in EACH HALF -- and in each DAY -- and compare orderings.

    The live sweep's own rule (backtest-artifacts.md, 5th check): a parameter is
    real only if the ORDER repeats. With 8 days there is enough to ask it of the
    days as well as of the two halves, which the 3-day live window could not.
    """
    order = sorted(rows, key=lambda r: r["ts_ms"])
    half = len(order) // 2
    h1, h2 = order[:half], order[half:]
    e1 = {v: _ev(keep(h1, v)) for v in values}
    e2 = {v: _ev(keep(h2, v)) for v in values}
    b1 = max((v for v in values if e1[v] == e1[v]), key=lambda v: e1[v], default=None)
    b2 = max((v for v in values if e2[v] == e2[v]), key=lambda v: e2[v], default=None)
    print(f"    halves : best half1 = {b1} ({e1.get(b1, float('nan')):+.4f}) | "
          f"half2 = {b2} ({e2.get(b2, float('nan')):+.4f})  -> "
          f"{'SAME' if b1 == b2 else 'FLIPPED -- fitting'}")
    days = sorted({r["day"] for r in rows})
    wins = {v: 0 for v in values}
    for d in days:
        sub = [r for r in rows if r["day"] == d]
        if len(sub) < 8:
            continue
        ed = {v: _ev(keep(sub, v)) for v in values}
        ok = [v for v in values if ed[v] == ed[v]]
        if not ok:
            continue
        wins[max(ok, key=lambda v: ed[v])] += 1
    print(f"    days   : argmax per day over {len(days)} days -> {wins}")
    # is the DIRECTION monotone, not just the argmax?
    ks = [v for v in values if e1[v] == e1[v] and e2[v] == e2[v]]
    if len(ks) > 2:
        r1 = statistics.correlation(ks, [e1[v] for v in ks])
        r2 = statistics.correlation(ks, [e2[v] for v in ks])
        print(f"    rank corr of the grid across halves: {r1:+.3f} vs {r2:+.3f} "
              f"(agreement is what matters, not the level)")


CAP_GRID = [1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 99.0]


def run_cap(rows: list, lag: int) -> None:
    """Cap the fill at `k x` the ask seen AT THE TRIGGER.

    WHY THIS MEASUREMENT. The ceiling is `ask + cross_limit_buffer` -- an
    ABSOLUTE 0.25. That is regressive in exactly the wrong direction: on an ask
    of 0.10 it licenses 3.5x the price, on an ask of 0.50 only 1.5x. Live
    2026-09-17: `sol-updown-5m-1789649400` saw up_ask 0.100 and FILLED AT 0.200
    -- 2.0x the ask -- while its own `ev/$ gate->fill` read -0.5200.

    A capped order that cannot fill at the cap is treated as NO TRADE, not as a
    fill at the cap. Assuming the fill is the maker-path artefact in
    backtest-artifacts.md (a resting order that fills whenever the display
    touches it measured +0.17 at a 92% fill rate and -0.39 once the tape had to
    trade through). Dropping the trade is the conservative reading.
    """
    print(f"{'='*100}\nFILL CAP as a MULTIPLE OF THE TRIGGER ASK   n={len(rows)}   "
          f"entry lag {lag} ms   (a capped trigger is DROPPED, not repriced)\n{'='*100}")
    print(HDR)
    base = [r for r in rows if r["px"] == r["px"]]
    for k in CAP_GRID:
        kept = [r for r in base
                if r["px_trig"] and r["px"] <= r["px_trig"] * k + 1e-9]
        print(line(k, kept) + f"   dropped {len(base)-len(kept):3d}")
    print("  " + "-" * 98)
    verdict(rows, CAP_GRID,
            lambda pool, k: [r for r in pool
                             if r["px_trig"] and r["px"] <= r["px_trig"] * k + 1e-9])


DEPTH_GRID = [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0]


def run_depth(rows: list, lag: int) -> None:
    """Refuse entries whose crossing is shallower than `d` bps.

    WHY THIS IS THE MEASUREMENT THAT MATTERS. The trigger is `crossed_side` on
    the LOCAL tape; the bet settles on the VENUE's oracle. Those are different
    price series for the small coins, and the disagreement is concentrated where
    the two are closest -- AT the strike. Live 2026-09-17, two losses:

      sol-updown-5m-1789649400  resolved DOWN, our tape closed +2.98 bps ABOVE
      doge-updown-5m-1789649700  lost,          our tape closed -8.60 bps BELOW

    Both entries were 1-9 bps from the strike. This corpus is the honest test of
    the fix: the trigger comes from the Binance 1s tape, and the outcome comes
    from `metadata.json`'s `finalPrice`, which is the VENUE's own settlement. So
    a depth floor is scored against the oracle that actually pays.
    """
    print(f"{'='*100}\nMINIMUM CROSSING DEPTH   n={len(rows)}   entry lag {lag} ms\n{'='*100}")
    print("  buckets (no gate):")
    print(HDR)
    edges = [0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 1e9]
    for a, b in zip(edges, edges[1:]):
        cell = [r for r in rows if a <= r["depth"] < b]
        if cell:
            print(line(a, cell) + f"   <- {a:g}..{b:g} bps")
    print("\n  as a FLOOR:")
    print(HDR)
    for v in DEPTH_GRID:
        print(line(v, [r for r in rows if r["depth"] >= v]))
    print("  " + "-" * 98)
    verdict(rows, DEPTH_GRID, lambda pool, v: [r for r in pool if r["depth"] >= v])


CONFIRM_GRID = [-5.0, -2.0, -1.0, 0.0, 0.5, 1.0, 2.0]


def run_confirm(rows: list, lag: int) -> None:
    """Require the TRAILING 60 s MEAN to be on the crossing's side of the strike.

    WHY. The arm triggers on an INSTANTANEOUS crossing (`crossed_side` compares
    the newest tick with the tick `look_s` older) but Polymarket settles on the
    MEAN of the last 60 s of Chainlink. Measured on 1,102 settled live rounds:
    applying the venue's rule to our OWN tape reproduces its winner 93.1% vs
    89.7% for the close. So ~10% of crossings are reversed by the settlement
    RULE alone, before any oracle basis.

    At tau T the settlement window [T,0] is still partly in the FUTURE, so this
    cannot compute it. What it CAN test is the implementable version: require the
    mean of the tape we have ALREADY SEEN to agree with the side we are buying.
    The deeper into the window we trigger, the more of it is already observed.

    `v` is the minimum trail gap IN THE SIDE'S FAVOUR: v=0 means "the trailing
    mean must already be past the strike"; negative values allow it to lag.
    """
    print(f"{'='*100}\nTRAILING-60s-MEAN CONFIRMATION   n={len(rows)}   "
          f"entry lag {lag} ms\n{'='*100}")
    print(HDR)
    for v in CONFIRM_GRID:
        kept = [r for r in rows
                if r["trail_gap"] == r["trail_gap"]
                and (r["trail_gap"] if r["side"] == "Up" else -r["trail_gap"]) >= v]
        print(line(v, kept) + f"   dropped {len(rows)-len(kept):3d}   (trail in side's favour >= {v:g} bps)")
    print("  " + "-" * 98)
    verdict(rows, CONFIRM_GRID,
            lambda pool, v: [r for r in pool if r["trail_gap"] == r["trail_gap"]
                             and (r["trail_gap"] if r["side"] == "Up"
                                  else -r["trail_gap"]) >= v])


TAU_GRID = [60.0, 50.0, 40.0, 30.0, 20.0, 10.0, 5.0]


def run_tau(rows: list, lag: int) -> None:
    """Enter LATER in the round: refuse any crossing before tau `t`.

    WHY THIS IS MECHANICALLY THE RIGHT LEVER. The bet settles on the MEAN of the
    last 60 s, so at trigger time T the settlement window is the whole interval
    [T, 0] -- still entirely in the future. A shorter remaining window means the
    mean is closer to the spot we just crossed, so the crossing predicts it
    better. Measured live: the median entry tau in the 5-60 s window is ~43 s, so
    the arm is entering where that exposure is LARGEST.

    Costs trade count: fewer rounds still hold a fresh crossing late.
    """
    print(f"{'='*100}\nENTER LATER THAN tau `t`   n={len(rows)}   entry lag {lag} ms\n{'='*100}")
    print("  buckets (no gate):")
    print(HDR)
    edges = [60.0, 45.0, 30.0, 15.0, 5.0, -1.0]
    for a, b in zip(edges, edges[1:]):
        cell = [r for r in rows if b < r["tau"] <= a]
        if cell:
            print(line(a, cell) + f"   <- tau {b:g}..{a:g} s")
    print("\n  as a FLOOR (tau <= t):")
    print(HDR)
    for v in TAU_GRID:
        print(line(v, [r for r in rows if r["tau"] <= v]))
    print("  " + "-" * 98)
    verdict(rows, TAU_GRID, lambda pool, v: [r for r in pool if r["tau"] <= v])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/btc5m/data")
    ap.add_argument("--prices", default="data/btc5m_prices.parquet")
    ap.add_argument("--look-s", type=float, default=1.0)
    ap.add_argument("--min-tau", type=float, default=5.0)
    ap.add_argument("--max-tau", type=float, default=60.0)
    ap.add_argument("--min-price", type=float, default=0.10)
    ap.add_argument("--max-price", type=float, default=0.65)
    ap.add_argument("--max-fill-px", type=float, default=0.65)
    ap.add_argument("--max-depth-bps", type=float, default=8.0)
    ap.add_argument("--entry-lag-ms", type=int, default=500)
    ap.add_argument("--max-book-age-ms", type=int, default=2000)
    ap.add_argument("--sweep", default="both",
                    choices=("range", "floor", "cap", "depth", "confirm", "tau",
                             "both", "all"))
    a = ap.parse_args()

    rows, sk = load_corpus(a.root, a.prices, a.look_s, a.min_tau, a.max_tau,
                           a.min_price, a.max_price, a.max_fill_px,
                           a.max_depth_bps, a.entry_lag_ms, a.max_book_age_ms)
    print(f"corpus windows with venue prices: {sk['windows']}")
    print(f"  dropped: { {k: v for k, v in sk.items() if k != 'windows' and v} }")
    print(f"ONE TRIGGER PER ROUND: {len(rows)}   days={len({r['day'] for r in rows})}"
          f"   entry lag {a.entry_lag_ms} ms   shipped gate "
          f"{20.0:.1f} bps\n")
    n = len(rows)
    if not n:
        return
    print(HDR)
    keep = lambda pool, v: [r for r in pool if r["rng"] <= v]
    if a.sweep in ("range", "both"):
        for v in RANGE_GRID:
            print(line(v, keep(rows, v)))
        print("  " + "-" * 98)
        verdict(rows, RANGE_GRID, keep)
    if a.sweep in ("floor", "both", "all"):
        kf = lambda pool, v: [r for r in pool if r["px"] >= v]
        print(f"\nBAND FLOOR:\n{HDR}")
        for v in FLOOR_GRID:
            print(line(v, kf(rows, v)))
        print("  " + "-" * 98)
        verdict(rows, FLOOR_GRID, kf)
    if a.sweep in ("cap", "all"):
        run_cap(rows, a.entry_lag_ms)
    if a.sweep in ("depth", "all"):
        run_depth(rows, a.entry_lag_ms)
    if a.sweep in ("confirm", "all"):
        run_confirm(rows, a.entry_lag_ms)
    if a.sweep in ("tau", "all"):
        run_tau(rows, a.entry_lag_ms)


if __name__ == "__main__":
    main()
