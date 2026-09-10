"""Backfill 1m bars (with taker-buy volume) from Binance into the store.

The quotes table can only reconstruct close+volume, so the order-flow models
(sentiment, hybrid) cannot be replayed without this. Binance serves the history,
so any window can be filled after the fact instead of waiting days to collect it.

Usage:
    python -m tools.backfill_bars                 # cover the recorded window
    python -m tools.backfill_bars --symbol BTCUSDT --hours 48
"""
from __future__ import annotations

import argparse
import asyncio
import time

import aiohttp

from btcpredict.config import settings
from btcpredict.feeds.binance_spot import fetch_klines
from btcpredict.store.db import Store

STEP_MS = 1_000 * 60          # 1m bars
BATCH = 1000                  # Binance's per-request kline limit


def parse(k: list) -> tuple[int, float, float, float]:
    return (int(k[0]), float(k[4]), float(k[5]), float(k[9]))


async def backfill(symbol: str, start_ms: int, end_ms: int, store: Store) -> int:
    total, cursor = 0, start_ms
    async with aiohttp.ClientSession() as session:
        while cursor < end_ms:
            # Ask for the batch ending just before the cursor window's end so
            # each response is a contiguous, non-overlapping slice.
            batch_end = min(end_ms, cursor + BATCH * STEP_MS)
            klines = await fetch_klines(session, "1m", BATCH, symbol=symbol,
                                        end_ms=batch_end)
            if not klines:
                break
            rows = [parse(k) for k in klines]
            total += store.upsert_bars(symbol, rows)
            last = max(r[0] for r in rows)
            if last <= cursor:
                break
            cursor = last + STEP_MS
            await asyncio.sleep(0.25)      # be polite to the REST endpoint
    return total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--hours", type=float, default=0.0,
                    help="how far back to fill (0 = cover the quotes table)")
    ap.add_argument("--start-ms", type=int, default=0)
    ap.add_argument("--end-ms", type=int, default=0)
    args = ap.parse_args()

    store = Store(settings.db_path)
    if args.start_ms and args.end_ms:
        start, end = args.start_ms, args.end_ms
    elif args.hours:
        end = int(time.time() * 1000)
        start = end - int(args.hours * 3_600_000)
    else:
        row = store.query("SELECT MIN(ts_ms) a, MAX(ts_ms) b FROM quotes")[0]
        start, end = row["a"], row["b"]
    print(f"backfilling {args.symbol} from "
          f"{time.strftime('%m-%d %H:%M', time.gmtime(start / 1000))} to "
          f"{time.strftime('%m-%d %H:%M', time.gmtime(end / 1000))}")
    n = asyncio.run(backfill(args.symbol, start, end, store))
    got = len(store.bars_since(args.symbol, 0))
    print(f"upserted {n} bars; {got} rows now stored for {args.symbol}")


if __name__ == "__main__":
    main()
