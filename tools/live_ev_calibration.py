"""Is the edge we PREDICT at the fill the edge we GET?

`fills.ev_at_fill` is the arm's own estimate of the edge at the moment it bought.
`settlements.pnl` is what actually happened. Every gate in the live arm (`min_edge`,
`hurdle`, `ev > N`) is set from BACKTEST EV; if the live realised EV does not
track the predicted EV, those gates are set on a number that does not exist live,
and the fix is to move the gate to where realised EV turns positive -- or to stop
the arm.

This is that comparison, with the battery this repo uses to keep itself honest:
pooled t, day-clustered t (adjacent 5-minute rounds are not independent), both
halves, and drop-the-best-day.

It reports, per bucket of PREDICTED EV:
  n, mean predicted EV, mean realised EV, the gap, the win rate, and the same
four statistics on the realised series.

Usage:  python -m tools.live_ev_calibration [--db data/btcpredict.db]
        [--family all|main|impulse|pair|ticket] [--buckets 5]
"""
from __future__ import annotations

import argparse
import collections
import sqlite3
import statistics as st
from pathlib import Path

FAMILIES = {
    "all": "1=1",
    "model": "d.reason = 'ok'",
    "impulse": "d.reason LIKE 'impulse %'",
    "pair": "d.reason LIKE 'pair %'",
    "ticket": "d.reason LIKE 'ticket %'",
    "main": "d.reason NOT LIKE 'ticket %'",
}

MACRO = ("SELECT COUNT(*), COALESCE(SUM(s.pnl), 0), "
         "SUM(CASE WHEN s.won THEN 1 ELSE 0 END), "
         "COALESCE(SUM(f.stake), 0) "
         "FROM fills f JOIN settlements s ON s.fill_id = f.id "
         "JOIN decisions d ON d.id = f.decision_id "
         "WHERE COALESCE(s.void, 0) = 0 AND (%s)")


def battery(vals: list[float], days: list[int]) -> dict:
    """The four statistics that have killed every claim that did not hold."""
    n = len(vals)
    if n == 0:
        return {}
    m = sum(vals) / n
    sd = st.pstdev(vals) if n > 1 else 0.0
    pooled_t = m / (sd / n ** 0.5) if sd else 0.0
    byday: dict[int, list[float]] = collections.defaultdict(list)
    for d, v in zip(days, vals):
        byday[d].append(v)
    dm = [sum(v) / len(v) for v in byday.values()]
    mdd = sum(dm) / len(dm)
    sdd = st.pstdev(dm) if len(dm) > 1 else 0.0
    daily_t = mdd / (sdd / len(dm) ** 0.5) if sdd else 0.0
    half = n // 2
    h1 = sum(vals[:half]) / max(1, half)
    h2 = sum(vals[half:]) / max(1, n - half)
    drop = dm[:-1] if len(dm) > 1 else []
    return {"n": n, "mean": m, "pooled_t": pooled_t, "daily_t": daily_t,
            "h1": h1, "h2": h2, "drop1": (sum(drop) / len(drop)) if drop else m,
            "days": len(byday)}


