"""Does having other ORDERS in flight inflate this order's latency?

What is already in FINDINGS.md and NOT repeated here:
  * geography is CLOSED -- Tokyo, ping 1.086ms, "move the server / detect earlier
    cannot buy anything there";
  * concurrency of OPEN POSITIONS was measured (cross_multi_exposure.py: peak 9,
    p90 4) and drove cross_max_open_stake_pct;
  * concurrency of CONNECTIONS was measured (5 concurrent single-topic sockets,
    5/5 delivered) and drove one socket per market;
  * concurrency of TICKS was covered by unit tests for double-entry.

None of those is "does a second order in flight slow down the first". This tool
measures that, and only that.

Method. `tools/latency_anatomy.py` used a PROXY (other acks within 1.5s). This
does it exactly: every `venue ack` line carries its own duration and is logged the
instant the POST returns, so each order's true service interval is
[start, end] = [ack_ts - dur_ms, ack_ts]. Overlap is then a fact, not a proxy.

STEP 1  rebuild the latency-vs-overlap table on exact intervals.
STEP 3  decide H3's mechanism. If the venue serialises per wallet, a burst of three
        concurrent orders should appear as a STAIRCASE of three ~185ms holds
        (total ~3x185ms). If it is genuinely parallel, the three return together.

    python3 tools/order_concurrency.py --log data/live.log
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import statistics


PAT = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}).*?venue ack (\S+) (\S+): (\d+)ms",
    re.M,
)


def parse(log):
    out = []
    for m in PAT.finditer(log):
        ts = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=dt.timezone.utc)
        end = int(ts.timestamp() * 1000) + int(m.group(2))
        dur = int(m.group(5))
        out.append({"slug": m.group(3), "side": m.group(4),
                    "start": end - dur, "end": end, "dur": dur})
    out.sort(key=lambda o: o["start"])
    return out


def overlaps(o, all_orders):
    n = 0
    for p in all_orders:
        if p is o:
            continue
        # strictly overlapping service windows
        if p["start"] < o["end"] and o["start"] < p["end"]:
            n += 1
    return n


def pct(v, p):
    return sorted(v)[min(len(v) - 1, int(len(v) * p))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="data/live.log")
    args = ap.parse_args()

    orders = parse(open(args.log, errors="ignore").read())
    print(f"order POSTs with a duration: {len(orders)}\n")
    if len(orders) < 3:
        return

    print("STEP 1 -- latency by EXACT number of other orders in flight")
    print(f"  {'others in flight':>18s} {'n':>4s} {'min':>6s} {'p25':>6s} "
          f"{'p50':>6s} {'p75':>6s} {'max':>6s}")
    by = {}
    for o in orders:
        k = overlaps(o, orders)
        by.setdefault(k, []).append(o["dur"])
    for k in sorted(by):
        v = by[k]
        print(f"  {k:>18d} {len(v):4d} {min(v):6.0f} {pct(v,.25):6.0f} "
              f"{statistics.median(v):6.0f} {pct(v,.75):6.0f} {max(v):6.0f}")
    alone = by.get(0, [])
    busy = [d for k, v in by.items() if k >= 1 for d in v]
    if alone and busy:
        print(f"\n  p50 ALONE {statistics.median(alone):.0f}ms   "
              f"p50 with company {statistics.median(busy):.0f}ms   "
              f"ratio {statistics.median(busy)/statistics.median(alone):.2f}x")
        print(f"  mean ALONE {statistics.fmean(alone):.0f}ms   "
              f"mean with company {statistics.fmean(busy):.0f}ms   "
              f"ratio {statistics.fmean(busy)/statistics.fmean(alone):.2f}x")
        # is the difference bigger than the spread? rank test, no scipy
        import random
        obs = statistics.fmean(busy) - statistics.fmean(alone)
        pool = alone + busy
        rng = random.Random(0)
        hits = 0
        for _ in range(5000):
            rng.shuffle(pool)
            if (statistics.fmean(pool[len(alone):])
                    - statistics.fmean(pool[:len(alone)])) >= obs:
                hits += 1
        print(f"  permutation p (5000 draws) = {hits/5000:.4f}")

    print("\nSTEP 3 -- is the venue SERIALISING per wallet?")
    print("  A staircase of sequential ~185ms holds = serialised.")
    print("  Stacked, returning together              = parallel.\n")
    bursts = []
    cur = []
    for o in orders:
        if cur and o["start"] >= max(p["end"] for p in cur):
            bursts.append(cur)
            cur = []
        cur.append(o)
    if cur:
        bursts.append(cur)
    shown = 0
    for b in bursts:
        if len(b) < 2:
            continue
        shown += 1
        t0 = min(p["start"] for p in b)
        span = max(p["end"] for p in b) - t0
        print(f"  burst {shown}: {len(b)} orders, wall-clock span {span}ms")
        for p in sorted(b, key=lambda x: x["start"]):
            print(f"    +{p['start']-t0:5d} -> +{p['end']-t0:5d}ms  "
                  f"dur {p['dur']:4d}ms  {p['slug']} {p['side']}")
        seq = sum(p["dur"] for p in b)
        print(f"    sequential would predict {seq}ms; actual span {span}ms "
              f"({'SERIALISED' if span > 0.8*seq else 'PARALLEL'})")
        if shown >= 6:
            break
    if not shown:
        print("  no multi-order bursts in this log")


if __name__ == "__main__":
    main()
