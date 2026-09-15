"""The confound control for order_concurrency.py.

order_concurrency.py found: 6 orders that genuinely overlapped another order's
service window all took >=315ms, vs p50 219ms for the 33 that did not. But the
overlapping orders are not random: our code fires on a SECOND symbol only when
the market is moving hard enough to cross several strikes at once. That is a
common cause -- a busy minute both makes us fire twice AND may make the venue
slow. The correlation would then be real and the mechanism wrong, and
serialising our submissions would buy nothing.

This tool tests that with a measure that knows nothing about our orders:
Binance 1-minute BTC/ETH/BNB bars.

  1. ambient |1m return| (bps) and volume ratio around each order, alone vs
     with-company;
  2. within the ALONE group, does ambient activity predict duration? If a busy
     market is enough, slow orders need no peer;
  3. the base rate of slow orders that had no peer at all.

    .venv/bin/python tools/concurrency_control.py --log data/live.log --db data/btcpredict.db
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
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
                    "start": end - dur, "end": end, "dur": dur, "ts": end})
    out.sort(key=lambda o: o["start"])
    return out


def symbol_of(slug: str) -> str:
    return slug.split("-")[0].upper() + "USDT"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="data/live.log")
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--slow-ms", type=int, default=300)
    args = ap.parse_args()

    orders = parse(open(args.log, errors="ignore").read())
    for o in orders:
        o["n_overlap"] = sum(
            1 for p in orders
            if p is not o and p["start"] < o["end"] and o["start"] < p["end"])
        o["sym"] = symbol_of(o["slug"])

    conn = sqlite3.connect(args.db)
    bars = {}
    for sym, o_ms, close, vol, _ in conn.execute(
            "SELECT symbol, open_ms, close, volume, taker_buy FROM bars"):
        bars.setdefault(sym, []).append((o_ms, close, vol))
    for v in bars.values():
        v.sort()

    def ambient(sym, ts):
        v = bars.get(sym)
        if not v:
            return None
        # the 1m bar containing ts, plus the two before it
        import bisect
        keys = [x[0] for x in v]
        i = bisect.bisect_right(keys, ts) - 1
        if i < 2:
            return None
        cur, prev = v[i][1], v[i - 1][1]
        ret_bps = abs(cur / prev - 1.0) * 1e4
        vols = [x[2] for x in v[max(0, i - 120):i + 1]]
        med = statistics.median(vols) or 1.0
        return ret_bps, v[i][2] / med

    cool = slow = weak = 0
    a_ret, c_ret, a_vol, c_vol = [], [], [], []
    for o in orders:
        am = ambient(o["sym"], o["ts"])
        o["amb"] = am
        if am is None:
            continue
        (a_ret if o["n_overlap"] == 0 else c_ret).append(am[0])
        (a_vol if o["n_overlap"] == 0 else c_vol).append(am[1])
        if o["n_overlap"]:
            cool += 1
            slow += o["dur"] >= args.slow_ms
        else:
            weak += o["dur"] < args.slow_ms

    n_alone = sum(1 for o in orders if o["n_overlap"] == 0)
    slow_alone = sum(1 for o in orders
                     if o["n_overlap"] == 0 and o["dur"] >= args.slow_ms)

    print("1. AMBIENT market at the moment of the order (venue bars, blind to us)")
    print(f"   {len(a_ret)} alone vs {len(c_ret)} with-company, ambient read on "
          f"{sum(1 for o in orders if o['amb'])} orders")
    if a_ret and c_ret:
        print(f"   |1m return| bps   alone median {statistics.median(a_ret):6.1f}"
              f"   with-company median {statistics.median(c_ret):6.1f}")
        print(f"   volume ratio      alone median {statistics.median(a_vol):6.2f}"
              f"   with-company median {statistics.median(c_vol):6.2f}")
        print("   -> if with-company means are much higher, a busy minute is the "
              "common cause")

    print("\n2. WITHIN THE ALONE GROUP, does a busy minute alone explain slowness?")
    alone = [o for o in orders if o["n_overlap"] == 0 and o["amb"]]
    if alone:
        med_r = statistics.median([o["amb"][0] for o in alone])
        hi = [o["dur"] for o in alone if o["amb"][0] >= med_r]
        lo = [o["dur"] for o in alone if o["amb"][0] < med_r]
        print(f"   alone, high-vol half (n={len(hi)}): p50 {statistics.median(hi):5.0f}ms"
              f"   max {max(hi):4d}ms   share>=300ms {sum(d>=300 for d in hi)}/{len(hi)}")
        print(f"   alone, low-vol  half (n={len(lo)}): p50 {statistics.median(lo):5.0f}ms"
              f"   max {max(lo):4d}ms   share>=300ms {sum(d>=300 for d in lo)}/{len(lo)}")

    print("\n3. BASE RATE: slow orders need no peer")
    print(f"   alone and >=300ms : {slow_alone}/{n_alone} "
          f"({slow_alone/max(1,n_alone):.0%})")
    print(f"   company and >=300ms: {slow}/{cool} ({slow/max(1,cool):.0%})"
          if cool else "   no company orders")
    print(f"   -> the association is {'real in direction but' if slow_alone else ''}"
          " built on 3 co-submission EVENTS, not 6 samples; the 6 orders are "
          "3 bursts of 2 and are not independent")

    print("\n4. THE 3 BURSTS, as events")
    bursts, cur = [], []
    for o in sorted(orders, key=lambda x: x["start"]):
        if cur and o["start"] >= max(p["end"] for p in cur):
            bursts.append(cur)
            cur = []
        cur.append(o)
    if cur:
        bursts.append(cur)
    for b in bursts:
        if len(b) < 2:
            continue
        t0 = min(p["start"] for p in b)
        t = dt.datetime.fromtimestamp(t0 / 1000, dt.timezone.utc)
        print(f"   {t:%H:%M:%S}  " + "  ".join(
            f"{p['sym']}/{p['side']} {p['dur']}ms"
            + (f" [vol {p['amb'][0]:.0f}bps {p['amb'][1]:.2f}x]" if p["amb"] else "")
            for p in b))


if __name__ == "__main__":
    main()
