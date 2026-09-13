"""Readout for the forward test: live book vs the honest-pricing book.

The Up side cannot be repaired by tuning (see FINDINGS.md): our disagreement
with the venue is worth 0.10 +- 0.36 of what we claim, every threshold tested
flipped sign between the first 60% of rounds and the last 40%, and the one
measurable mispricing -- the venue understating favourites by a factor of 1.24
on the log-odds -- reads +1.1%/$1 on 610 rounds, which is t 0.45 and cannot be
settled on the recording. So it is being settled forward.

This compares the two books on the metric that decides: profit per $1 staked,
with a standard error and a t-statistic, by side and in total.

    python -m tools.ab_report                    # default: both DBs on this host
    python -m tools.ab_report --db data/paper2.db
    python -m tools.ab_report --min-n 200        # go quiet until there is data

Reading it:
  * EV/$1 is stake-independent, so the two books can be compared even though
    they are sized differently;
  * t is the only column that says anything. Below about 2 it is noise, and a
    month of paper trades at ~250/day is roughly what the honest config needs
    before its t means anything;
  * the abort condition is stated up front: if the honest book's EV/$1 is
    <= 0 after 2,000 settled trades, the favourite-bias hypothesis is dead and
    the honest config is not the answer either.
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import sqlite3
import statistics as st
from pathlib import Path

ABORT_TRADES = 2000
# Trades before this moment are NOT a sample of the strategy that is running.
# Hand-maintained, and it has already moved twice in one session, so it only
# holds while the config is frozen -- which is the current plan:
#
#   .. 2026-09-10 23:14  the retired digital/0.12 gate, flat $1-$10 band
#   23:14 .. 2026-09-11 09:41  honest pricing, but every Down FAVOURITE was
#       refused by a complement bug in the pre-trade recheck (63 rounds refused,
#       54 of 55 trades Up) -- an Up-only sample of a different live path
#   09:41 .. 2026-09-11 12:59  what the replay actually measured (267 Up / 347
#       Down), at 2% of the bankroll x tilt with a $25 cap
#   12:59:44Z .. 2026-09-12 07:01  flat $10 (band $10-$10, tilt off) from
#       12:59:44Z; guards off from 13:05:44Z; LIMIT orders from 18:29Z; a
#       tau 150-270 window from 04:10Z; a server-only two-sided mode 04:17-06:43
#       that never existed in this workspace. 215 fills, -$7.69 (-1.09%/$1).
#       Nothing in this stretch pools with anything else: every knob moved.
#   from 2026-09-12 07:01Z  digital model both sides, calibrated benched.
#       Market orders, tau 60-210, 2% x tilt floored at the venue minimum,
#       guards ON (20% daily, 5-loss halt). Justified by the tick-level test in
#       FINDINGS.md (2026-09-12): same 973 settled rounds, digital's gate
#       +0.0775/$1 against the calibrated gate's +0.0123/$1.
#   from 2026-09-12 07:25Z  entry_mode=impulse: the model is NOT consulted for
#       entries. Live arm (A) trades a >=4 bps / 1 s move in the underlying,
#       one entry per round, at the venue minimum with the guards ON. The model
#       gate keeps being evaluated and recorded, so arm (B) -- the same rounds
#       scored by the gate -- is reconstructible offline from `quotes`+`rounds`.
#       Validation (1,641 rounds, one entry per round, net of the measured
#       1.32c/$1 slip): +0.1145 1st half / +0.0815 2nd half, against the gate's
#       +0.0288 / -0.0081 on the same rounds.
#   07:25Z .. 2026-09-12 09:38:33Z  the same impulse logic, but the trigger was
#       evaluated once per poll (1002 ms median) while the venue sits 5.3 ms
#       away -- so ~500 ms median of the trigger-to-order path was our own
#       sleep. One fill in this window, and it paid 0.703 on a 0.660 quote
#       (+4.3c, 6.5% of stake). NOT pooled with what follows: the fill process
#       changed, so the price paid is a different quantity.
#   from 2026-09-12 09:38:33Z  entry_mode=impulse with the trigger on the
#       FEED's clock (impulse_on_tick): evaluated on every bookTicker update
#       (~33/s) instead of once a second, so an order leaves ~20 ms after the
#       window closes rather than ~500 ms. Same statistic, same threshold, one
#       entry per round, guards unchanged. This is the era to measure fill
#       price against the trigger price in.
#   09:38:33Z .. 2026-09-12 11:23:46Z  the above with MARKET+FOK orders. Two
#       fills, both of which FAILED first (the bundle's reference was the
#       DISPLAYED price, stale by more than slippageBps) and then paid the
#       repriced price -- 0.81 against an ask of 0.60, and 0.84 against 0.17.
#   from 2026-09-12 11:23:46Z  order_type=limit_take: the order crosses the book
#       at max(quote, fair - safety_margin) and fills AT THE BOOK'S ASK (measured
#       with one real crossing order: limit 0.18 vs ask 0.15 -> filled 0.1526),
#       with the impulse arm's fair now a real digital rather than its own quote.
#       The fill process changed again, so this does not pool with the two fills
#       above either. The ticket arm also went live in this era (ticket_live,
#       10/day) and books as mode='live' -- separate it by joining fills to
#       decisions.reason LIKE 'ticket %'.
#
# The scaling rule's ~1,000-trade clock starts at the cut, not at the config
# switch: those 55 trades cannot be pooled with these.
CONFIG_CHANGE = "2026-09-12T11:23:46Z"


def parse_when(s: str) -> int:
    s = s.strip().replace("Z", "+00:00")
    if s.replace(".", "", 1).isdigit():
        return int(float(s))
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


def load(db: Path) -> list[sqlite3.Row]:
    if not db.exists():
        return []
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return list(c.execute("""
        SELECT f.mode, f.side, f.stake, s.pnl, s.won, s.ts_ms
        FROM fills f JOIN settlements s ON s.fill_id = f.id
        WHERE s.void = 0
        ORDER BY s.ts_ms"""))


def report(label: str, rows: list[sqlite3.Row], min_n: int) -> None:
    print(f"\n{label}: {len(rows)} settled trades")
    if not rows:
        return
    groups: dict[str, list[sqlite3.Row]] = {"ALL": list(rows)}
    for r in rows:
        groups.setdefault(r["side"], []).append(r)
    print(f"  {'group':<8}{'n':>7}{'WR%':>8}{'EV/$1':>10}{'se':>9}{'t':>8}"
          f"{'P&L':>11}")
    for name in ("ALL", "Up", "Down"):
        sel = groups.get(name)
        if not sel:
            continue
        rets = [r["pnl"] / r["stake"] for r in sel if r["stake"]]
        if not rets:
            continue
        ev = st.mean(rets)
        se = st.pstdev(rets) / math.sqrt(len(rets)) if len(rets) > 1 else 0.0
        wr = 100.0 * sum(1 for r in sel if r["won"]) / len(sel)
        pnl = sum(r["pnl"] for r in sel)
        flag = ""
        if name == "ALL" and len(sel) < min_n:
            flag = "  (below --min-n: ignore)"
        print(f"  {name:<8}{len(sel):>7}{wr:>7.1f}%{ev:>+10.3f}{se:>9.3f}"
              f"{ev / se if se else 0:>+8.2f}{pnl:>+11.2f}{flag}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", nargs="*", default=None,
                    help="databases to read (default: live + paper2)")
    ap.add_argument("--min-n", type=int, default=0,
                    help="flag groups with too few trades to read")
    ap.add_argument("--since", default=CONFIG_CHANGE,
                    help=f"only settled trades after this ISO time or epoch ms "
                         f"(default {CONFIG_CHANGE}, when the honest pricing "
                         f"went live). Pass 'all' to include earlier eras, "
                         f"which describes no configuration that ever ran.")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    dbs = [Path(p) for p in args.db] if args.db else [
        root / "data" / "btcpredict.db", root / "data" / "paper2.db"]
    cut = 0 if str(args.since).lower() == "all" else parse_when(args.since)
    if cut:
        print(f"counting settled trades after "
              f"{dt.datetime.fromtimestamp(cut/1000, dt.timezone.utc):%Y-%m-%d %H:%M} UTC "
              f"(the shipped config's start; --since all to include older eras)")

    for db in dbs:
        rows = [r for r in load(db) if r["ts_ms"] > cut]
        for mode in ("live", "paper"):
            sel = [r for r in rows if r["mode"] == mode]
            if sel:
                report(f"{db.name} [{mode}]", sel, args.min_n)

    paper = [r for r in load(dbs[-1])
             if r["mode"] == "paper" and r["ts_ms"] > cut]
    if paper:
        n = len(paper)
        rets = [r["pnl"] / r["stake"] for r in paper if r["stake"]]
        ev = st.mean(rets)
        print(f"\nhonest-pricing book: {n} trades, mean {ev:+.4f}/$1"
              f"  (abort at {ABORT_TRADES} trades if EV <= 0: "
              f"{100 * n / ABORT_TRADES:.0f}% of the way there)")
    else:
        print("\nhonest-pricing book: no paper trades yet")


if __name__ == "__main__":
    main()
