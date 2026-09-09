"""Harvest settled 5m rounds by walking marketTopicId backwards.

The public feed only exposes the live round, but `market/detail` will return any
topic id. Topic ids for our event are non-contiguous (other markets interleave),
averaging ~5 ids per round, so we scan every id in a window and keep the hits.

Each settled round yields an exact oracle observation:
    strike    = Chainlink price at round start
    end_price = Chainlink price at round end   (== next round's strike)
    winner    = the resolved outcome
That chain reconstructs the true settlement price series at 5-minute granularity,
which is what the model must be calibrated against.

Usage:  python -m tools.harvest --rounds 600
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time

import aiohttp

from btcpredict.config import settings
from btcpredict.feeds.market_feed import MarketFeed
from btcpredict.store.db import Store

log = logging.getLogger("harvest")


async def _probe(feed: MarketFeed, tid: int, sem: asyncio.Semaphore):
    async with sem:
        for attempt in range(3):
            try:
                r = await feed.by_topic_id(tid)
                return tid, r
            except Exception:
                await asyncio.sleep(0.4 * (attempt + 1))
        return tid, None


def store_round(store: Store, r) -> None:
    up, down = r.up, r.down
    store.upsert_round({
        "slug": r.slug,
        "market_topic_id": r.market_topic_id,
        "market_id": r.market_id,
        "condition_id": r.condition_id,
        "symbol": r.symbol,
        "start_ms": r.start_ms,
        "end_ms": r.end_ms,
        "strike": r.strike,
        "end_price": r.end_price,
        "fee_rate_bps": r.fee_rate_bps,
        "up_token_id": up.token_id if up else None,
        "down_token_id": down.token_id if down else None,
        "price_feed_id": r.price_feed_id,
        "feed_provider": r.feed_provider,
        "status": r.status,
        "winner": r.winner_name,
    })


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=300, help="target settled rounds")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--batch", type=int, default=60, help="topic ids per sweep")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    store = Store(settings.db_path)
    sem = asyncio.Semaphore(args.concurrency)

    async with aiohttp.ClientSession() as session:
        feed = MarketFeed(session)
        live = await feed.live_round()
        if not live:
            log.error("could not fetch live round; aborting")
            return
        log.info("live round %s (topic %d)", live.slug, live.market_topic_id)
        store_round(store, live)

        seen = {r["market_topic_id"] for r in store.query(
            "SELECT market_topic_id FROM rounds WHERE market_topic_id IS NOT NULL")}
        cursor = min(seen) if seen else live.market_topic_id
        settled = store.query(
            "SELECT COUNT(*) c FROM rounds WHERE winner IS NOT NULL")[0]["c"]
        log.info("starting at topic %d, %d settled rounds already stored", cursor, settled)

        misses = 0
        t0 = time.time()
        while settled < args.rounds and misses < 8:
            ids = [i for i in range(cursor - 1, cursor - 1 - args.batch, -1)
                   if i not in seen]
            if not ids:
                cursor -= args.batch
                continue
            results = await asyncio.gather(*(_probe(feed, i, sem) for i in ids))
            hits = 0
            for tid, r in results:
                seen.add(tid)
                if r and r.slug.startswith("btc-updown-5m"):
                    store_round(store, r)
                    hits += 1
            cursor -= args.batch
            misses = misses + 1 if hits == 0 else 0
            settled = store.query(
                "SELECT COUNT(*) c FROM rounds WHERE winner IS NOT NULL")[0]["c"]
            log.info("swept to %d | +%d hits | %d settled | %.0fs",
                     cursor, hits, settled, time.time() - t0)

    rows = store.query(
        "SELECT MIN(start_ms) a, MAX(start_ms) b, COUNT(*) c FROM rounds "
        "WHERE winner IS NOT NULL")[0]
    if rows["c"]:
        span_h = (rows["b"] - rows["a"]) / 3_600_000
        log.info("done: %d settled rounds spanning %.1f hours", rows["c"], span_h)
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
