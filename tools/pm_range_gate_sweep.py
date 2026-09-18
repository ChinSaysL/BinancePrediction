"""Is the per-family round-range gate REAL, or five numbers that fit?

WHY THIS EXISTS. `cross_overrides` ships five per-family `max_round_range_bps`
values -- eth-5m 12.9, bnb-5m 7.8, btc-15m 13.6, eth-15m 21.0, bnb-15m 21.1 --
and every one is a 0.1-resolution grid point, i.e. the argument of a sweep. A
2.7x spread across families whose only difference is the UNDERLYING is the shape
noise makes, and this repo's own rule (backtest-artifacts.md, 5th check) is that
a parameter must RANK THE SAME IN BOTH HALVES or it is fitting. The rule's own
worked example is exactly this shape: `0.90 -> 0.80` repeated at +0.0104 in both
halves (real) while `0.80 vs 0.75 vs 0.70` flipped between them (noise).

The second question is the BAND FLOOR, `cross_min_price = 0.10`. Measured
2026-09-17 on this same PM record: crossings priced 0.00-0.50 won 16.9% (EV
-0.0445) while 0.50-0.60 won 59.2% (+0.0195). If that repeats here the floor
belongs near 0.50, not 0.10.

NOTHING HERE IS A NEW TRIGGER. It is the bot's own `crossed_side` (imported, not
reimplemented) and the bot's own gate definition, reproduced from the recorded
quotes:

    range = 1e4 * (max(spot) - min(spot)) / strike, over [round open, trigger]

which is `Bot._round_range_bps` verbatim (`self._ticks` is one sample per poll,
so the recorded 1 Hz `quotes` reproduce it).

RULES APPLIED, all of them earned (see backtest-artifacts.md):
  * ONE ENTRY PER ROUND -- the first crossing in the window. Row-level inflation
    is the #1 way a losing strategy looks profitable here.
  * the battery: pooled t, day-clustered t, both halves, drop-best-day.
  * the noise ceiling. With ~15 values on the grid, E[max|z|] ~ sqrt(2 ln 15) =
    2.33, so a max |t| below that is not a finding.
  * NEVER quote the exact optimum. What matters is whether the ordering repeats.

Usage (on the VPS):
    cd /root/BinancePrediction
    PYTHONPATH=. /root/analysis-venv/bin/python tools/pm_range_gate_sweep.py
    ... --entry-lag-ms 500      # price the fill half a second later
"""
from __future__ import annotations

import argparse
import math
import pathlib
import sqlite3
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from btcpredict.main import crossed_side           # noqa: E402
from pm_live_band_sweep import battery, fee_per_share   # noqa: E402

RANGE_GRID = [4, 6, 8, 10, 12, 13, 14, 16, 18, 20, 22, 26, 99]
FLOOR_GRID = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]


