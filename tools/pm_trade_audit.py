"""Are we actually trading, and if not, why not.

`stats: ... | no trades` in the live log is easy to misread: it counts FILLS in
the recent window, not attempts. This separates the two, per hour, and names the
gate that refused each non-trade, then scores whatever DID fill against the
venue's own resolution (`rounds.winner`, the Chainlink rule that pays).

Read-only. Run on the VPS against the live DB.

    .venv/bin/python tools/pm_trade_audit.py            # today, UTC
    .venv/bin/python tools/pm_trade_audit.py --days 3
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.venues.polymarket.broker import TAKER_FEE_RATE  # noqa: E402
from btcpredict.venues.polymarket.markets import RESOLUTION  # noqa: E402

#: The round's `feed_provider` for Polymarket rounds -- the venue's own era
#: marker, and the only reliable one in this DB.
PM_FEED_PROVIDER = RESOLUTION


def fee_per_share(px: float) -> float:
    """Taker fee, dollars per share. Measured on a fill, not published."""
    return TAKER_FEE_RATE * px * (1.0 - px)


def utc(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime(
        "%m-%d %H:%M")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--days", type=int, default=1, help="how many days back")
    ap.add_argument("--since", type=int, default=0,
                    help="epoch SECONDS; the window starts here")
    ap.add_argument("--pm-only", action="store_true",
                    help="start at the first POLYMARKET-era fill, so the "
                         "Binance-era modes cannot pollute the record")
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    # An INTEGER cutoff, computed here. `strftime('%s', ...)` returns TEXT and
    # SQLite sorts every TEXT value ABOVE every number, so `ts_ms > strftime(..)`
    # is silently false -- it reported "no fills" on a day with 43 of them.
    now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    cut = a.since * 1000 if a.since else now_ms - a.days * 86_400_000
    if a.pm_only:
        # The POLYMARKET era, dated from the ROUNDS' feed provider -- never from
        # the `fills.mode` labels. `cross`/`train`/`probe` read like PM arm
        # names but ran on the old venue (`vendor: PREDICT_FUN`) for days
        # before the switch, and cutting on them produced a "PM era" record of
        # 110 fills that were all the previous venue's.
        row = con.execute("select min(start_ms) from rounds where"
                          " feed_provider = ?", (PM_FEED_PROVIDER,)).fetchone()
        cut = (row[0] or 0) if a.since else max(cut, row[0] or 0)
        print("=== POLYMARKET era only, from %s (feed %s) ==="
              % (utc(cut), PM_FEED_PROVIDER))

    print("=== fills by hour (since %s) ===" % utc(cut))
    got = False
    for h, n, stk in con.execute(
            "select strftime('%m-%d %H', ts_ms/1000, 'unixepoch') h, count(*),"
            " round(sum(stake),2) from fills where ts_ms > ? group by h"
            " order by h", (cut,)):
        print("   %s   n=%-4d $%s" % (h, n, stk))
        got = True
    if not got:
        print("   none")

    print("\n=== fills by mode ===")
    for mode, n, stk in con.execute(
            "select mode, count(*), round(sum(stake),2) from fills"
            " where ts_ms > ? group by mode order by 2 desc", (cut,)):
        print("   %-12s n=%-4d $%s" % (mode, n, stk))

    print("\n=== why the arm did nothing (refusals, most common first) ===")
    tot = 0
    for reason, n, acted in con.execute(
            "select substr(reason,1,64), count(*), sum(acted) from decisions"
            " where ts_ms > ? and acted=0 group by substr(reason,1,64)"
            " order by 2 desc limit 14", (cut,)):
        print("   n=%-6d %s" % (n, reason))
        tot += n
    print("   ---- %d refusals" % tot)

    print("\n=== what DID fill, scored against the venue's resolution ===")
    rows = list(con.execute(
        "select f.side, f.filled_px, f.stake, f.fee_paid, f.slug, f.mode,"
        " d.spot, d.strike"
        " from fills f join rounds r on r.slug=f.slug"
        " left join decisions d on d.id=f.decision_id"
        " where r.winner in ('Up','Down') and f.ts_ms > ?", (cut,)))
    if not rows:
        print("   no fills in the window")
        return 0
    # `won` must come from the venue's own rule, so re-read the winner here.
    winners = {s: w for s, w in con.execute(
        "select slug, winner from rounds where winner in ('Up','Down')")}
    recs = []
    for side, px, stake, fee, slug, mode, spot, strike in rows:
        w = winners.get(slug)
        if not w:
            continue
        won = 1.0 if side == w else 0.0
        f = (fee / stake) if stake else fee_per_share(px)
        recs.append((won, px, stake, f, mode))
    n = len(recs)
    wr = st.fmean(r[0] for r in recs)
    paid = st.fmean(r[1] for r in recs)
    fee = st.fmean(r[3] for r in recs)
    pnl = sum((r[0] - r[1] - r[3]) * r[2] for r in recs)
    stake = sum(r[2] for r in recs)
    print("   n=%d  win %.1f%%  paid %.3f  fee/sh %.4f  P&L $%+.2f of $%.2f"
          " staked  (EV/$1 %+.4f)"
          % (n, 100 * wr, paid, fee, pnl, stake, pnl / stake if stake else 0))
    print("   break-even win rate at this price: %.1f%%"
          % (100 * (paid + fee)))
    for mode in sorted({r[4] for r in recs}):
        s = [r for r in recs if r[4] == mode]
        p = sum((r[0] - r[1] - r[3]) * r[2] for r in s)
        print("     %-8s n=%-4d win %5.1f%%  P&L $%+.2f"
              % (mode, len(s), 100 * st.fmean(r[0] for r in s), p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
