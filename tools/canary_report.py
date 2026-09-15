"""Canary report for the ask+2c execution cap (shipped 2026-09-15 17:14:38 UTC).

Answers one question: did capping the cross arm's fill tolerance at ask+2c move
the realised execution price down, and did the arm keep winning?

The pre-change facts it is measured against (live DB, all cross fills):
    paid 0.623  against a trigger ask of 0.571   (+0.055/sh slippage,
                                                  84% of fills above the ask)
    win  59.1%  where the population at that price wins 71.9%
    net  -0.037/sh   (-0.088/$1)

Run on the VPS against the live DB:

    .venv/bin/python tools/canary_report.py --since "2026-09-15 17:14:38"

Exit status is 0 always -- this reports, it does not gate.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import statistics


def parse_since(s: str) -> int:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
            return int(d.timestamp() * 1000)
        except ValueError:
            continue
    raise SystemExit(f"cannot parse --since {s!r}")


def rows(c, since_ms, mode):
    q = """
    select f.ts_ms, f.side, f.requested_px, f.filled_px, f.shares, f.stake,
           f.fee_paid, r.winner, d.quote, d.tau_s, f.slug
      from fills f
      join decisions d on d.id = f.decision_id
      left join rounds r on r.slug = f.slug
     where d.reason like 'cross into%'
       and f.filled_px > 0
       and r.winner in ('Up','Down')
       and f.ts_ms >= ?
    """
    if mode == "one-per-round":
        seen, out = set(), []
        for row in sorted(c.execute(q, (since_ms,))):
            if row[10] in seen:
                continue
            seen.add(row[10])
            out.append(row)
        return out
    return list(c.execute(q, (since_ms,)))


def attempts(c, since_ms):
    """Every cross order attempt (fills and no-fills), for the fill rate."""
    q = """
    select count(*) from decisions
     where reason like 'cross into%' and acted = 1 and ts_ms >= ?
    """
    return c.execute(q, (since_ms,)).fetchone()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--since", default="2026-09-15 17:14:38",
                    help="UTC timestamp the canary went live")
    ap.add_argument("--baseline-fills", type=int, default=44,
                    help="pre-change fills to compare against")
    args = ap.parse_args()

    since = parse_since(args.since)
    c = sqlite3.connect(args.db)
    n_att = attempts(c, since)

    print(f"canary live since {args.since} UTC")
    print(f"cross attempts (acted) since then: {n_att}\n")

    target = {
        "paid": 0.585, "paid_lo": 0.0,
        "win_floor": 0.60, "win_good": 0.65,
    }

    for mode in ("all", "one-per-round"):
        rs = rows(c, since, mode)
        label = "all cross fills" if mode == "all" else "one per round"
        if not rs:
            print(f"{label:>22s}: no fills yet")
            continue
        paid = statistics.fmean(r[3] for r in rs)
        tape = statistics.fmean(r[8] for r in rs)
        win = statistics.fmean(1.0 if r[7] == r[1] else 0.0 for r in rs)
        net = statistics.fmean(
            (1.0 if r[7] == r[1] else 0.0) - r[3] - r[6] / r[4] for r in rs)
        slip = statistics.fmean(r[3] - r[8] for r in rs)
        above = sum(1 for r in rs if r[3] > r[8] + 1e-9)
        print(f"{label}  n={len(rs)}")
        print(f"   paid      {paid:.4f}   (baseline 0.623, target <= 0.585)"
              f"   {'PASS' if paid <= target['paid'] else 'FAIL'}")
        print(f"   trigger   {tape:.4f}")
        print(f"   slippage  {slip:+.4f}/sh   (baseline +0.055)")
        print(f"   above ask {above}/{len(rs)} = {above/len(rs):.0%}   (baseline 84%)")
        print(f"   win       {win:.1%}   (baseline 59.1%, floor 60%, scale at 65%)"
              f"   {'PASS' if win >= target['win_floor'] else 'FAIL'}")
        print(f"   net/sh    {net:+.4f}   (baseline -0.037)")
        print()

    print("decision rules:")
    print("  win >= 65% after 50 fills  -> leak closed, safe to scale size")
    print("  win <  60% after 50 fills  -> pause the arm")
    print("  paid still > 0.585         -> the cap is not reaching the venue")
    print()
    print("revert:  on the VPS, restore .env.bak-20260915-171428 and")
    print("         systemctl restart btcpredict")


if __name__ == "__main__":
    main()
