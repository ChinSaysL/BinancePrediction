"""Live order book for the prediction market, over the venue's WEBSOCKET.

WHY. `self._asks` was refreshed once per ~1s poll by two REST calls, and the
venue completes 66% of its post-cross repricing INSIDE that second (measured
2026-09-13, n=189: pre-cross poll 0.505 -> cross tick 0.618 -> +4s 0.645). So
the cross arm gated on a price that no longer existed -- it logged "@0.350"
and filled at 0.656, which reads as a 30c execution failure but is really the
gate quoting a stale tape.

Measured on this feed against the same market: median 250ms between updates
(p10 111ms, min 37ms), ZERO gaps over 1s, and venue->us latency median 89ms.
That is 4x the resolution of the poll it replaces.

WHAT IT DOES NOT FIX. Placement still costs two REST round trips (the venue
requires get_quote -> quoteId -> POST), so this does not make the fill cheaper
on its own. What it buys is an honest gate, depth-aware sizing, and -- for the
first time -- the ability to see inside the one second that decides this arm.

THE BOOK IS THE `Up` TOKEN. Verified live 2026-09-13 against REST for the same
market and instant: WS bid/ask matched the Up book exactly (distance 0.000)
while the Down book sat 0.140 away. The complete-set identity then gives the
other side, and it holds on 99.5% of 126k recorded quotes:
    down_ask = 1 - up_bid      down_bid = 1 - up_ask

DOCS ERRATA (verified live; the published docs are wrong on all three):
  * the aggregated topic `web3_prediction_orderbook_data` registers with
    SUCCESS and then pushes NOTHING. Only per-market topics deliver.
  * frames arrive as type "DATA", not the documented "TOPIC".
  * prices and sizes are JSON numbers, not the documented strings.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger(__name__)

WS_URL = "wss://api.binance.com/sapi/wss"
TOPIC_FMT = "web3_prediction_orderbook_{}"


@dataclass
class BookTop:
    """Top of book, plus the LEVELS behind it.

    The levels are not a luxury. Measured live 2026-09-13, a real Down book:
        asks [(0.09, 1.09), (0.10, 26.11), (0.11, 1.12)]
    -- the touch held $0.10 while the level behind it held $2.61. An order
    priced off the touch alone is priced off almost nothing, and that is
    exactly how a cross gated at 0.760 filled at 0.9008: it swept to the limit.
    """
    up_bid: float
    up_ask: float
    up_ask_size: float
    up_bid_size: float
    ts_ms: int                       # the venue's updateTimestampMs
    recv_ms: int                     # when we saw it
    # (price, size) ascending for asks, descending for bids. Up-token book.
    asks: tuple = ()
    bids: tuple = ()

    @property
    def down_ask(self) -> float:
        return round(1.0 - self.up_bid, 4)

    @property
    def down_bid(self) -> float:
        return round(1.0 - self.up_ask, 4)

    def age_s(self, now_ms: int | None = None) -> float:
        return ((now_ms or int(time.time() * 1000)) - self.recv_ms) / 1000.0

    def ask(self, side: str) -> float:
        return self.up_ask if side == "Up" else self.down_ask

    def ask_dollars(self, side: str) -> float:
        """$ resting at the TOUCH on `side`.

        Buying Down consumes the Up BID queue (a Down buy is an Up sell), so
        the size that matters flips with the side. Verified live against the
        venue's own Down book: price AND size mirror exactly.
        """
        return (self.up_ask * self.up_ask_size if side == "Up"
                else (1.0 - self.up_bid) * self.up_bid_size)

    def levels(self, side: str) -> list[tuple[float, float]]:
        """(price, shares) a buyer of `side` would consume, cheapest first.

        A Down buy walks the Up BID queue downward, and each Up bid at p is a
        Down offer at 1-p -- so descending Up bids become ASCENDING Down
        prices, which is the order a buyer consumes them in.
        """
        if side == "Up":
            return [(float(p), float(z)) for p, z in self.asks]
        return [(round(1.0 - float(p), 4), float(z)) for p, z in self.bids]

    def sweep(self, side: str, stake: float) -> tuple[float, float]:
        """(average price, dollars fillable) for spending `stake` on `side`.

        THE number the arm should gate on. Gating on the touch prices an order
        against liquidity that is often a few cents wide: a cross gated at
        0.760 filled at 0.9008 because $1.09 ate through to the limit. Returns
        the volume-weighted price actually payable, and how much of `stake`
        the visible book can absorb.
        """
        spend = 0.0
        shares = 0.0
        for px, sz in self.levels(side):
            if px <= 0 or sz <= 0:
                continue
            room = stake - spend
            if room <= 0:
                break
            take = min(px * sz, room)
            spend += take
            shares += take / px
        if shares <= 0:
            return 0.0, 0.0
        return spend / shares, spend


@dataclass
class PredictionBook:
    """Latest top-of-book per marketId, kept current by one WS connection."""
    api_key: str
    api_secret: str
    session: aiohttp.ClientSession
    signer: object                       # a LiveBroker: reuse its _sign
    tops: dict[int, BookTop] = field(default_factory=dict)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _markets: tuple[int, ...] = ()
    _resub: asyncio.Event = field(default_factory=asyncio.Event)
    connected: bool = False
    messages: int = 0

    def best(self, market_id: int | None, max_age_s: float) -> BookTop | None:
        """The book for `market_id`, or None if missing or stale.

        Staleness is not an edge case here: a silently frozen feed would price
        every order off a dead book, which is strictly worse than the REST poll
        this replaces. Callers fall back rather than wait.
        """
        if market_id is None:
            return None
        t = self.tops.get(int(market_id))
        if t is None or t.age_s() > max_age_s:
            return None
        return t

    #: Most topics to hold at once. Six markets, each with a live round and a
    #: next one, plus slack.
    MAX_TOPICS = 16

    def follow(self, market_ids: list[int]) -> None:
        """Subscribe to these markets, ADDITIVELY.

        The topic is a CONNECT-TIME query parameter, so any change to the set
        costs a handshake. Comparing the set for equality -- which is what this
        did -- was fine while one market was followed and became a live
        regression the moment six were: markets roll at different times, so on
        most polls one id drops out and returns, and the feed re-handshaked
        every 10-40s (63 reconnects in the first three minutes), leaving the
        book stale for every market including the one that trades.

        A market leaving the poll needs no action: its topic simply stops
        pushing. So only ADDITIONS resubscribe, and the set is trimmed (oldest
        first) only when it would exceed MAX_TOPICS.
        """
        ids = [int(m) for m in market_ids if m]
        if not ids:
            return
        cur = list(self._markets)
        added = [m for m in ids if m not in cur]
        if not added:
            return
        merged = cur + added
        if len(merged) > self.MAX_TOPICS:
            merged = merged[-self.MAX_TOPICS:]
        self._markets = tuple(merged)
        self._resub.set()

    def stop(self) -> None:
        self._stop.set()
        self._resub.set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if not self._markets:
                await asyncio.sleep(1.0)
                continue
            mids = self._markets
            topic = "|".join(TOPIC_FMT.format(m) for m in mids)
            qs = self.signer._sign({"random": secrets.token_hex(8),
                                    "topic": topic})
            try:
                async with self.session.ws_connect(
                    f"{WS_URL}?{qs}",
                    headers={"X-MBX-APIKEY": self.api_key},
                    heartbeat=30.0, timeout=20,
                ) as ws:
                    self.connected = True
                    backoff = 1.0
                    self._resub.clear()
                    log.info("prediction book ws connected: %s", list(mids))
                    # NOT `async for msg in ws`: that blocks until a frame
                    # arrives, and a subscribed market goes SILENT the moment
                    # its round ends. The resubscribe check would then never
                    # run and the feed would sit on a dead topic forever --
                    # every arm silently reverting to the 1s REST poll with no
                    # error anywhere. Poll the socket instead so the loop stays
                    # responsive to `follow()` through the quiet.
                    while not (self._stop.is_set() or self._resub.is_set()):
                        try:
                            msg = await asyncio.wait_for(ws.receive(),
                                                         timeout=1.0)
                        except asyncio.TimeoutError:
                            continue                  # quiet: re-check _resub
                        if msg.type in (aiohttp.WSMsgType.CLOSED,
                                        aiohttp.WSMsgType.CLOSING,
                                        aiohttp.WSMsgType.ERROR):
                            break
                        if msg.type is not aiohttp.WSMsgType.TEXT:
                            continue
                        self._ingest(msg.data)
            except Exception as e:                       # noqa: BLE001
                log.warning("prediction book ws dropped (%s: %s); retry in %.0fs",
                            type(e).__name__, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self.connected = False

    def _ingest(self, raw: str) -> None:
        try:
            env = json.loads(raw)
            if env.get("type") != "DATA":
                return                                    # COMMAND/REGISTER
            d = env.get("data")
            d = json.loads(d) if isinstance(d, str) else d
            if not isinstance(d, dict) or d.get("msgType") != "orderbook":
                return
            asks, bids = d.get("asks") or [], d.get("bids") or []
            if not asks or not bids:
                return                                    # one-sided: unusable
            mid = int(d["marketId"])
            self.tops[mid] = BookTop(
                up_bid=float(bids[0][0]), up_ask=float(asks[0][0]),
                up_ask_size=float(asks[0][1]), up_bid_size=float(bids[0][1]),
                ts_ms=int(d.get("updateTimestampMs") or 0),
                recv_ms=int(time.time() * 1000),
                asks=tuple((float(p), float(z)) for p, z in asks[:10]),
                bids=tuple((float(p), float(z)) for p, z in bids[:10]))
            self.messages += 1
        except (ValueError, TypeError, KeyError, IndexError):
            return                                        # never kill the loop
