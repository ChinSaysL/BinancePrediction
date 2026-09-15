"""STEP 3: does the venue SERIALISE authenticated requests per wallet?

The observational evidence in order_concurrency.py says orders that overlapped a
peer took 602ms median vs 219ms alone (6/6 slow vs a 30% base rate). But those 6
orders are 3 co-submission EVENTS, the burst minutes carry 2.3x normal volume
(concurrency_control.py), and within the alone group a busy minute does not
predict duration at all. Observational data cannot settle it. So ask the venue.

Method: fire K concurrent `trade/get-quote` POSTs and time each one.
  * get-quote is a READ. It returns a price; it places nothing. We already call
    it constantly on the order path (p50 ~47ms), so this adds no new load class.
  * K=1 is the control, interleaved, so a slow minute cannot be mistaken for a
    serialisation effect.

    serialised per wallet  ->  K=3 latencies ~ 3 x K=1, and they finish in a
                               STAIRCASE
    parallel               ->  K=3 latencies ~ K=1, finishing together

Caveat, stated up front: get-quote has no ~185ms floor while place-order-bundle
does, so the two may sit in different queues. This probe can CONFIRM
serialisation; a flat result is only weak evidence about the order path.

    .venv/bin/python tools/venue_serialisation.py --db data/btcpredict.db
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import statistics
import time

import aiohttp

from btcpredict.config import settings
from btcpredict.execution.live import LiveBroker


def candidate_tokens(db: str, n: int = 8) -> list[tuple[str, str]]:
    """Newest rounds first. ORDER BY slug is WRONG -- it sorts the slug as
    text and picked a dead 'unitree-...' market, whose every get-quote came
    back HTTP 400 'This market is currently inactive' in ~40ms. That measured
    the REJECT path, not the quote service. Order by start_ms."""
    c = sqlite3.connect(db)
    rows = c.execute(
        "SELECT slug, up_token_id FROM rounds "
        "WHERE up_token_id IS NOT NULL AND up_token_id != '' "
        "ORDER BY start_ms DESC LIMIT ?", (n,)).fetchall()
    if not rows:
        raise SystemExit("no round with a token id in the local DB")
    return [(r[0], r[1]) for r in rows]


async def timed(b: LiveBroker, token: str) -> float:
    t = time.monotonic()
    await b.get_quote(token, 1.0)
    return (time.monotonic() - t) * 1000.0


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--rounds", type=int, default=8)
    args = ap.parse_args()

    slug, token = None, None
    async with aiohttp.ClientSession() as session:
        b = LiveBroker(session, settings.binance_api_key,
                       settings.binance_api_secret,
                       slippage_bps=settings.slippage_bps)
        if not await b.discover_wallet():
            raise SystemExit("could not discover the wallet")

        # GUARD: a rejected quote returns the same shape as a real one, and a
        # 400 costs ~40ms -- measuring that would produce a confident table
        # about the wrong code path. Only measure a market that actually
        # ANSWERS with a quote.
        for s, t in candidate_tokens(args.db):
            if await b.get_quote(t, 1.0) is not None:
                slug, token = s, t
                break
        if token is None:
            raise SystemExit(
                "no candidate round returned a quote -- all inactive. "
                "NOT measuring: a 400 reject is not the quote path.")
        print(f"probing with {slug} up_token_id {token[:18]}...\n")

        # warm the connection so TLS is not in the measurement
        await timed(b, token)

        results: dict[int, list[float]] = {1: [], 2: [], 3: []}
        for _ in range(args.rounds):
            for k in (1, 2, 3):
                if k == 1:
                    d = await timed(b, token)
                    results[1].append(d)
                else:
                    t0 = time.monotonic()
                    ds = await asyncio.gather(*[timed(b, token) for _ in range(k)])
                    span = (time.monotonic() - t0) * 1000.0
                    results[k].extend(ds)
                    results.setdefault(0, []).append(span)  # wall clock for k

    print(f"  {'K':>3s} {'n':>4s} {'min':>7s} {'p50':>7s} {'max':>7s}   wall-clock span p50")
    base = statistics.median(results[1])
    for k in (1, 2, 3):
        v = results[k]
        print(f"  {k:>3d} {len(v):4d} {min(v):7.0f} {statistics.median(v):7.0f} "
              f"{max(v):7.0f}")

    print("\n  wall clock for one fire of K concurrent (median):")
    walls = results.get(0)
    if walls:
        w1 = [walls[i] for i in range(0, len(walls), 3)]
        w2 = [walls[i] for i in range(1, len(walls), 3)]
        w3 = [walls[i] for i in range(2, len(walls), 3)]
        for k, w in ((1, w1), (2, w2), (3, w3)):
            print(f"    K={k}: {statistics.median(w):7.0f}ms")

    print(f"\n  p50 ratio K=3 / K=1 = "
          f"{statistics.median(results[3])/base:.2f}x")
    if statistics.median(results[3]) / base > 1.8:
        print("  -> SERIALISED: a second concurrent request waits for the first.")
        print("     Co-submitting orders is therefore real loss, and capping")
        print("     in-flight orders at 1 would hand the FIRST order the fast")
        print("     path (~220ms) instead of the queued one (~600ms).")
    else:
        print("  -> PARALLEL: concurrent requests do not queue on the wallet.")
        print("     The 6 slow co-submitted orders were NOT queued by us; the")
        print("     cause is load or tail. Serialising would buy nothing.")


if __name__ == "__main__":
    asyncio.run(main())