def line(label: str, ev: list[float], pred: list[float], days: list[int],
         wins: list[int], show_pred: bool = True) -> None:
    b = battery(ev, days)
    if not b:
        print(f"  {label:<20} (empty)")
        return
    wr = 100.0 * sum(wins) / b["n"]
    head = f"  {label:<20} n={b['n']:<5}"
    if show_pred:
        mp = sum(pred) / len(pred)
        head += (f" pred {mp:+7.4f}  real {b['mean']:+7.4f}  "
                 f"gap {b['mean'] - mp:+7.4f}")
    else:
        head += f" real {b['mean']:+7.4f}"
    print(f"{head}  WR {wr:5.1f}%  pooled-t{b['pooled_t']:+5.2f}"
          f"  daily-t{b['daily_t']:+5.2f}  halves {b['h1']:+.2f}/{b['h2']:+.2f}"
          f"  drop1 {b['drop1']:+.2f}  days {b['days']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--family", default="all", choices=sorted(FAMILIES))
    ap.add_argument("--buckets", type=int, default=5)
    args = ap.parse_args()

    con = sqlite3.connect(Path(args.db), timeout=30)
    print("=== all-time record by arm (settled, non-void):")
    for name in ("model", "impulse", "pair", "ticket", "all"):
        n, pnl, w, staked = con.execute(MACRO % FAMILIES[name]).fetchone()
        if not n:
            print(f"  {name:<9} (no settled fills)")
            continue
        print(f"  {name:<9} n={n:<5} staked ${staked:8.2f}  pnl ${pnl:+8.4f}  "
              f"wins {w}/{n} ({100 * (w or 0) / n:4.1f}%)  "
              f"per-fill ${pnl / n:+.4f}  "
              f"return {100 * pnl / staked:+.2f}% of stake")
    print()
    rows = con.execute(
        "SELECT f.ts_ms, f.ev_at_fill, f.stake, s.pnl, "
        "CASE WHEN s.won THEN 1 ELSE 0 END, f.filled_px, d.reason "
        "FROM fills f JOIN settlements s ON s.fill_id = f.id "
        "JOIN decisions d ON d.id = f.decision_id "
        f"WHERE COALESCE(s.void, 0) = 0 AND ({FAMILIES[args.family]}) "
        "ORDER BY f.ts_ms").fetchall()
    if not rows:
        print(f"no settled fills for family {args.family}")
        return
    days = [r[0] // 86400000 for r in rows]
    ts = [r[0] for r in rows]
    # BOTH sides of the comparison are per $1 STAKED. `ev_at_fill` is
    # expected_value(fair, filled_px, fee) -- already per dollar -- and the
    # realised side must be divided by the stake, because sizing varies ($0.70
    # to $2.26 on this record, $3.29 average). Comparing raw `pnl` per fill
    # against a per-dollar prediction mixes units and silently turns a sizing
    # rule into a phantom edge.
    pred = [r[1] if r[1] is not None else 0.0 for r in rows]
    has_pred = [r[1] is not None for r in rows]
    real = [r[3] / (r[2] or 1.0) for r in rows]
    staked = sum(r[2] or 0.0 for r in rows)
    won = [r[4] for r in rows]
    reason = [r[6] or "" for r in rows]
    lo, hi = min(ts), max(ts)
    import time
    fmt = lambda t: time.strftime("%m-%d %H:%M", time.gmtime(t / 1000))
    print(f"family {args.family}: {len(rows)} settled fills, "
          f"{sum(has_pred)} carrying an ev_at_fill, "
          f"{len(set(days))} days, {fmt(lo)} -> {fmt(hi)} UTC, "
          f"${staked:.2f} staked")
    fam = collections.Counter(r.split()[0] if r else "(none)" for r in reason)
    print("  by reason family:", dict(fam.most_common(8)))
    print("  (everything below is PER $1 STAKED)")
    print()
    line("ALL (realised)", real, [0.0] * len(real), days, won,
         show_pred=False)
    tot = sum(r[3] for r in rows)
    print(f"  {'total':<20} ${tot:+.4f} on ${staked:.2f} staked "
          f"({100 * tot / staked:+.2f}% of stake)")

    # Bucket by the arm's own predicted EV: does realised EV rise with it? If it
    # is flat, the gate is noise; if it rises, the gate is a real lever. Only
    # over the fills that recorded one.
    idx_all = [i for i in range(len(rows)) if has_pred[i]]
    if len(idx_all) < 10:
        print(f"\n(only {len(idx_all)} fills carry a predicted EV -- "
              f"no bucket table)")
        return
    order = sorted(idx_all, key=lambda i: pred[i])
    k = max(2, args.buckets)
    size = len(order) // k
    print(f"\nby PREDICTED EV ({k} quantile buckets), over {len(order)} fills:")
    line("TOTAL", [real[i] for i in order], [pred[i] for i in order],
         [days[i] for i in order], [won[i] for i in order])
    for b in range(k):
        idx = order[b * size:(b + 1) * size] if b < k - 1 else order[b * size:]
        if not idx:
            continue
        label = f"q{b + 1} pred ${pred[idx[0]]:+.3f}"
        line(label, [real[i] for i in idx], [pred[i] for i in idx],
             [days[i] for i in idx], [won[i] for i in idx])

    # The other candidate lever: the price we paid. Cheap favourites are the
    # arm's own thesis; this says whether the cheapest entries are the best.
    print("\nby entry PRICE band (filled_px):")
    bands = [(0.0, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.80),
             (0.80, 1.01)]
    for a, z in bands:
        idx = [i for i in range(len(rows))
               if rows[i][5] is not None and a <= rows[i][5] < z]
        if not idx:
            continue
        line(f"{a:.2f}-{z:.2f}", [real[i] for i in idx], [pred[i] for i in idx],
             [days[i] for i in idx], [won[i] for i in idx])


if __name__ == "__main__":
    main()
