"""Record the venue's prediction-market order book and our spot, raw, side by side.

Why this exists: the REST order-book endpoint returns nothing in the last ~6
seconds of a round, so every measurement built on `quotes.up_ask/down_ask` is
blind exactly where the round is decided -- and the venue *pushes* its book over a
websocket (`wss://api.binance.com/sapi/wss`, topic web3_prediction_orderbook_*,
<200 ms after the upstream event), which this codebase never used. Three
questions are blocked on it: the sub-second book/spot lead-lag, whether the book
lags a strike crossing by more than the 2c a 1 Hz poll can see, and whether the
last seconds misprice the cheap tail (the "$1 -> $90" trade, which loses 91c per
dollar everywhere the 1 Hz tape can see).

The messages are stored RAW. The published example shows one ask/bid ladder per
marketId, but the REST endpoint is per tokenId, so which of the two outcomes a
message describes is not documented -- and guessing it wrong is exactly the kind
of silent error this project keeps finding. Decode from the recorded data.

Writes to its own SQLite file: read-only with respect to the venue and to the
bot's database.

    .venv/bin/python -m tools.record_book --seconds 3600 --out /root/pmws/book.db
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import pathlib
import random
import sqlite3
import time

import aiohttp

from btcpredict.config import MARKETS, SPOT_WS, settings
from btcpredict.execution.live import LiveBroker

log = logging.getLogger("record_book")

WS_BASE = "wss://api.binance.com/sapi/wss"
TOPIC_ALL = "web3_prediction_orderbook_data"      # aggregated: every market
SCHEMA = """
CREATE TABLE IF NOT EXISTS book_raw (
    ts_ms   INTEGER NOT NULL,     -- OUR clock, when the frame arrived
    topic   TEXT,
    raw     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spot_ticks (
    ts_ms   INTEGER NOT NULL,     -- OUR clock
    bid     REAL, ask REAL, mid REAL
);
CREATE INDEX IF NOT EXISTS book_raw_ts ON book_raw(ts_ms);
CREATE INDEX IF NOT EXISTS spot_ticks_ts ON spot_ticks(ts_ms);
CREATE TABLE IF NOT EXISTS marks (k TEXT PRIMARY KEY, v TEXT);
"""


class Recorder:
    def __init__(self, db: sqlite3.Connection, session: aiohttp.ClientSession):
        self.db = db
        self.session = session
        # Same construction as the bot: the wallet address/id are not in .env,
        # they are discovered from the account (read-only) before signing.
        self.broker = LiveBroker(session, settings.binance_api_key,
                                 settings.binance_api_secret)
        self.book_msgs = 0
        self.spot_msgs = 0
        self.last_log = time.time()

    # -- spot ---------------------------------------------------------------
    async def spot(self) -> None:
        url = f"{SPOT_WS}/btcusdt@bookTicker"
        while True:
            try:
                async with self.session.ws_connect(url, heartbeat=20) as ws:
                    log.info("spot ws connected")
                    async for msg in ws:
                        if msg.type is not aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            d = json.loads(msg.data)
                            bid, ask = float(d["b"]), float(d["a"])
                        except (ValueError, KeyError, TypeError):
                            continue
                        self.db.execute(
                            "INSERT INTO spot_ticks VALUES (?,?,?,?)",
                            (int(time.time() * 1000), bid, ask, (bid + ask) / 2))
                        self.spot_msgs += 1
                        if self.spot_msgs % 200 == 0:
                            self.db.commit()
                        self.maybe_log()
            except Exception as e:                              # noqa: BLE001
                log.warning("spot ws: %s: %s -- reconnecting in 2s",
                            type(e).__name__, e)
                await asyncio.sleep(2.0)

    # -- venue book ---------------------------------------------------------
    def url(self) -> str:
        """Same SAPI signing as the REST client: sort, HMAC, append.

        `apikey` must be IN the query string: a websocket has no
        X-MBX-APIKEY header, and the venue answers a signed URL without it with
        `{"type":"COMMAND","data":"Parameter 'apikey' was empty."}`.
        """
        params = {"apikey": settings.binance_api_key,
                  "random": random.randint(100000, 999999),
                  "topic": TOPIC_ALL,
                  "recvWindow": 30000,
                  "timestamp": int(time.time() * 1000)}
        return f"{WS_BASE}?{self.broker._sign(params)}"

    async def book(self) -> None:
        if not self.broker.wallet_address or not self.broker.wallet_id:
            if not await self.broker.discover_wallet():
                log.error("no prediction wallet discoverable; the orderbook "
                          "topic cannot be signed")
                return
        while True:
            try:
                # The key goes in the X-MBX-APIKEY header, exactly as every REST
                # call in this codebase authenticates; `apikey` also rides in the
                # signed query for completeness. Without either, the venue
                # accepts the socket and answers
                # {"type":"COMMAND","data":"Parameter 'apikey' was empty."}
                async with self.session.ws_connect(
                        self.url(), heartbeat=20,
                        headers={"X-MBX-APIKEY": settings.binance_api_key}) as ws:
                    log.info("prediction-book ws connected (topic=%s)", TOPIC_ALL)
                    async for msg in ws:
                        if msg.type is not aiohttp.WSMsgType.TEXT:
                            if msg.type in (aiohttp.WSMsgType.CLOSED,
                                            aiohttp.WSMsgType.ERROR):
                                break
                            continue
                        ts = int(time.time() * 1000)
                        self.db.execute("INSERT INTO book_raw VALUES (?,?,?)",
                                        (ts, TOPIC_ALL, msg.data))
                        self.book_msgs += 1
                        if self.book_msgs % 50 == 0:
                            self.db.commit()
                        self.maybe_log()
                # A connection that opens and closes at once is the venue
                # rejecting us, not a network drop: back off so the log shows
                # the reason instead of a reconnect spin.
                await asyncio.sleep(2.0)
            except Exception as e:                              # noqa: BLE001
                log.warning("book ws: %s: %s -- reconnecting in 5s",
                            type(e).__name__, e)
                await asyncio.sleep(5.0)

    def maybe_log(self) -> None:
        if time.time() - self.last_log >= 60:
            self.last_log = time.time()
            log.info("recorded: spot %d, book %d (%.1f min in)",
                     self.spot_msgs, self.book_msgs,
                     (time.time() - self.started) / 60)

    async def run(self, seconds: float) -> None:
        self.started = time.time()
        tasks = [asyncio.create_task(self.spot()),
                 asyncio.create_task(self.book())]
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.Event().wait(), timeout=seconds)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def amain() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=3600.0)
    ap.add_argument("--out", default="/root/pmws/book.db")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    path = pathlib.Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)

    async with aiohttp.ClientSession() as session:
        rec = Recorder(db, session)
        log.info("markets under watch: %s", [p for p, _, _ in MARKETS])
        try:
            await rec.run(args.seconds)
        finally:
            db.commit()
            db.execute("INSERT OR REPLACE INTO marks VALUES ('last_run', ?)",
                       (json.dumps({"spot": rec.spot_msgs,
                                    "book": rec.book_msgs,
                                    "at": int(time.time() * 1000)}),))
            db.commit()
            log.info("done: spot %d, book %d", rec.spot_msgs, rec.book_msgs)
            db.close()


if __name__ == "__main__":
    asyncio.run(amain())
