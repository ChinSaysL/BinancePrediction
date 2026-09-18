"""Does the 80%-win-rate entry config still hold? Replayed on the PM record.

THE CLAIM. `FINDINGS.md` records a tuned entry config -- candle-open gate,
`min_edge=0.05`, `min_quote=0.25`, `max_quote=0.90`, `signal_persistence=4`,
`candle_open_window_s=10` -- at **23 trades, WR 0.783, +$6.10 (+26.5%)**, and
`min_edge=0.08` at **WR 0.88**. That is the "big gains with 80% win rate" result.

WHAT THE SAME DOC ALREADY SAYS ABOUT IT, twice (both must be re-checked, not
taken on trust):
  1. FEE. "Backtests had assumed a flat 64 bps. That is ~3x too low for cheap
     contracts and ~3x too high for favourites." Corrected, win rate went UP
     (63.4% -> 69.4%) while per-trade collapsed (+$0.34 -> +$0.023) and
     P(profitable) fell 96.1% -> 58.5%. "The apparent edge was ~93% an artefact
     of the flat-fee assumption."
  2. HELD OUT. "the first 13 trades lose -$0.72 and the last 13 make +$9.70 --
     every other period is unprofitable, and 3 trades carry 75% of the profit.
     The second half is NOT held out: the gate parameters were swept on the full
     recording... The fair label is hypothesis with encouraging support, not an
     edge."

WHY THIS TOOL. Both of those were measured on the OLD venue, on a ~76-round
recording, with no public book. The bot's DB now holds **2203 resolved PM rounds
and 282k model decisions** with the model's own `edge`/`quote` per poll, so the
gate can be replayed by re-filtering rather than recomputing -- and split
CHRONOLOGICALLY, which is the check that was never done.

THE FEE IS THE PM FEE, VERIFIED ON LIVE FILLS, NOT ASSUMED:
    `fee = 0.07 * p * (1-p)` per share, checked against three real fills:
    px 0.26 x 3.8462 sh -> 0.0518 (formula 0.05180) | px 0.22 x 4.5455 -> 0.0546
    (0.05460) | px 0.76 x 1.3158 -> 0.0168 (formula 0.01680). Exact.
    (The `180 * min(1,(1-px)/px)` curve in FINDINGS is the OLD venue's.)

Usage:
    cd /root/BinancePrediction
    PYTHONPATH=. /root/analysis-venv/bin/python tools/pm_entry_gate_replay.py
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import time

FEE_RATE = 0.07


def fee_per_share(p: float) -> float:
    return FEE_RATE * p * (1.0 - p)


def _t(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return 0.0 if var <= 0 else m / math.sqrt(var / n)


def report(label: str, rows: list[dict], holdout_ms: int | None = None) -> None:
    if not rows:
        print("  %-28s no entries" % label)
        return
    rows = sorted(rows, key=lambda r: r["ts_ms"])
    pnl = [r["pnl"] for r in rows]
    by_day: dict[str, list[float]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r["pnl"])
    win = sum(1 for r in rows if r["won"]) / len(rows)
    paid = sum(r["px"] for r in rows) / len(rows)
    half = len(rows) // 2
    # THE HELD-OUT SPLIT, at a DATE boundary rather than a row count: a row
    # split can put two polls of the SAME round on opposite sides.
    ho = [r for r in rows if holdout_ms and r["ts_ms"] >= holdout_ms]
    ins = [r for r in rows if not holdout_ms or r["ts_ms"] < holdout_ms]
    line = ("  %-28s n=%3d win %5.1f%% paid %5.3f EV/sh %+.4f t %+5.2f "
            "day-t %+5.2f days %2d"
            % (label, len(rows), win * 100, paid, sum(pnl) / len(pnl), _t(pnl),
               _t([sum(v) / len(v) for v in by_day.values()]), len(by_day)))
    print(line)
    if holdout_ms:
        for nm, sub in (("    in-sample  ", ins), ("    HELD OUT   ", ho)):
            if len(sub) < 2:
                print("%s n=%d (too few)" % (nm, len(sub)))
                continue
            w = sum(1 for r in sub if r["won"]) / len(sub)
            p = sum(r["pnl"] for r in sub) / len(sub)
            print("%s n=%3d win %5.1f%% paid %5.3f EV/sh %+.4f t %+5.2f"
                  % (nm, len(sub), w * 100, sum(r["px"] for r in sub) / len(sub),
                     p, _t([r["pnl"] for r in sub])))
    print("    halves EV/sh: %+.4f / %+.4f"
          % (sum(r["pnl"] for r in rows[:half]) / max(1, half),
             sum(r["pnl"] for r in rows[half:]) / max(1, len(rows) - half)))


def load(db: str):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rounds = {r["slug"]: r for r in con.execute(
        "SELECT slug, winner FROM rounds WHERE winner IS NOT NULL "
        "AND condition_id IS NOT NULL AND condition_id <> ''")}
    per: dict[str, list] = {}
    for d in con.execute(
            "SELECT d.slug, d.ts_ms, d.tau_s, d.side, d.quote, d.edge, d.hurdle "
            "FROM decisions d JOIN rounds r ON r.slug = d.slug "
            "WHERE r.winner IS NOT NULL AND r.condition_id IS NOT NULL "
            "AND r.condition_id <> '' AND d.side IN ('Up','Down') "
            "AND d.quote IS NOT NULL ORDER BY d.slug, d.ts_ms"):
        if d["slug"] in rounds:
            per.setdefault(d["slug"], []).append(d)
    return rounds, per


def replay(rounds, per, *, min_edge, lo, hi, min_tau, max_tau, persistence,
           use_hurdle=True, candle_open=False, candle_window_s=10.0,
           candle_opens=(1, 2, 3, 4)):
    """One entry per round: first poll where the signal has PERSISTED.

    THE ACT CONDITION IS THE BOT'S OWN, from `edge/signal.py:185`:
        `act = ev > min_ev and edge > hurdle and edge >= min_edge`
    `edge` and `hurdle` are both stored per poll, so the gate is re-applied
    rather than recomputed. `ev` is not stored but `ev_per_dollar == edge/quote`,
    so `ev > 0` is `edge > 0` for a positive quote.

    ⛔ THE STREAK RESTARTS AT EVERY CANDLE OPEN (`main.py`: `candle_idx =
    now_ms // 60_000`), because "each 1m candle is a fresh prediction problem".
    The first version of this tool ran the streak across the whole round, which
    hands a 4-poll streak to polls that live would have reset -- it inflates the
    persistence-4 config. Corrected here.

    THE CANDLE GATE (`trade_on_candle_open`) blocks a poll unless it is inside
    the first `candle_window_s` of a minute AND `round(tau/60)` is a selected
    candle; it also lets `min_tau` reach back by the window, since the final
    candle opens at tau ~59.9s just under a 60s floor.
    """
    out = []
    for slug, rows in per.items():
        streak, last, cur_candle = 0, None, None
        entry = None
        for d in rows:
            now = d["ts_ms"]
            ci = now // 60_000
            if ci != cur_candle:
                cur_candle = ci
                streak, last = 0, None
            tau = d["tau_s"] or 0.0
            sec = (now // 1000) % 60
            floor = min_tau - candle_window_s if candle_open else min_tau
            ok = (d["edge"] is not None and d["edge"] >= min_edge
                  and lo <= d["quote"] <= hi and floor <= tau <= max_tau)
            if ok and use_hurdle:
                ok = d["hurdle"] is not None and d["edge"] > d["hurdle"]
            if ok and candle_open:
                ok = (sec < candle_window_s
                      and int(round(tau / 60.0)) in candle_opens)
            if not ok:
                streak, last = 0, None
                continue
            if d["side"] == last:
                streak += 1
            else:
                last, streak = d["side"], 1
            if streak >= persistence:
                entry = d
                break
        if entry is None:
            continue
        px = float(entry["quote"])
        won = 1.0 if rounds[slug]["winner"] == entry["side"] else 0.0
        out.append({"pnl": won - px - fee_per_share(px), "won": won, "px": px,
                    "ts_ms": entry["ts_ms"],
                    "day": time.strftime("%Y-%m-%d",
                                         time.gmtime(entry["ts_ms"] / 1000))})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", default="2026-09-13",
                    help="UTC date the held-out period starts (YYYY-MM-DD)")
    ap.add_argument("--db", default="/root/BinancePrediction/data/btcpredict.db")
    a = ap.parse_args()
    rounds, per = load(a.db)
    print("PM rounds with model decisions: %d" % len(per))
    ho_ms = int(time.mktime(time.strptime(a.holdout, "%Y-%m-%d"))) * 1000
    print("held-out period starts %s (UTC)\n" % a.holdout)

    base = dict(min_tau=5, max_tau=240)
    tuned = dict(min_edge=0.05, lo=0.25, hi=0.90, persistence=4, **base)
    live = dict(min_edge=0.01, lo=0.45, hi=0.80, persistence=2, **base)

    print("=== 1. THE GATES I OMITTED, on the TUNED config ===")
    for label, kw in (
            ("+.hurdle", dict(use_hurdle=True, candle_open=False)),
            ("+.candle", dict(use_hurdle=False, candle_open=True)),
            ("+.hurdle+candle", dict(use_hurdle=True, candle_open=True)),
    ):
        report("tuned e=.05 p=4 " + label,
               replay(rounds, per, **tuned, **kw), ho_ms)

    print("\n=== 2. SAME, on the config as SHIPPED in .env ===")
    for label, kw in (
            ("no gates", dict(use_hurdle=False, candle_open=False)),
            ("+.hurdle", dict(use_hurdle=True, candle_open=False)),
            ("+.candle", dict(use_hurdle=False, candle_open=True)),
            ("+.hurdle+candle", dict(use_hurdle=True, candle_open=True)),
    ):
        report("live e=.01 p=2 " + label,
               replay(rounds, per, **live, **kw), ho_ms)

    print("\n=== 3. does min_edge RANK the same in the held-out period "
          "(with BOTH gates on)? ===")
    for e in (0.02, 0.03, 0.05, 0.08, 0.10):
        report("e=%.2f p=4 +hurdle+candle" % e,
               replay(rounds, per, min_edge=e, lo=0.25, hi=0.90, persistence=4,
                      **base, use_hurdle=True, candle_open=True), ho_ms)

    print("\n=== 4. does persistence RANK the same in the held-out period? ===")
    for p in (1, 2, 3, 4, 6):
        report("p=%d e=.05 +hurdle+candle" % p,
               replay(rounds, per, min_edge=0.05, lo=0.25, hi=0.90,
                      persistence=p, **base, use_hurdle=True,
                      candle_open=True), ho_ms)


if __name__ == "__main__":
    main()
