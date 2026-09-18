"""Which CROSS band actually wins, on the LIVE Polymarket record?

WHY THIS EXISTS. `pm_cross_sweep.py` (360 cells) and `pm_cross_backtest.py` both
run on the 2026-07-17 kartik tape -- ONE day, ~1 s L2, which `pm-cross-arm-
measured.md` calls a HARD LIMIT: "no parameter can be shipped from this". The
53-day `data/btc5m` tape retired that limit for the TRIGGER, but its prices are
trade prints, not the quotes we would have paid.

The bot's own DB now holds **7,231 Polymarket rounds with recorded books** -- real
`up_ask`/`down_ask` at real timestamps on the REAL venue, across ~11 days. That is
the first sample where "which entry band" can be answered against the price we
would actually have paid, with a day-clustered test.

THE QUESTION THE OPERATOR ASKED is "the highest win rate". It is the wrong
objective and the point of the table below is to show WHY: win rate is monotone
INCREASING in the price paid while EV is monotone DECREASING, because the market
prices the crossing too. A 90% win rate at a 88% break-even is worth less than a
56% win rate at a 46% break-even, and the second one is the trade nobody wants to
take.

RULES APPLIED (all of them earned, see backtest-artifacts.md):
  * the trigger is the bot's OWN `crossed_side`, imported not copied -- a
    reimplementation that disagreed would measure the reimplementation
  * ONE ENTRY PER ROUND. Row-level inflation is the #1 way a losing strategy
    looks profitable here (n=407 t 2.77 -> n=122 t 1.12).
  * the four-check battery: pooled t, day-clustered t, both halves, drop-best-day
  * a band is only interesting if it survives ALL of them. With ~10 bands tested
    the noise ceiling is E[max|z|] ~ sqrt(2 ln 10) = 2.15, so quote the max |t|.

Usage (on the VPS):
    cd /root/BinancePrediction
    PYTHONPATH=. /root/analysis-venv/bin/python tools/pm_live_band_sweep.py
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import time

from btcpredict.main import crossed_side

FEE_RATE = 0.07          # taker fee is 0.07 * p * (1-p) per share


def fee_per_share(p: float) -> float:
    return FEE_RATE * p * (1.0 - p)


def _t(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return 0.0
    return m / math.sqrt(var / n)


def battery(rows: list[dict]) -> dict:
    """The four checks. `rows` = one entry per round."""
    if not rows:
        return {}
    pnl = [r["pnl"] for r in rows]
    by_day: dict[str, list[float]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r["pnl"])
    day_means = [sum(v) / len(v) for v in by_day.values()]
    order = sorted(rows, key=lambda r: r["ts_ms"])
    half = len(order) // 2
    win = sum(1 for r in rows if r["won"]) / len(rows)
    paid = sum(r["px"] for r in rows) / len(rows)
    best = max(by_day, key=lambda d: sum(by_day[d]) / len(by_day[d]))
    rest = [r for r in rows if r["day"] != best]
    rest_days: dict[str, list[float]] = {}
    for r in rest:
        rest_days.setdefault(r["day"], []).append(r["pnl"])
    return {
        "n": len(rows), "win": win, "be": paid, "ev": sum(pnl) / len(pnl),
        "t": _t(pnl), "day_t": _t(day_means), "days": len(by_day),
        "h1": sum(r["pnl"] for r in order[:half]) / max(1, half),
        "h2": sum(r["pnl"] for r in order[half:]) / max(1, len(order) - half),
        "dropbest": (sum(r["pnl"] for r in rest) / len(rest)) if rest else 0.0,
    }


def load(db: str, look_s: float, min_tau: float, max_tau: float,
         max_book_age_ms: int, max_ask: float, entry_lag_ms: int = 0):
    """One TRIGGER attempt per PM round: (round facts, entry side, entry ask).

    ⛔ THE ENTRY PRICE IS TAKEN `entry_lag_ms` AFTER THE TRIGGER, AND THAT IS THE
    WHOLE EXPERIMENT. Pricing the fill at the trigger instant is the stale-price
    artefact this arm has already been caught on twice: the venue's own ack is
    260-1237 ms, so the price we pay is the price `lag` later, and
    `pm-cross-arm-measured.md` measured the book moving **+0.115 median within
    100 ms of our own fire** (p90 +0.50). An arm whose EV decays monotonically
    with this lag is pricing staleness, not edge -- so read the decay across
    0 / 200 / 400 / 1000 / 2000 / 5000, never the 0 ms number alone.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rounds = {r["slug"]: r for r in con.execute(
        "SELECT slug, strike, winner, start_ms, end_ms FROM rounds "
        "WHERE winner IS NOT NULL AND strike IS NOT NULL AND strike > 0 "
        "AND condition_id IS NOT NULL AND condition_id <> ''")}
    rows_by_slug: dict[str, list] = {}
    for q in con.execute(
            "SELECT slug, ts_ms, tau_s, spot, up_ask, down_ask, book_age_ms "
            "FROM quotes WHERE slug IN (SELECT slug FROM rounds WHERE winner "
            "IS NOT NULL AND condition_id IS NOT NULL AND condition_id <> '') "
            "ORDER BY slug, ts_ms"):
        if q["slug"] in rounds:
            rows_by_slug.setdefault(q["slug"], []).append(q)
    out = []
    for slug, rows in rows_by_slug.items():
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
            if side is None:
                continue
            hit = (i, side)
        if hit is None:
            continue
        i, side = hit
        trig_ts = rows[i]["ts_ms"]
        want = trig_ts + entry_lag_ms
        pick = None
        for q in rows[i:]:
            if q["ts_ms"] >= want:
                if q["tau_s"] is not None and q["tau_s"] <= 0:
                    break
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
        out.append({"slug": slug, "ts_ms": trig_ts, "side": side,
                    "px": float(px), "tau": rows[i]["tau_s"],
                    "winner": r["winner"], "age": age,
                    "fill_lag_ms": pick["ts_ms"] - trig_ts})
    return out


