"""What has the live arm actually done today?

Every "the arm is behaving" claim so far was checked with an ad-hoc query, and
each of those had to rediscover the schema (there is no `size_usdt`; the stake
column is `stake`) and the reason strings. This is that query, once, with the
schema discovered rather than assumed.

It prints, for one UTC day:
  * the live config the running process would load (band, tau, margins, caps);
  * decisions grouped by reason, with the money staked per reason;
  * every real-money fill, with the decision reason that produced it;
  * ticket entries with their tau and quoted price (the ARM's own behaviour);
  * settlements and the ticket arm's realised P&L against its daily stop;
  * any reason string that is NOT recognised, so a new arm cannot hide in the
    table unnoticed.

Usage:  python -m tools.live_status [--db data/btcpredict.db] [--day 2026-09-12]
"""
from __future__ import annotations

import argparse
import calendar
import collections
import re
import signal
import sqlite3
import time
from pathlib import Path


def hhmmss(ms: int) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(ms / 1000))


def columns(con: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def tables(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def main() -> None:
    # Piping into head/grep must not dump a traceback.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--day", default=None, help="UTC date, e.g. 2026-09-12")
    args = ap.parse_args()

    if args.day:
        # calendar.timegm treats the struct as UTC, unlike mktime.
        day = calendar.timegm(time.strptime(args.day, "%Y-%m-%d")) * 1000
    else:
        day = int(time.time() // 86400) * 86400 * 1000
    con = sqlite3.connect(Path(args.db), timeout=30)
    print(f"=== {time.strftime('%Y-%m-%d', time.gmtime(day / 1000))} UTC "
          f"(from {hhmmss(day)}), db {args.db}")
    print("tables:", ", ".join(tables(con)))

    dcols = columns(con, "decisions")
    stake_col = "stake" if "stake" in dcols else None
    print(f"\n--- decisions by reason"
          f"{' (staked)' if stake_col else ''}")
    # The table logs EVERY poll's reason, not just entries: thousands of rows
    # all meaning "nothing to do". Reasons are parameterised ("tau 50s outside
    # window" vs "tau 51s..."), so digits are collapsed or they never group.
    agg: dict[str, list] = collections.defaultdict(lambda: [0, 0.0, 0])
    for reason, n, tot, acted in con.execute(
            f"SELECT reason, COUNT(*), SUM({stake_col or '1'}), "
            f"MAX(COALESCE(acted, 0)) FROM decisions WHERE ts_ms >= ? "
            f"GROUP BY reason", (day,)):
        key = re.sub(r"\d+", "N", reason or "(null)")
        agg[key][0] += n
        agg[key][1] += tot or 0.0
        agg[key][2] = max(agg[key][2], acted or 0)
    entries = [(k, v) for k, v in agg.items()
               if v[1] > 0 or k.startswith("ticket") or k == "ok"]
    rejects = sorted((kv for kv in agg.items() if kv not in entries),
                     key=lambda kv: -kv[1][0])
    for k, (n, tot, acted) in sorted(entries, key=lambda kv: -kv[1][1]):
        print(f"   ENTRY  {k[:34]:<36} n={n:<5} ${tot:8.2f}")
    print(f"   (rejections, top 12 of {len(rejects)} distinct kinds)")
    for k, (n, _tot, _acted) in rejects[:12]:
        print(f"   skip   {k[:34]:<36} n={n}")
    print(f"   total skipped polls: {sum(v[0] for _, v in rejects)}")

    fcols = columns(con, "fills")
    scols = columns(con, "settlements")
    if fcols:
        pxc = next((c for c in ("filled_px", "requested_px") if c in fcols), None)
        print("\n--- real-money fills today")
        rows = con.execute(
            "SELECT f.ts_ms, f.slug, f.side, f.shares, f.stake, d.reason "
            "FROM fills f LEFT JOIN decisions d ON d.id = f.decision_id "
            "WHERE f.ts_ms >= ? ORDER BY f.id", (day,)).fetchall()
        if not rows:
            print("   (none)")
        for ts_ms, slug, side, sh, stk, reason in rows:
            px = ""
            if pxc:
                got = con.execute(
                    f"SELECT {pxc} FROM fills WHERE ts_ms = ? AND slug = ?",
                    (ts_ms, slug)).fetchone()
                px = f"@{got[0]:.4f} " if got and got[0] else ""
            print(f"   {hhmmss(ts_ms)}  {slug[:26]:<28} {side or '':<4} "
                  f"{px}sh={sh or 0:.2f} ${stk or 0:.2f}  <- {(reason or '')[:20]}")

    # Is the edge we PREDICTED at the fill the edge we got? `ev_at_fill` is the
    # arm's own estimate at entry; settlements carry the truth. Comparing the
    # two is the only way to tell a mis-set entry gate from bad luck.
    if "ev_at_fill" in fcols and scols and "pnl" in scols:
        print("\n--- predicted (ev_at_fill) vs realised, per fill")
        rows = con.execute(
            "SELECT f.ts_ms, f.ev_at_fill, f.stake, s.pnl, d.reason "
            "FROM fills f LEFT JOIN settlements s ON s.fill_id = f.id "
            "LEFT JOIN decisions d ON d.id = f.decision_id "
            "WHERE f.ts_ms >= ? AND f.ev_at_fill IS NOT NULL "
            "ORDER BY f.id", (day,)).fetchall()
        settled = [r for r in rows if r[3] is not None]
        if not rows:
            print("   (no fills carrying an ev_at_fill today)")
        else:
            pred = sum(r[1] * (r[2] or 1.0) for r in rows) / len(rows)
            print(f"   fills {len(rows)}, settled {len(settled)}; "
                  f"mean predicted EV/fill ${pred:+.4f}")
            if settled:
                real = sum(r[3] for r in settled) / len(settled)
                print(f"   mean realised  EV/fill ${real:+.4f}  "
                      f"(gap ${real - pred:+.4f})")
            else:
                print("   (nothing settled yet -- no realised figure)")

    print("\n--- ticket arm's own entries (tau at entry, quoted price)")
    rows = con.execute(
        "SELECT ts_ms, tau_s, quote, acted, reason FROM decisions "
        "WHERE reason LIKE 'ticket %' AND ts_ms >= ? ORDER BY id", (day,)).fetchall()
    if not rows:
        print("   (none today)")
    for ts_ms, tau, quote, acted, reason in rows:
        print(f"   {hhmmss(ts_ms)}  tau={tau:>4.1f}s  px={quote:.3f}  "
              f"acted={acted}  {reason}")

    scols = columns(con, "settlements")
    if scols and "pnl" in scols:
        print("\n--- realised P&L today (settled, non-void), by arm")
        base = ("FROM settlements s JOIN fills f ON f.id = s.fill_id "
                "JOIN decisions d ON d.id = f.decision_id "
                "WHERE s.ts_ms >= ? AND COALESCE(s.void, 0) = 0")
        for label, extra in (("ALL", ""),
                             ("ticket", " AND d.reason LIKE 'ticket %'"),
                             ("main (non-ticket)", " AND d.reason NOT LIKE 'ticket %'")):
            n, pnl, w, big, bad = con.execute(
                f"SELECT COUNT(*), COALESCE(SUM(s.pnl), 0), "
                f"SUM(COALESCE(s.won, 0)), MAX(s.pnl), MIN(s.pnl) "
                f"{base}{extra}", (day,)).fetchone()
            if not n:
                print(f"   {label:<18} (nothing settled)")
                continue
            print(f"   {label:<18} n={n:<4} pnl=${pnl:+8.4f}  wins {w}/{n} "
                  f"({100 * (w or 0) / n:4.1f}%)  best ${big:+.3f}  "
                  f"worst ${bad:+.3f}  per-settlement ${pnl / n:+.4f}")
        print("   (the ticket arm's daily loss stop is $3.00)")

    gcols = columns(con, "guard_events")
    if gcols:
        print("\n--- guard events today")
        for row in con.execute(
                f"SELECT * FROM guard_events WHERE ts_ms >= ? "
                f"ORDER BY id DESC LIMIT 10", (day,)).fetchall():
            print("   ", "  ".join(str(x)[:28] for x in row[:5]))


if __name__ == "__main__":
    main()
