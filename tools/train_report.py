"""TRAIN arm canary report.

The Train arm is the only arm whose trigger is the UNDERLYING rather than the
book: fire when |spot - strike| >= cross_train_bps, 5m rounds only, tau >= 60s,
buy the leading side as a MARKET order bounded at ask + 1.5c, ceiling 0.89.

`canary_report.py` covers the Cross arm and cannot answer for this one -- it
hard-filters `d.reason like 'cross into%'`. This is its Train counterpart.

BACKTEST BASELINES (tools/momentum_continuation.py, X>=15bp D=0s, --min-tau 60,
n=41 over 7 days, paying the limit):
    stated ask      0.804
    filled price    0.819
    win rate        92.7%
    net/sh          +0.099      day-t +5.54
Break-even on a 0.819 fill with the measured fee is ~90.5%, so the arm's whole
margin is the 2.2 points between that and 92.7%.

WHAT TO WATCH, AND WHY IT IS NOT JUST THE WIN RATE. The cap ladder showed net/sh
is FLAT from 0.80 to 0.89 while day-t climbs +1.77 -> +5.54, i.e. the cap is a
sample-volume knob, not an EV knob. So the first thing to check is not whether
the arm is profitable but whether the POPULATION we fill matches the population
that was measured: backtest fills average 0.819, and a live mean drifting up
toward 0.89 would mean we are being filled only in the expensive tail, where a
win pays +0.10 and a loss costs -1.00.

Run on the VPS against the live DB:

    .venv/bin/python tools/train_report.py
    .venv/bin/python tools/train_report.py --since "2026-09-16 05:30:54"
    .venv/bin/python tools/train_report.py --one-per-round

Exit status is 0 always -- this reports, it does not gate.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import math
import sqlite3
import statistics

BASE_ASK = 0.804          # backtest mean stated ask
BASE_FILLED = 0.819       # backtest mean fill paying ask+1.5c
BASE_WIN = 0.927          # backtest win rate
BASE_NET = 0.099          # backtest net/sh paying the limit
BREAKEVEN = 0.905         # p = q(1+fee) at q=0.819 with the measured fee
TARGET_N = 25             # fills needed to leave the canary phase

# Stated-ask bands: this is the population the CAP selects.
ASK_BANDS = ((0.70, 0.80), (0.80, 0.85), (0.85, 0.89), (0.89, 1.01))
# Fill bands carry an extra low bucket AND an open top: the cap binds on the
# ASK, not the fill, so a 0.89 ask paid at +1.5c fills ABOVE 0.89. Two of the
# 18 recorded Train fills did exactly that and would be lost by a closed band.
FILL_BANDS = ((0.00, 0.70), (0.70, 0.80), (0.80, 0.85), (0.85, 0.89),
              (0.89, 1.01))


def parse_since(s: str) -> int:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
            return int(d.timestamp() * 1000)
        except ValueError:
            continue
    raise SystemExit(f"cannot parse --since {s!r}")


def day_t(ts_list: list, vals: list) -> tuple:
    """Day-clustered t on a per-observation value.

    Entries cluster in time: several fills can land in one round or one hour, so
    a plain standard error over fills overstates the evidence. Averaging by UTC
    day first is the same discipline the sweeps use.
    """
    by = collections.defaultdict(list)
    for ts, v in zip(ts_list, vals):
        by[dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).date()].append(v)
    means = [statistics.fmean(v) for v in by.values() if v]
    if len(means) < 2:
        return float("nan"), len(means)
    sd = statistics.stdev(means)
    if sd == 0:
        return float("nan"), len(means)
    return statistics.fmean(means) / (sd / math.sqrt(len(means))), len(means)


def fill_rows(c, since_ms: int, one_per_round: bool) -> list:
    """Filled Train orders, joined to their decision and the round outcome.

    `d.quote` IS the stated ask: it equals `f.requested_px` and the `@X` printed
    in the reason string ("train into Down @0.890 tau175s run+0.000").
    """
    q = """
    select f.ts_ms, f.side, f.requested_px, f.filled_px, f.shares, f.stake,
           f.fee_paid, r.winner, d.quote, d.tau_s, f.slug
      from fills f
      join decisions d on d.id = f.decision_id
      left join rounds r on r.slug = f.slug
     where d.reason like 'train into%'
       and f.filled_px > 0
       and r.winner in ('Up','Down')
       and f.ts_ms >= ?
     order by f.ts_ms
    """
    rows = list(c.execute(q, (since_ms,)))
    if not one_per_round:
        return rows
    seen, out = set(), []
    for r in rows:
        if r[10] in seen:
            continue
        seen.add(r[10])
        out.append(r)
    return out


def log_attempts(path: str, since_ms: int) -> tuple | None:
    """Order attempts and no-fills, read from the log.

    The DB CANNOT answer this. `decisions.acted = 1` returns 18 for the whole
    Train history, which EQUALS the fill count -- and that would be reported as
    a 100% fill rate, which is false: `cross_train` misses are a known cost (3
    of 7 attempts on the night of 09-16) and the log shows 6 `train exec ...
    NO FILL` lines in the same window. A missed attempt therefore never lands a
    decisions row with acted=1. The `train exec` line is the only record that
    an order was placed and did not fill.

    Timestamps are compared as naive local wall clock on both sides, because
    the log prefix and `datetime.fromtimestamp` share one timezone and guessing
    UTC here would silently drop or duplicate a window's worth of lines.
    """
    n = nf = 0
    try:
        fh = open(path, errors="replace")
    except OSError:
        return None
    since_local = (dt.datetime.fromtimestamp(since_ms / 1000)
                   if since_ms else None)
    with fh:
        for line in fh:
            if "btcpredict: train exec " not in line:
                continue
            if since_local is not None:
                try:
                    ts = dt.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if ts < since_local:
                    continue
            n += 1
            if "NO FILL" in line:
                nf += 1
    return n, nf


def band_of(px: float, bands) -> str:
    for lo, hi in bands:
        if lo <= px < hi:
            return f"[{lo:.2f},{hi:.2f})"
    return "outside"


def band_table(rows, bands, pick, name):
    print(f"   {name}")
    buckets = collections.OrderedDict((band_of(pick(r), bands), []) for r in rows)
    for r in rows:
        buckets[band_of(pick(r), bands)].append(r)
    for label, rs in buckets.items():
        if not rs:
            print(f"     {label:14s} n=  0")
            continue
        win = statistics.fmean(1.0 if r[7] == r[1] else 0.0 for r in rs)
        filled = statistics.fmean(r[3] for r in rs)
        net = statistics.fmean((1.0 if r[7] == r[1] else 0.0) - r[3]
                               - r[6] / r[4] for r in rs)
        t, days = day_t([r[0] for r in rs],
                        [(1.0 if r[7] == r[1] else 0.0) - r[3] - r[6] / r[4]
                         for r in rs])
        flag = "" if len(rs) >= 15 else "  (too few)"
        print(f"     {label:14s} n={len(rs):3d}  win {win:6.1%}  "
              f"fill {filled:.3f}  net/sh {net:+.4f}  day-t {t:+5.2f} "
              f"{days}d{flag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--since", default=None,
                    help="UTC timestamp to measure from; default is all history")
    ap.add_argument("--one-per-round", action="store_true",
                    help="count only the first fill per round (cross_max_per_round"
                         "=1 makes retries the only source of duplicates)")
    ap.add_argument("--log", default=None,
                    help="live.log, to recover the ATTEMPT count: the DB cannot, "
                         "see log_attempts()")
    args = ap.parse_args()

    since = parse_since(args.since) if args.since else 0
    c = sqlite3.connect(args.db)
    rs = fill_rows(c, since, args.one_per_round)
    mode = "one per round" if args.one_per_round else "all fills"

    print(f"TRAIN canary report -- {mode}"
          + (f", since {args.since} UTC" if args.since else ", all history"))
    print(f"fills             {len(rs)}")
    if args.log:
        la = log_attempts(args.log, since)
        if la is None:
            print(f"attempts          {args.log} not readable")
        elif la[0]:
            print(f"attempts (log)    {la[0]}   ({la[1]} NO FILL = "
                  f"{la[1] / la[0]:.0%} missed)")
            # Deliberately NOT printed as a ratio to the DB fill count: the log
            # only labels `train exec` from the commit that added the label, so
            # the two cover different windows and dividing them manufactures a
            # fill rate out of nothing.
            print("   NB the log labels `train exec` only from the commit that "
                  "added it.")
            print("   Fills from before that are in the DB and not in this "
                  "count, so")
            print("   attempts and fills are comparable only inside a --since "
                  "window")
            print("   that falls wholly within the labelled era.")
        else:
            print("attempts (log)    0 in this window")
    print()
    if not rs:
        print("no fills with a settled round yet -- nothing to report")
        return
    span = (dt.datetime.fromtimestamp(rs[0][0] / 1000, dt.timezone.utc),
            dt.datetime.fromtimestamp(rs[-1][0] / 1000, dt.timezone.utc))
    print(f"window            {span[0]:%Y-%m-%d %H:%M} .. {span[1]:%Y-%m-%d %H:%M}"
          f"  ({(span[1] - span[0]).total_seconds() / 3600:.1f}h)")

    asked = statistics.fmean(r[8] for r in rs)
    filled = statistics.fmean(r[3] for r in rs)
    win = statistics.fmean(1.0 if r[7] == r[1] else 0.0 for r in rs)
    slip = statistics.fmean(r[3] - r[8] for r in rs)
    above = sum(1 for r in rs if r[3] > r[8] + 1e-9)
    per_share = [(1.0 if r[7] == r[1] else 0.0) - r[3] - r[6] / r[4] for r in rs]
    net = statistics.fmean(per_share)
    dollars = statistics.fmean(
        (r[4] if r[7] == r[1] else 0.0) - r[5] - r[6] for r in rs)
    t, days = day_t([r[0] for r in rs], per_share)
    fees = statistics.fmean(r[6] for r in rs)

    print(f"\nn                 {len(rs):3d}   (target >= {TARGET_N} to leave canary)"
          f"   {'PASS' if len(rs) >= TARGET_N else 'NOT YET'}")
    print(f"win rate          {win:6.1%}   (backtest {BASE_WIN:.1%}, "
          f"breakeven {BREAKEVEN:.1%} at a {BASE_FILLED:.3f} fill)"
          f"   {'PASS' if win >= BREAKEVEN else 'FAIL'}")
    print(f"stated ask        {asked:.4f}   (backtest {BASE_ASK:.3f})")
    print(f"filled price      {filled:.4f}   (backtest {BASE_FILLED:.3f})")
    print(f"slippage          {slip:+.4f}/sh   (we pay about the limit: "
          f"{above}/{len(rs)} = {above / len(rs):.0%} above the stated ask)")
    print(f"fee               ${fees:.4f}/fill")
    print(f"net/sh            {net:+.4f}   (backtest {BASE_NET:+.3f})   day-t "
          f"{t:+5.2f} over {days} days")
    print(f"net $/fill        {dollars:+.4f}")
    print()

    band_table(rs, ASK_BANDS, lambda r: r[8], "by STATED ASK (the population the cap selects)")
    print()
    band_table(rs, FILL_BANDS, lambda r: r[3], "by FILLED PRICE (what we actually paid)")

    print("\ndecision rules:")
    print("  the cap ladder says net/sh is FLAT 0.80->0.89 while day-t rises with")
    print("  sample. So read the FILLED band table for POPULATION MATCH, not the")
    print("  cap: backtest fills average 0.819, and a live mean drifting to 0.89")
    print("  means we are only being filled in the tail (win +0.10, loss -1.00).")
    print("  win < breakeven after 25 fills -> pause the arm and re-check the")
    print("  trigger, not the cap: the ladder already cleared the cap.")
    print("  net/sh < 0 with a mean fill near 0.819 -> the edge itself is absent")


if __name__ == "__main__":
    main()