def band_table(entries: list[dict], edges: list[float]) -> None:
    print("  band         n   win     be      EV/sh    t      day-t  days"
          "   half1    half2    drop-best")
    for lo, hi in zip(edges, edges[1:]):
        rows = []
        for e in entries:
            if not (lo <= e["px"] < hi if hi < 1.0 else lo <= e["px"] <= hi):
                continue
            px = e["px"]
            won = 1.0 if e["winner"] == e["side"] else 0.0
            rows.append({"pnl": won - px - fee_per_share(px), "won": won,
                         "px": px, "ts_ms": e["ts_ms"],
                         "day": time.strftime("%Y-%m-%d",
                                              time.gmtime(e["ts_ms"] / 1000))})
        b = battery(rows)
        if not b or b["n"] < 15:
            print("  %.2f-%.2f %4d   (n too small)" % (lo, hi, b.get("n", 0)))
            continue
        print("  %.2f-%.2f %4d  %5.1f%%  %5.1f%%  %+7.4f  %+5.2f  %+5.2f  %3d"
              "  %+7.4f %+7.4f  %+7.4f"
              % (lo, hi, b["n"], b["win"] * 100, b["be"] * 100, b["ev"],
                 b["t"], b["day_t"], b["days"], b["h1"], b["h2"],
                 b["dropbest"]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/root/BinancePrediction/data/btcpredict.db")
    ap.add_argument("--look-s", type=float, default=1.0)
    ap.add_argument("--min-tau", type=float, default=5.0)
    ap.add_argument("--max-tau", type=float, default=60.0)
    ap.add_argument("--max-book-age-ms", type=int, default=2000)
    ap.add_argument("--max-ask", type=float, default=0.99)
    ap.add_argument("--entry-lag-ms", type=int, default=0,
                    help="price the fill this long AFTER the trigger; see load()")
    ap.add_argument("--lag-sweep", action="store_true")
    ap.add_argument("--tau-grid", action="store_true")
    a = ap.parse_args()

    if a.lag_sweep:
        print("ENTRY-LAG SWEEP (the decisive test): one entry/round, tau [%g,%g],"
              "  look %.1fs" % (a.min_tau, a.max_tau, a.look_s))
        print("  lag      n   win     be      EV/sh    t      day-t  med-fill"
              "  h1       h2")
        for lag in (0, 200, 400, 600, 1000, 2000, 5000):
            es = load(a.db, a.look_s, a.min_tau, a.max_tau, a.max_book_age_ms,
                      a.max_ask, lag)
            if not es:
                continue
            rows = [{"pnl": (1.0 if e["winner"] == e["side"] else 0.0) - e["px"]
                     - fee_per_share(e["px"]),
                     "won": 1.0 if e["winner"] == e["side"] else 0.0,
                     "px": e["px"], "ts_ms": e["ts_ms"],
                     "day": time.strftime("%Y-%m-%d",
                                          time.gmtime(e["ts_ms"] / 1000))}
                    for e in es]
            b = battery(rows)
            med = sorted(e["fill_lag_ms"] for e in es)[len(es) // 2]
            print("  %5d %5d  %5.1f%%  %5.1f%%  %+7.4f  %+5.2f  %+5.2f  %6d  "
                  "%+7.4f %+7.4f"
                  % (lag, b["n"], b["win"] * 100, b["be"] * 100, b["ev"],
                     b["t"], b["day_t"], med, b["h1"], b["h2"]))
        return

    entries = load(a.db, a.look_s, a.min_tau, a.max_tau, a.max_book_age_ms,
                   a.max_ask, a.entry_lag_ms)
    print("PM triggers (one per round), tau [%g,%g], look %.1fs, book age <= %dms,"
          " entry lag %dms"
          % (a.min_tau, a.max_tau, a.look_s, a.max_book_age_ms, a.entry_lag_ms))
    print("  entries: %d" % len(entries))
    if not entries:
        return
    days: dict[str, int] = {}
    for e in entries:
        d = time.strftime("%Y-%m-%d", time.gmtime(e["ts_ms"] / 1000))
        days[d] = days.get(d, 0) + 1
    print("  by day: " + "  ".join("%s=%d" % kv for kv in sorted(days.items())))
    print("  median fill lag %dms"
          % sorted(e["fill_lag_ms"] for e in entries)[len(entries) // 2])
    b = battery([{"pnl": (1.0 if e["winner"] == e["side"] else 0.0) - e["px"]
                  - fee_per_share(e["px"]),
                  "won": 1.0 if e["winner"] == e["side"] else 0.0, "px": e["px"],
                  "ts_ms": e["ts_ms"],
                  "day": time.strftime("%Y-%m-%d", time.gmtime(e["ts_ms"] / 1000))}
                 for e in entries])
    print("  ALL  n=%d win %.1f%% be %.1f%% EV/sh %+.4f t %+.2f day-t %+.2f "
          "days %d | h1 %+.4f h2 %+.4f drop-best %+.4f"
          % (b["n"], b["win"] * 100, b["be"] * 100, b["ev"], b["t"], b["day_t"],
             b["days"], b["h1"], b["h2"], b["dropbest"]))
    print()
    edges = [0.0, 0.10, 0.25, 0.35, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75,
             0.80, 0.85, 0.90, 1.0]
    band_table(entries, edges)


if __name__ == "__main__":
    main()
