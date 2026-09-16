"""PER-ATTEMPT LEDGER for the Cross and Train arms.

WHY THIS EXISTS. Every fill-level statistic in this repo is keyed by SLUG, and a
slug is not an order:

  * a slug can carry SEVERAL ATTEMPTS -- the first FOK is rejected by the venue,
    we re-quote, and the retry goes out seconds later at a different price;
  * a slug can carry SEVERAL FILL ROWS -- in the live DB 5 of 53 cross slugs
    have 2-4 rows each (eth-updown-5m-1789413600 has 4);
  * the trigger line, the entry quote and the fill price are three DIFFERENT
    prices, and joining them by slug mixes attempts.

That is how I produced a table showing `btc-updown-5m-1789475700 Down` with a
live quote of 0.5600 against a fill of 0.2869 -- a 27c better fill than the
quote it was supposedly priced against. Slug-level joins cannot be trusted for
anything involving price.

WHAT THIS BUILDS. One record per (slug, side, trigger), with the events that
belong to it in time order:

    TRIGGER  CROSS <slug> <side> sweep_px=X (spot S vs strike K, tau T)
    ENTRY    warm quote used ... px Q          <- the price we decided on
    FRESH    shadow quote ... fresh px Q ... -> bound B (ceiling C)
    SEND     venue ack ... quote Q slip S -> bound B, ceiling C   <- 1 per POST
    RESULT   <arm> exec <slug> <side>: decided D limit L -> F in Ns
    PATH     cross meter ... +0.25s=A +0.50s=B +1.00s=C [fire|refuse]
             cross fine  ... 05=A 10=B 15=C ...   <- 50ms ask ladder, ~1s

The PATH lines are the point. They record what the BOOK did after the trigger,
so the anti-selection question becomes answerable without a counterfactual:
we can see whether a fill was available inside our own ceiling, and what we
paid if we got one.

USAGE (on the VPS, from /root/BinancePrediction):

    .venv/bin/python tools/attempt_ledger.py
    .venv/bin/python tools/attempt_ledger.py --since "2026-09-15 17:00"
    .venv/bin/python tools/attempt_ledger.py --dump
    .venv/bin/python tools/attempt_ledger.py --arm train --dump

Exit status is 0 always -- this reports, it does not gate.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import re
import sqlite3
import statistics

TS = r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)"

P_TRIGGER = re.compile(
    TS + r".*?btcpredict: (CROSS|TRAIN) (\S+) (\S+) sweep_px=([0-9.]+) "
         r"\(spot ([0-9.]+) vs strike ([0-9.]+), tau (\d+)s\)")
P_WARM = re.compile(
    TS + r".*?btcpredict: warm quote used for (\S+) (\S+): age ([0-9.]+)s "
         r"px ([0-9.]+)")
P_SHADOW = re.compile(
    TS + r".*?btcpredict: shadow quote (\S+) (\S+): fresh px ([0-9.]+) "
         r"after (\d+)ms -> bound ([0-9.]+) \(ceiling ([0-9.]+)\)")
P_ACK = re.compile(
    TS + r".*?venue ack (\S+) (\S+): (\d+)ms, quote ([0-9.]+) slip (\d+) "
         r"-> bound ([0-9.]+), ceiling ([0-9.]+)")
# The outcome. `<arm> exec <slug> <side>: decided D limit L -> R in Ns | ...`
P_EXEC = re.compile(
    TS + r".*?btcpredict: (cross|train) exec (\S+) (\S+): decided ([0-9.]+) "
         r"limit ([0-9.]+) -> (NO FILL|[0-9.]+) in ([0-9.]+)s")
P_REFUSE = re.compile(
    TS + r".*?btcpredict: (cross|train) (\S+) (\S+) not taken: (.*)$")
P_METER = re.compile(
    TS + r".*?btcpredict: (cross|train) meter (\S+) (\S+): "
         r"\+0\.25s=(\S+) \+0\.50s=(\S+) \+1\.00s=(\S+) \[(\w+)\]")
P_FINE = re.compile(
    TS + r".*?btcpredict: (cross|train) fine (\S+) (\S+) (?:fire|refuse): (.*)$")
P_DECLINE = re.compile(
    TS + r".*?btcpredict: (CROSS|TRAIN) attempt (\d+)/(\d+) declined")
P_VENUEFAIL = re.compile(
    TS + r".*?btcpredict: (CROSS|TRAIN) order attempt (\d+)/(\d+) failed for "
         r"(\S+) (\S+) \(([a-z_]+)\)")
P_NOFILL = re.compile(
    TS + r".*?btcpredict: (CROSS|TRAIN) no fill for (\S+) (\S+): ([a-z_]+)")

FEE_OF_PRICE = 0.0117          # measured median fee as a fraction of price


def ladder(fine: str) -> list:
    """The 50ms ask ladder from a `cross fine` line -> [(ms, ask), ...].

    Values of `n/a` mean the websocket book was missing at that sample; they are
    skipped rather than treated as a price.
    """
    out = []
    for tok in fine.split():
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        if not k.isdigit() or v == "n/a":
            continue
        try:
            out.append((int(k), float(v)))
        except ValueError:
            continue
    return sorted(out)


class Ledger:
    """One record per (slug, side) trigger, with its events attached."""

    def __init__(self):
        self.recs = {}
        # Attempt-level counters that CANNOT be attributed to a slug: the
        # `CROSS attempt n/m declined` line names no market, so it can only be
        # counted globally. Attributing it by "the next line" would be a guess.
        self.counts = collections.Counter()

    def get(self, slug: str, side: str) -> dict:
        r = self.recs.get((slug, side))
        if r is None:
            r = self.recs[(slug, side)] = {
                "slug": slug, "side": side, "arm": None, "ts": None,
                "triggers": 0, "ask": None, "spot": None, "strike": None,
                "depth_bps": None, "tau": None,
                "warm": None, "warm_age": None, "fresh": None, "fresh_ms": None,
                "bound": None, "ceiling": None, "acks": [], "pretrade": [],
                "declines": 0, "venue_fails": 0, "refuse": None,
                "meter": None, "ladder": [], "exec_px": None, "no_fill": None,
                "elapsed": None, "reason": None, "execs": 0,
            }
        return r

    def add(self, line: str) -> None:
        m = P_TRIGGER.search(line)
        if m:
            ts, arm, slug, side, spx, spot, k, tau = m.groups()
            r = self.get(slug, side)
            r["triggers"] += 1
            if r["ask"] is None:                 # keep the FIRST trigger
                r.update(arm=arm, ts=ts, ask=float(spx), spot=float(spot),
                         strike=float(k), tau=int(tau),
                         depth_bps=1e4 * abs(float(spot) - float(k)) / float(k))
            return
        m = P_WARM.search(line)
        if m:
            ts, slug, side, age, px = m.groups()
            r = self.get(slug, side)
            if r["warm"] is None:
                r["warm"], r["warm_age"] = float(px), float(age)
            return
        m = P_SHADOW.search(line)
        if m:
            ts, slug, side, px, ms, bound, ceil = m.groups()
            r = self.get(slug, side)
            if r["fresh"] is None:
                r.update(fresh=float(px), fresh_ms=int(ms),
                         bound=float(bound), ceiling=float(ceil))
            return
        m = P_ACK.search(line)
        if m:
            ts, slug, side, ms, px, slip, bound, ceil = m.groups()
            self.get(slug, side)["acks"].append(
                dict(ts=ts, ms=int(ms), quote=float(px), slip=int(slip),
                     bound=float(bound), ceiling=float(ceil)))
            return
        m = P_EXEC.search(line)
        if m:
            ts, arm, slug, side, dec, lim, res, el = m.groups()
            r = self.get(slug, side)
            r["arm"] = "TRAIN" if arm == "train" else (r["arm"] or "CROSS")
            r["execs"] += 1
            # A (slug, side) can legitimately carry MORE THAN ONE exec line: the
            # arm can re-fire the same market. Keeping both fields from separate
            # orders produced a `NO FILL` row with a gap, because exec_px came
            # from the first and no_fill from the second. The LAST exec wins, and
            # both fields are set together so the row is always one real order.
            r["elapsed"] = float(el)
            if res == "NO FILL":
                r["no_fill"], r["exec_px"] = True, None
            else:
                r["exec_px"], r["no_fill"] = float(res), None
            return
        m = P_REFUSE.search(line)
        if m:
            ts, arm, slug, side, why = m.groups()
            r = self.get(slug, side)
            r["arm"] = "TRAIN" if arm == "train" else (r["arm"] or "CROSS")
            if r["refuse"] is None:
                r["refuse"] = why.strip()
            return
        m = P_METER.search(line)
        if m:
            ts, arm, slug, side, a, b, c, kind = m.groups()
            self.get(slug, side)["meter"] = dict(
                p25=a, p50=b, p100=c, kind=kind)
            return
        m = P_FINE.search(line)
        if m:
            ts, arm, slug, side, rest = m.groups()
            self.get(slug, side)["ladder"] = ladder(rest)
            return
        m = P_DECLINE.search(line)
        if m:
            self.counts["declines"] += 1
            return
        m = P_VENUEFAIL.search(line)
        if m:
            ts, arm, slug, side, _n, _m, why = m.groups()
            r = self.get(slug, side)
            r["arm"] = "TRAIN" if arm == "TRAIN" else (r["arm"] or "CROSS")
            r["venue_fails"] += 1
            return
        m = P_NOFILL.search(line)
        if m:
            ts, arm, slug, side, why = m.groups()
            self.get(slug, side)["reason"] = why
            return


def settle_map(db: str) -> dict:
    c = sqlite3.connect(db)
    return dict(c.execute(
        "select slug, winner from rounds where winner in ('Up','Down')"))


def pct(n, d):
    return f"{n / d:5.1%}" if d else "  n/a"


def net(px, won):
    return (1.0 if won else 0.0) - px - FEE_OF_PRICE * px


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="data/live.log")
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--since", default=None,
                    help="UTC 'YYYY-MM-DD HH:MM:SS'; default all of the log")
    ap.add_argument("--arm", choices=("cross", "train", "all"), default="all")
    ap.add_argument("--dump", action="store_true",
                    help="print every order attempt, one line each")
    args = ap.parse_args()

    led = Ledger()
    for line in open(args.log, errors="replace"):
        if args.since and line[:19] < args.since:
            continue
        led.add(line)

    win = settle_map(args.db)
    recs = list(led.recs.values())
    for r in recs:
        r["winner"] = win.get(r["slug"])
    if args.arm != "all":
        want = "TRAIN" if args.arm == "train" else "CROSS"
        recs = [r for r in recs if (r["arm"] or "CROSS") == want]
    recs.sort(key=lambda r: r["ts"] or "")

    ordered = [r for r in recs if r["exec_px"] is not None or r["no_fill"]]
    refused = [r for r in recs if r["refuse"] and not r["no_fill"]]

    print(f"ledger for {args.log}"
          + (f" since {args.since}" if args.since else "")
          + f"   arm={args.arm}")
    fired = [r for r in recs if r["triggers"]]
    print(f"  markets in the ledger {len(recs)}")
    print(f"    fired a trigger     {len(fired)}")
    print(f"    refused pre-order   {len(refused)}")
    print(f"  went to the venue     {len(ordered)}")
    print(f"  venue POST attempts   {sum(len(r['acks']) for r in recs)}"
          f"   (declines {led.counts['declines']}, "
          f"venue_failed {sum(r['venue_fails'] for r in recs)})")
    refires = [r for r in recs if r["execs"] > 1]
    if refires:
        print(f"  markets fired TWICE  {len(refires)}   "
              f"(the last exec is kept; {sum(r['execs'] - 1 for r in refires)}"
              f" extra order(s) not counted separately)")
    print()

    # ---------------- the attempt table ----------------
    print("=== EVERY ORDER ATTEMPT (trigger ask -> what we paid) ===")
    print("  time      arm   market                      side  trigAsk  warm   "
          "fresh  bound  ceil  paid    gap    result")
    for r in ordered:
        # A record can exist without a trigger line: the trigger is logged only
        # when the arm FIRES, so a market that was refused, or whose CROSS line
        # sits outside --since, has an ack/exec but no timestamp of its own.
        t = (r["ts"] or "")[11:19] or " --:--  "
        gap = (f"{r['exec_px'] - r['ask']:+.3f}"
               if r["exec_px"] is not None and r["ask"] else "   -  ")
        res = "NO FILL" if r["no_fill"] else f"{r['exec_px']:.4f}"
        f2 = lambda v: f"{v:.3f}" if v is not None else "  -  "
        print(f"  {t} {r['arm'] or '?':5s} {r['slug']:28s} "
              f"{r['side']:5s} {f2(r['ask'])} {f2(r['warm'])} "
              f"{f2(r['fresh'])} {f2(r['bound'])} {f2(r['ceiling'])} "
              f"{res:7s} {gap}  "
              f"{'win' if r['winner'] == r['side'] else 'LOSS'}"
              f"{'' if r['winner'] else ' ?'}")

    # ---------------- trigger ask vs what we paid ----------------
    both = [r for r in ordered if r["exec_px"] is not None and r["ask"]
            and r["winner"]]
    print(f"\n=== TRIGGER ASK vs EXECUTABLE FILL  (n={len(both)} filled) ===")
    if both:
        gaps = [r["exec_px"] - r["ask"] for r in both]
        print(f"  mean gap {statistics.fmean(gaps):+.4f}   median "
              f"{statistics.median(gaps):+.4f}   min {min(gaps):+.4f}   "
              f"max {max(gaps):+.4f}")
        print(f"  mean trigger ask {statistics.fmean(r['ask'] for r in both):.4f}"
              f"   mean fill {statistics.fmean(r['exec_px'] for r in both):.4f}")
        w = sum(1 for r in both if r["winner"] == r["side"])
        print(f"  win {pct(w, len(both))}   net/sh "
              f"{statistics.fmean(net(r['exec_px'], r['winner'] == r['side']) for r in both):+.4f}"
              f"   <- the only honest number")

    # ---------------- was a fill available inside our ceiling? ----------------
    print("\n=== WAS A FILL AVAILABLE INSIDE OUR OWN CEILING? ===")
    print("  (from the 50ms `cross fine` ladder; n/a samples skipped)")
    avail = miss = nofill = 0
    for r in ordered:
        if not r["ladder"] or r["ceiling"] is None:
            continue
        best = min(a for _, a in r["ladder"])
        if best <= r["ceiling"] + 1e-9:
            avail += 1
            if r["no_fill"]:
                miss += 1
        else:
            nofill += 1
    print(f"  attempts with a ladder       {avail + nofill}")
    print(f"  book offered <= our ceiling  {avail}   (we did NOT fill {miss})")
    print(f"  book never reached ceiling   {nofill}")

    # ---------------- filled vs not, at trigger level ----------------
    print("\n=== FILLED vs NOT, at trigger level (diagnostic, not P&L) ===")
    for nm, grp in (("filled", [r for r in ordered if r["exec_px"] is not None]),
                    ("no fill", [r for r in ordered if r["no_fill"]]),
                    ("refused pre-order", refused)):
        g = [r for r in grp if r["winner"]]
        if not g:
            print(f"  {nm:18s} n=  0"); continue
        w = sum(1 for r in g if r["winner"] == r["side"])
        ask = statistics.fmean(r["ask"] for r in g if r["ask"])
        dep = statistics.fmean(r["depth_bps"] for r in g if r["depth_bps"])
        print(f"  {nm:18s} n={len(g):3d}  win {pct(w, len(g))}  "
              f"mean trigger ask {ask:.4f}  mean depth {dep:6.3f}bps")

    print("\ncaveats that matter when reading this:")
    print("  * the ladder covers ~1s from the meter, not the whole order wait;")
    print("    `cross exec` elapsed runs to 8s on a retry, so 'available' is a")
    print("    LOWER bound on what was on offer.")
    print("  * a trigger ask is not an executable price. The gap column is the")
    print("    whole point of this tool: read it before believing any")
    print("    trigger-level expectancy number.")
    print("  * `warm` is the quote we DECIDED on, `fresh` the one we re-read")
    print("    ~50ms later. Where they differ, the book moved under us.")


if __name__ == "__main__":
    main()
