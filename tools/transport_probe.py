"""IDEA 5: does our own transport cost anything?

The claim: if the client drops the TCP/TLS connection between rounds, a fresh
handshake adds 21-28ms to every order, and a heartbeat would remove it.

Two measurements, because the claim has two halves:

  PART A -- production. 39 real `venue ack` lines with their durations, against
  the IDLE GAP before each one. If a cold socket were being paid for, the first
  order after a long quiet spell would be the slow one. This needs no probe.

  PART B -- probe. Same read-only `trade/get-quote` POST measured three ways:
  on a brand-new session (cold DNS+TCP+TLS), on a warm one, and after a 35s
  idle (to see whether the connection is actually kept alive between rounds).

    .venv/bin/python tools/transport_probe.py --log data/live.log
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import re
import sqlite3
import statistics
import time

import aiohttp

from btcpredict.config import settings
from btcpredict.execution.live import LiveBroker

PAT = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d+).*?venue ack \S+ \S+: (\d+)ms",
    re.M)


def tokens(db, n=8):
    c = sqlite3.connect(db)
    rows = c.execute(
        "SELECT slug, up_token_id FROM rounds "
        "WHERE up_token_id IS NOT NULL AND up_token_id != '' "
        "ORDER BY start_ms DESC LIMIT ?", (n,)).fetchall()
    return [(r[0], r[1]) for r in rows]


def part_a(log):
    print("PART A -- production: idle gap before an order vs its ack duration")
    ev = []
    for m in PAT.finditer(open(log, errors="ignore").read()):
        t = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=dt.timezone.utc).timestamp() + int(m.group(2)) / 1000.0
        ev.append((t, int(m.group(3))))
    ev.sort()
    if len(ev) < 6:
        print("  too few acks")
        return
    gaps, durs = [], []
    for i in range(1, len(ev)):
        gaps.append(ev[i][0] - ev[i - 1][0])
        durs.append(ev[i][1])
    print(f"  n={len(gaps)}   idle gap p50 {statistics.median(gaps):.0f}s  "
          f"max {max(gaps):.0f}s")
    for lo, hi, name in ((0, 30, "gap <30s"), (30, 600, "30s-10min"),
                         (600, 10**9, ">10min")):
        v = [d for g, d in zip(gaps, durs) if lo <= g < hi]
        if v:
            print(f"  {name:10s} n={len(v):3d}  ack p50 {statistics.median(v):6.0f}ms"
                  f"  max {max(v):5.0f}ms")
    print("  A cold-socket cost would show as the >10min bucket being slower.")


async def one(session, b, token):
    t = time.monotonic()
    await b.get_quote(token, 1.0)
    return (time.monotonic() - t) * 1000.0


async def part_b(db):
    print("\nPART B -- probe: cold session vs warm vs after 35s idle")
    tok = tokens(db)
    # Session 1 is DISCOVERY ONLY, then thrown away -- discovery must not warm
    # the session whose first request we are about to time.
    async with aiohttp.ClientSession() as s1:
        b1 = LiveBroker(s1, settings.binance_api_key,
                        settings.binance_api_secret,
                        slippage_bps=settings.slippage_bps)
        if not await b1.discover_wallet():
            print("  could not discover the wallet")
            return
        live = None
        for slug, t in tok:
            if await b1.get_quote(t, 1.0) is not None:
                live = (slug, t)
                break
    if live is None:
        # A 400 "market inactive" costs ~40ms and is NOT the quote path; timing
        # it would produce a confident table about the wrong endpoint.
        print("  no live market answered -- NOT measuring a 400 reject")
        return
    print(f"  market: {live[0]}")

    async with aiohttp.ClientSession() as s2:
        b2 = LiveBroker(s2, settings.binance_api_key,
                        settings.binance_api_secret,
                        slippage_bps=settings.slippage_bps)
        b2.wallet_address = b1.wallet_address
        b2.wallet_id = b1.wallet_id
        cold = await one(s2, b2, live[1])
        print(f"  COLD first request on a new session     {cold:7.1f}ms")
        warm = [await one(s2, b2, live[1]) for _ in range(6)]
        print(f"  WARM steady state (n=6)                 "
              f"{statistics.median(warm):7.1f}ms  "
              f"[{min(warm):.0f}-{max(warm):.0f}]")
        print("  ... idling 35s to test keep-alive ...")
        await asyncio.sleep(35)
        after = await one(s2, b2, live[1])
        print(f"  AFTER 35s IDLE, same session            {after:7.1f}ms")
    print("\n  If the venue's own ~185ms order hold is what it is, none of these")
    print("  numbers can matter: a 20ms transport saving is ~10% of the pipeline,")
    print("  inside a window that needs ~360ms of lead to be useful at all.")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="data/live.log")
    ap.add_argument("--db", default="data/btcpredict.db")
    args = ap.parse_args()
    part_a(args.log)
    await part_b(args.db)


if __name__ == "__main__":
    asyncio.run(main())
