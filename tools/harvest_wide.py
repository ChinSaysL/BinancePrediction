"""Aggressive backward scan for historical rounds.

`market/detail` serves any marketTopicId, but ids for our event are irregularly
spaced (observed 5 to 31 ids per round as other markets interleave), so the
original harvester's "give up after 8 empty batches" quit far too early. This
sweeps a wide contiguous id range with high concurrency instead.

Each recovered round yields strike, winner, and crucially the Up/Down tokenIds,
which are what let on-chain OrderFilled events be mapped back to a round.
"""
from __future__ import annotations

import argparse, asyncio, logging, time
import aiohttp

from btcpredict.config import settings
from btcpredict.feeds.market_feed import MarketFeed
from btcpredict.store.db import Store
from tools.harvest import store_round

log = logging.getLogger("harvest_wide")


async def probe(feed, tid, sem):
    async with sem:
        for a in range(2):
            try:
                return tid, await feed.by_topic_id(tid)
            except Exception:
                await asyncio.sleep(0.3 * (a + 1))
    return tid, None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--span", type=int, default=4000, help="topic ids to sweep")
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--chunk", type=int, default=200)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    store = Store(settings.db_path)
    sem = asyncio.Semaphore(args.concurrency)
    async with aiohttp.ClientSession() as s:
        feed = MarketFeed(s)
        live = await feed.live_round()
        top = live.market_topic_id
        seen = {r["market_topic_id"] for r in store.query(
            "SELECT market_topic_id FROM rounds WHERE market_topic_id IS NOT NULL")}
        start = min(seen) if seen else top
        log.info("sweeping %d ids down from %d (have %d rounds)",
                 args.span, start, len(seen))

        t0, hits = time.time(), 0
        for lo in range(start - 1, start - 1 - args.span, -args.chunk):
            ids = [i for i in range(lo, max(lo - args.chunk, start - 1 - args.span), -1)
                   if i not in seen]
            if not ids:
                continue
            for tid, r in await asyncio.gather(*(probe(feed, i, sem) for i in ids)):
                seen.add(tid)
                if r and r.slug.startswith("btc-updown-5m"):
                    store_round(store, r)
                    hits += 1
            n = store.query("SELECT COUNT(*) c FROM rounds WHERE winner IS NOT NULL "
                            "AND up_token_id IS NOT NULL")[0]["c"]
            log.info("at id %d | +%d found | %d rounds with tokenIds | %.0fs",
                     lo, hits, n, time.time() - t0)

    rows = store.query("SELECT MIN(start_ms) a, MAX(start_ms) b, COUNT(*) c FROM rounds "
                       "WHERE winner IS NOT NULL AND up_token_id IS NOT NULL")[0]
    if rows["c"]:
        log.info("DONE: %d rounds spanning %.1f hours", rows["c"],
                 (rows["b"] - rows["a"]) / 3_600_000)
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
