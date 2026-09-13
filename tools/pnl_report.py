"""Realised P&L over 1 / 7 / 30 days, by arm -- plus what a 30-day figure would
require us to assume.

Two different things live in this file, and they are kept visually separate:

  * REALISED: what the settled, non-void, real-money fills actually paid, sliced
    by settlement time. Nothing here is modelled. If a window has no fills
    because the record does not go back that far, it says so instead of
    printing a zero that looks like a flat month.
  * PROJECTED: what the same arms would pay over 30 days IF the measured edges
    hold. Every assumption is printed with the number. The range is not the
    standard deviation of the daily sum -- that would be far too narrow, because
    it ignores that the EDGE ITSELF is only known to about +/-50% (pooled-t ~2
    on ~50 trades). The range quoted is the sampling error on the edge, which is
    the dominant uncertainty at this sample size.

Usage:  python -m tools.pnl_report [--db data/btcpredict.db] [--live-only]
"""
from __future__ import annotations

import argparse
import collections
import sqlite3
import time
from pathlib import Path

DAY_MS = 86_400_000

# Reason family -> how the arm's fills are identified in `decisions`.
ARMS = {
    "impulse": "d.reason LIKE 'impulse %'",
    "ticket": "d.reason LIKE 'ticket %'",
    "model": "d.reason = 'ok'",
    "pair (external)": "d.reason LIKE 'pair %'",
}

# What the DEPLOYED arms measure, per $1 staked, and how often they trade.
# Sources: FINDINGS.md -- impulse `--sweep-window --by-price` (live path, 48
# fills/1,128 rounds), ticket `--split z div --min-z 0.8 --min-div 0.05` (n=28).
PROJECTION = {
    "impulse": {"trades_day": 12.0, "stake": 1.2, "ev_per_dollar": 0.638,
                "pooled_t": 2.30, "n": 48},
    "ticket": {"trades_day": 5.5, "stake": 1.0, "ev_per_dollar": 1.330,
               "pooled_t": 1.95, "n": 28},
}


