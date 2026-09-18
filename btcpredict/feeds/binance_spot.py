"""Binance spot price feed.

The prediction market settles on a Chainlink Data Streams BTC/USD feed, but
measurement against harvested rounds shows that feed tracks Binance spot to
well under a cent at round boundaries, so spot is a sound intra-round proxy.
The residual basis is still recorded every poll so the assumption stays testable.

A WebSocket bookTicker gives sub-second mids; REST is the fallback and the
seed. `price` never blocks, so callers must check `age_s`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque

import aiohttp

from btcpredict.config import SPOT_REST, SPOT_WS

log = logging.getLogger(__name__)


class SpotFeed:
    def __init__(self, session: aiohttp.ClientSession, symbol: str = "BTCUSDT"):
        self.session = session
        self.symbol = symbol
        self._price: float | None = None
        self._ts_ms: int = 0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Every update in arrival order, with the clock reading at which we saw
        # it. The impulse trigger reads THIS series rather than the once-a-second
        # poll sample: the venue is 5 ms away, so sampling the price only when
        # the poll wakes is what put a 500 ms median between the move and the
        # order.
        #
        # 40,000, NOT 4,000 (2026-09-17). The depth is a WINDOW on the recent
        # past, and the Polymarket strike backfill reads it for the price at a
        # round's START INSTANT -- which must be a tick AT OR BEFORE that
        # instant. At BTC's measured 114 updates/s, 4,000 ticks is 35 seconds,
        # so any round discovered more than ~35 s after it opened lost its
        # pre-open ticks and could NEVER be priced (`sol/xrp/doge` sat at
        # `not live` exactly this way). 40,000 is ~5 minutes at that rate and
        # ~15 minutes at 45/s -- one round plus the discovery slack -- for
        # ~2 MB per symbol of tuples.
        self.ticks: deque[tuple[int, float]] = deque(maxlen=40_000)
        # Set on every update so the decision loop can wake on the price itself.
        self.tick_event = asyncio.Event()

    @property
    def price(self) -> float | None:
        return self._price

    @property
    def ts_ms(self) -> int:
        return self._ts_ms

    @property
    def age_s(self) -> float:
        if not self._ts_ms:
            return float("inf")
        return max(0.0, time.time() - self._ts_ms / 1000.0)

    def is_fresh(self, max_age_s: float) -> bool:
        return self._price is not None and self.age_s <= max_age_s

    def _publish(self) -> None:
        """Record the update and wake anything waiting on the price."""
        if self._price is None:
            return
        self.ticks.append((self._ts_ms, self._price))
        self.tick_event.set()

    async def wait_tick(self, timeout: float) -> bool:
        """Wait for the next price update; True if one arrived.

        Clears first, then waits: the caller re-reads the whole buffer
        afterwards, so an update that landed while it was busy is not lost --
        it is already in `ticks`. Any side effect of a cleared event is bounded
        by the next update, which on this stream is ~30 ms away.
        """
        self.tick_event.clear()
        try:
            await asyncio.wait_for(self.tick_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def refresh_rest(self) -> float | None:
        """One-shot REST read. Used to seed the feed and as a fallback."""
        url = f"{SPOT_REST}/api/v3/ticker/bookTicker?symbol={self.symbol}"
        try:
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                d = await r.json(content_type=None)
            bid, ask = float(d["bidPrice"]), float(d["askPrice"])
            self._price = (bid + ask) / 2.0
            self._ts_ms = int(time.time() * 1000)
            self._publish()
            return self._price
        except Exception as e:
            log.warning("spot REST failed: %s: %s", type(e).__name__, e)
            return None

    async def _run_ws(self) -> None:
        stream = f"{SPOT_WS}/{self.symbol.lower()}@bookTicker"
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with self.session.ws_connect(
                    stream, heartbeat=20, timeout=aiohttp.ClientTimeout(total=15)
                ) as ws:
                    log.info("spot ws connected: %s", self.symbol)
                    backoff = 1.0
                    async for msg in ws:
                        if self._stop.is_set():
                            break
                        if msg.type is not aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            d = json.loads(msg.data)
                            bid, ask = float(d["b"]), float(d["a"])
                        except (ValueError, KeyError, TypeError):
                            continue
                        self._price = (bid + ask) / 2.0
                        self._ts_ms = int(time.time() * 1000)
                        self._publish()
            except Exception as e:
                if self._stop.is_set():
                    break
                log.warning("spot ws dropped (%s: %s), retry in %.0fs",
                            type(e).__name__, e, backoff)
            if self._stop.is_set():
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, 30.0)

    async def start(self) -> None:
        await self.refresh_rest()
        self._task = asyncio.create_task(self._run_ws())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


async def fetch_klines(
    session: aiohttp.ClientSession, interval: str, limit: int,
    symbol: str = "BTCUSDT", end_ms: int | None = None,
) -> list[list]:
    url = f"{SPOT_REST}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    if end_ms:
        url += f"&endTime={end_ms}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        return await r.json(content_type=None)