def load(db: str, look_s: float, max_book_age_ms: int, entry_lag_ms: int,
         min_tau: float = 5.0, max_tau: float = 60.0, max_ask: float = 0.99):
    """One TRIGGER per PM round, with the round-range the gate decided on."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rounds = {r["slug"]: r for r in con.execute(
        "SELECT slug, strike, winner, start_ms FROM rounds "
        "WHERE winner IS NOT NULL AND strike IS NOT NULL AND strike > 0 "
        "AND condition_id IS NOT NULL AND condition_id <> ''")}
    by_slug: dict[str, list] = {}
    for q in con.execute(
            "SELECT slug, ts_ms, tau_s, spot, up_ask, down_ask, book_age_ms "
            "FROM quotes ORDER BY slug, ts_ms"):
        if q["slug"] in rounds:
            by_slug.setdefault(q["slug"], []).append(q)
    out = []
    for slug, rows in by_slug.items():
        r = rounds[slug]
        ticks: list = []
        hit = None
        for i, q in enumerate(rows):
            if q["spot"] and q["spot"] > 0:
                ticks.append((q["ts_ms"], q["spot"]))
            if hit is not None:
                continue
            if not (min_tau <= (q["tau_s"] or 0) <= max_tau):
                continue
            age = q["book_age_ms"]
            if max_book_age_ms and (age is None or age > max_book_age_ms):
                continue
            side = crossed_side(ticks, r["strike"], look_s)
            if side is not None:
                hit = (i, side)
        if hit is None:
            continue
        i, side = hit
        trig_ts = rows[i]["ts_ms"]
        want = trig_ts + entry_lag_ms
        pick = None
        for q in rows[i:]:
            if q["ts_ms"] >= want and (q["tau_s"] or 0) > 0:
                pick = q
                break
        if pick is None:
            continue
        age = pick["book_age_ms"]
        if max_book_age_ms and (age is None or age > max_book_age_ms):
            continue
        px = pick["up_ask"] if side == "Up" else pick["down_ask"]
        if not px or px <= 0 or px > max_ask:
            continue
        # THE GATE'S OWN NUMBER: range over [round open, trigger].
        seen = [s for t_ms, s in ticks if t_ms >= r["start_ms"] and t_ms <= trig_ts]
        if len(seen) < 2:
            continue                      # `_round_range_bps` -> None, no opinion
        won = 1.0 if r["winner"] == side else 0.0
        out.append({"fam": "-".join(slug.split("-")[:3]), "slug": slug,
                    "ts_ms": trig_ts, "side": side, "px": float(px),
                    "won": won, "rng": 1e4 * (max(seen) - min(seen)) / r["strike"],
                    "day": time.strftime("%Y-%m-%d", time.gmtime(trig_ts / 1000)),
                    "pnl": won - float(px) - fee_per_share(float(px))})
    return out


def _ev(rows) -> float:
    return sum(r["pnl"] for r in rows) / len(rows) if rows else float("nan")


def line(v: float, rows: list) -> str:
    b = battery(rows)
    if not b or b["n"] < 8:
        return f"  {v:>7.2f} {b.get('n', 0) if b else 0:>4}   (too few)"
    return (f"  {v:>7.2f} {b['n']:>4} {100*b['win']:>5.1f}% {b['be']:>6.3f} "
            f"{b['ev']:>+8.4f} {b['t']:>+6.2f} {b['day_t']:>+6.2f} "
            f"{b['h1']:>+8.4f} {b['h2']:>+8.4f} {b['dropbest']:>+9.4f}")


HDR = (f"  {'value':>7} {'n':>4} {'win':>6} {'be':>6} {'EV/sh':>8} {'t':>6} "
       f"{'day-t':>6} {'half1':>8} {'half2':>8} {'drop-best':>9}")


def verdict(name: str, rows: list, values: list, keep) -> None:
    """Rank the values in EACH HALF separately and compare the ordering."""
    order = sorted(rows, key=lambda r: r["ts_ms"])
    half = len(order) // 2
    h1, h2 = order[:half], order[half:]
    e1 = {v: _ev(keep(rows, v, h1)) for v in values}
    e2 = {v: _ev(keep(rows, v, h2)) for v in values}
    b1 = max((v for v in values if e1[v] == e1[v]), key=lambda v: e1[v], default=None)
    b2 = max((v for v in values if e2[v] == e2[v]), key=lambda v: e2[v], default=None)
    same = abs(e1.get(b1, float("nan")) - e2.get(b2, float("nan"))) < 1e9 and b1 == b2
    print(f"    {name}: best in half1 = {b1}  ({e1.get(b1, float('nan')):+.4f}), "
          f"half2 = {b2}  ({e2.get(b2, float('nan')):+.4f})  -> "
          f"{'SAME' if same else 'FLIPPED -- fitting'}")


def run_range(rows: list, only: str | None, lag: int) -> None:
    fams = sorted({r["fam"] for r in rows})
    for fam in fams:
        if only and only not in fam:
            continue
        sub = [r for r in rows if r["fam"] == fam]
        if len(sub) < 20:
            print(f"\n{fam}: n={len(sub)} -- too few to rank")
            continue
        cur = {"eth-updown-5m": 12.9, "bnb-updown-5m": 7.8,
               "btc-updown-15m": 13.6, "eth-updown-15m": 21.0,
               "bnb-updown-15m": 21.1}.get(fam, 20.0)
        print(f"\n{fam}   n={len(sub)} triggers   shipped gate {cur} bps   "
              f"(entry lag {lag} ms)")
        print(HDR)
        keep = lambda rs, v, pool=None: [r for r in (pool if pool is not None else rs)
                                         if r["rng"] <= v]
        for v in RANGE_GRID:
            print(line(v, keep(sub, v)))
        verdict(fam, sub, RANGE_GRID, keep)


def run_floor(rows: list, lag: int) -> None:
    print(f"\n{'='*100}\nBAND FLOOR sweep (all families, one entry per round)   "
          f"n={len(rows)}   entry lag {lag} ms\n{'='*100}")
    print(HDR)
    keep = lambda rs, v, pool=None: [r for r in (pool if pool is not None else rs)
                                     if r["px"] >= v]
    for v in FLOOR_GRID:
        print(line(v, keep(rows, v)))
    verdict("floor", rows, FLOOR_GRID, keep)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/root/BinancePrediction/data/btcpredict.db")
    ap.add_argument("--look-s", type=float, default=1.0)
    ap.add_argument("--max-book-age-ms", type=int, default=2000)
    ap.add_argument("--entry-lag-ms", type=int, default=0)
    ap.add_argument("--family")
    ap.add_argument("--sweep", default="both", choices=("range", "floor", "both"))
    a = ap.parse_args()
    rows = load(a.db, a.look_s, a.max_book_age_ms, a.entry_lag_ms)
    print(f"one trigger per PM round: {len(rows)} rounds   "
          f"families: {len({r['fam'] for r in rows})}   "
          f"days: {len({r['day'] for r in rows})}")
    if a.sweep in ("range", "both"):
        run_range(rows, a.family, a.entry_lag_ms)
    if a.sweep in ("floor", "both"):
        run_floor(rows, a.entry_lag_ms)


if __name__ == "__main__":
    main()