def hhmm(ms: int) -> str:
    return time.strftime("%m-%d %H:%M", time.gmtime(ms / 1000))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--live-only", action="store_true", default=True,
                    help="real money only (fills.mode='live'); default on")
    args = ap.parse_args()
    con = sqlite3.connect(Path(args.db), timeout=30)
    con.row_factory = sqlite3.Row

    base = ("FROM settlements s JOIN fills f ON f.id = s.fill_id "
            "JOIN decisions d ON d.id = f.decision_id "
            "WHERE COALESCE(s.void, 0) = 0")
    modes = [r[0] for r in con.execute(
        f"SELECT DISTINCT f.mode {base}".replace("WHERE", "WHERE"))]
    print("fill modes in the settled record:",
          {m: n for m, n in con.execute(
              f"SELECT f.mode, COUNT(*) {base} GROUP BY 1")})
    if args.live_only:
        base += " AND f.mode = 'live'"

    bounds = [(r["lo"], r["hi"]) for r in con.execute(
        f"SELECT MIN(s.ts_ms) lo, MAX(s.ts_ms) hi {base}")]
    if not bounds or bounds[0][0] is None:
        print("no settled live fills")
        return
    lo, hi = bounds[0]
    print(f"real-money settled record: {hhmm(lo)} -> {hhmm(hi)} UTC "
          f"({(hi - lo) / DAY_MS:.1f} days)\n")

    now = int(time.time() * 1000)
    print("=== REALISED, by window (as far back as the record goes) ===")
    print(f"  {'window':<22} {'n':>4} {'staked':>9} {'P&L':>10} {'ret':>8} "
          f"{'WR':>6}  {'best':>7} {'worst':>7}")
    for label, ms in (("1 day", DAY_MS), ("7 days", 7 * DAY_MS),
                      ("30 days", 30 * DAY_MS), ("all of it", 10 ** 15)):
        r = con.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(f.stake), 0) stk, "
            f"COALESCE(SUM(s.pnl), 0) pnl, "
            f"SUM(CASE WHEN s.won THEN 1 ELSE 0 END) w, "
            f"MAX(s.pnl) best, MIN(s.pnl) worst {base} AND s.ts_ms >= ?",
            (now - ms,)).fetchone()
        if not r["n"]:
            print(f"  {label:<22} -- no fills in this window")
            continue
        print(f"  {label:<22} {r['n']:>4} ${r['stk']:>8.2f} ${r['pnl']:>+9.4f} "
              f"{100 * r['pnl'] / r['stk']:>+7.2f}% "
              f"{100 * (r['w'] or 0) / r['n']:>5.1f}%  "
              f"${r['best']:>+6.2f} ${r['worst']:>+6.2f}")

    print("\n=== REALISED, by arm (all of it) ===")
    for name, cond in ARMS.items():
        r = con.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(f.stake), 0) stk, "
            f"COALESCE(SUM(s.pnl), 0) pnl, "
            f"SUM(CASE WHEN s.won THEN 1 ELSE 0 END) w {base} AND {cond}",
        ).fetchone()
        if not r["n"]:
            print(f"  {name:<18} (no settled fills)")
            continue
        print(f"  {name:<18} n={r['n']:<5} staked ${r['stk']:>8.2f}  "
              f"P&L ${r['pnl']:>+8.4f}  ret {100 * r['pnl'] / r['stk']:>+7.2f}%  "
              f"WR {100 * (r['w'] or 0) / r['n']:>5.1f}%  "
              f"per-fill ${r['pnl'] / r['n']:>+7.4f}")

    print("\n=== REALISED, per UTC day ===")
    print(f"  {'day':<8} {'n':>4} {'staked':>9} {'P&L':>10} {'ret':>8}")
    for r in con.execute(
            f"SELECT s.ts_ms / {DAY_MS} d, COUNT(*) n, "
            f"COALESCE(SUM(f.stake), 0) stk, COALESCE(SUM(s.pnl), 0) pnl "
            f"{base} GROUP BY 1 ORDER BY 1"):
        day = time.strftime("%m-%d", time.gmtime(r["d"] * DAY_MS / 1000))
        print(f"  {day:<8} {r['n']:>4} ${r['stk']:>8.2f} ${r['pnl']:>+9.4f} "
              f"{100 * r['pnl'] / r['stk']:>+7.2f}%")

    print("\n=== PROJECTED (NOT realised) -- what 30 days would pay if the "
          "measured edges hold ===")
    print("  assumptions, each measured, none fitted:")
    per_day, var_day = 0.0, 0.0
    for name, p in PROJECTION.items():
        ev = p["ev_per_dollar"] * p["stake"] * p["trades_day"]
        sd = 0.8 * p["stake"] * (p["trades_day"] ** 0.5)   # per-trade sd ~$0.80/$1
        per_day += ev
        var_day += sd ** 2
        print(f"    {name:<8} {p['trades_day']:>4.1f} trades/day x ${p['stake']:.2f}"
              f" x EV/${p['ev_per_dollar']:.3f} per $1 = ${ev:>+6.2f}/day"
              f"   [pooled t {p['pooled_t']:+.2f} on n={p['n']}]")
    print(f"    {'-' * 62}")
    print(f"    {'total':<8} expected ${per_day:>+6.2f}/day  "
          f"(daily noise sd ${var_day ** 0.5:.2f})")
    for label, days in (("1 day", 1), ("7 days", 7), ("30 days", 30)):
        mean = per_day * days
        noise = (var_day ** 0.5) * (days ** 0.5)
        # The dominant uncertainty is not the daily noise, it is the EDGE: a
        # pooled t of ~2 means each arm's EV is known to roughly +/-50%.
        edge_lo, edge_hi = mean * 0.5, mean * 1.5
        print(f"    {label:<8} ${mean:>+7.2f} expected  |  "
              f"daily-noise band ${mean - noise:>+7.2f}..${mean + noise:>+7.2f}  |  "
              f"edge-uncertainty band ${edge_lo:>+7.2f}..${edge_hi:>+7.2f}")
    print("    the second band is the honest one: at pooled-t ~2 the edge itself"
          "\n    could be half or double what was measured, and neither arm has"
          "\n    a live record long enough to settle it. A negative 30 days is"
          "\n    still inside one standard error of the edge.")
    print("\n  (the ticket's $3/day stop and the guard's 20% bankroll cap bound")
    print("   the downside per day but do not protect a wrong edge.)")


if __name__ == "__main__":
    main()
