"""Is the ~249ms order latency a VENUE HOLD, or OUR client?

Why this is the next thing to measure. The cap sweep found the horizon dominates
everything: with the arrival modelled at 250ms the arm is profitable at every cap,
and at 500ms it is negative at every cap. `venue ack` p50 is 249ms. So the arm is
one latency doubling away from unprofitable, and the cap -- which is what we spent
the day tuning -- is a rounding error next to it.

We already know it is not the network: RTT to api.binance.com from the VPS is
2.05ms, and a warm HTTPS round trip is 6.2ms. Two authenticated POSTs to the same
host, timed in-process by the same code path, come back 47ms (get-quote) and 249ms
(place-order-bundle). So ~200ms is on the venue's side of the socket -- or in our
own client between the start of the call and the bytes leaving.

Three hypotheses, and they separate cleanly:

  H1 VENUE HOLD. The venue deliberately waits before answering an order. Signature:
     a HARD FLOOR -- a minimum that is large, with many samples bunched just above
     it, and a long tail. get-quote should show no such floor.
  H2 VENUE PROCESSING. Matching/risk genuinely takes that long. Signature: a low
     minimum and a broad, roughly continuous body.
  H3 OUR CLIENT. Connection-pool wait, TLS re-handshake, or serialisation.
     Signature: duration correlates with CONCURRENCY (other requests in flight),
     and the floor moves with load.

    python3 tools/latency_anatomy.py --log data/live.log
"""

from __future__ import annotations

import argparse
import bisect
import collections
import re
import statistics


def line_at(log, marker):
    """Every (timestamp_ms, value) for a marker line, in log order."""
    out = []
    for m in re.finditer(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}).*?" + marker, log, re.M
    ):
        t = m.group(1)
        try:
            import datetime as dt
            ms = int(dt.datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
                     .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
        out.append((ms + int(m.group(2)), float(m.group(3))))
    return out


def pct(v, p):
    return v[min(len(v) - 1, int(len(v) * p))]


def show(name, v):
    if not v:
        print(f"  {name:32s} n=0")
        return
    v = sorted(v)
    print(f"  {name:32s} n={len(v):4d}  min {v[0]:6.0f}  p10 {pct(v,.1):6.0f}  "
          f"p25 {pct(v,.25):6.0f}  p50 {pct(v,.5):6.0f}  p75 {pct(v,.75):6.0f}  "
          f"p90 {pct(v,.9):6.0f}  max {v[-1]:6.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="data/live.log")
    args = ap.parse_args()
    log = open(args.log, errors="ignore").read()

    gq = [v for _, v in line_at(log, r"shadow quote .*?: fresh px [\d.]+ after (\d+)ms")]
    ack = line_at(log, r"venue ack .*?: (\d+)ms")

    print("H1/H2/H3 -- the two authenticated POSTs, same host, same code path\n")
    show("POST trade/get-quote", gq)
    show("POST trade/place-order-bundle", [v for _, v in ack])
    print()

    if not ack:
        return
    ackv = [v for _, v in ack]
    floor = min(ackv)
    print(f"  ack floor (min of {len(ackv)}): {floor:.0f}ms")
    for w in (5, 10, 20, 50):
        n = sum(1 for x in ackv if x <= floor + w)
        print(f"    within {w:3d}ms of the floor: {n:3d}/{len(ackv)} = {n/len(ackv):5.1%}")
    if gq:
        print(f"  get-quote floor: {min(gq):.0f}ms   (a hold on the ORDER path only"
              f" would leave this untouched)")
    print()
    print("  HOLD signature needs BOTH: a large floor AND mass bunched at it.")
    print("  Below, the fraction of samples in each 25ms band above the floor:")
    for lo in range(0, 200, 25):
        n = sum(1 for x in ackv if floor + lo <= x < floor + lo + 25)
        bar = "#" * min(60, n)
        print(f"    +{lo:3d}-{lo+25:3d}ms  {n:3d} {bar}")
    print()

    # H3: concurrency. count acks within +-1s of each ack
    ts = [t for t, _ in ack]
    conc = [sum(1 for o in ts if o != t and abs(o - t) <= 1500) for t in ts]
    print("  H3 -- duration vs OTHER ORDERS IN FLIGHT (within 1.5s):")
    by = collections.defaultdict(list)
    for c, v in zip(conc, ackv):
        by[min(c, 4)].append(v)
    for c in sorted(by):
        v = sorted(by[c])
        print(f"    {c} other order(s) in flight: n={len(v):3d}  min {v[0]:5.0f}  "
              f"p50 {statistics.median(v):5.0f}  max {v[-1]:5.0f}")
    if len(set(conc)) > 1:
        hi = [v for c, v in zip(conc, ackv) if c >= 1]
        lo = [v for c, v in zip(conc, ackv) if c == 0]
        if hi and lo:
            print(f"    median with others {statistics.median(hi):.0f}ms vs alone "
                  f"{statistics.median(lo):.0f}ms")
    print()
    print("  H1 -- a hold should also make the duration INDEPENDENT of outcome:")
    for lab, pat in ((r"\(rejected\)", "rejected"), (r"\(venue_failed\)", "venue_failed"),
                     (r"order failed after", "gave up after 3")):
        print(f"    {lab:20s} occurrences: {len(re.findall(pat, log))}")


if __name__ == "__main__":
    main()
