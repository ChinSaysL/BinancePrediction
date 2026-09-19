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
  * and MULTIPLE per-market topics joined with "|" on ONE connection behave the
    same way: the venue answers COMMAND/REGISTER SUCCESS and then delivers
    exactly one of them. Measured 2026-09-14 -- six families subscribed on one
    socket produced asks for btc-updown-5m only and ZERO for the other five,
    for 40 minutes, with no error anywhere; a probe of the SAME eth-updown-5m
    market as a lone topic got 94 frames in 30 seconds. Hence one connection
    per market below.
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

    @property
    def lag_ms(self) -> float | None:
        """venue clock -> our clock, in ms. The DELIVERY latency.

        ⛔ NOT `age_s`, and the difference is the whole point. `age_s` measures
        `now - recv_ms` -- how long since OUR LAST FRAME -- so it says nothing
        about how long the frame took to arrive and is near zero on a busy
        book however far away the venue is. This measures `recv_ms - ts_ms`,
        which is the part geography owns.

        Until now the only samples of it were the ones the lag ALARM logged,
        and a median taken over those (599 ms, n=1104) is conditioned on the
        alarm having fired. That number was quoted as the feed's typical
        latency -- including in `cross_ladder_band_pad`'s own justification --
        which it never was. Recording it per quote is what settles it.

        Carries the venue's clock against ours, so a constant NTP skew shifts
        it. The SHAPE and the differences between hosts survive that; a single
        absolute value does not.
        """
        if self.ts_ms and self.recv_ms and self.ts_ms > 0:
            return float(self.recv_ms - self.ts_ms)
        return None

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
    """Latest top-of-book per marketId, one WS CONNECTION PER MARKET.

    It was one connection carrying every market as pipe-joined topics, which
    the venue accepts and then silently honours for a single market (see the
    errata above). That made a six-family widening completely inert: five of
    six carried no ask at all and nothing logged an error.
    """
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
    #: market id -> the last time follow() asked for it
    _seen: dict[int, float] = field(default_factory=dict)
    #: market id -> its connection task
    _tasks: dict[int, asyncio.Task] = field(default_factory=dict)
    #: market ids with an established socket right now
    _live: set = field(default_factory=set)

    @staticmethod
    def _as_market_id(x) -> int | None:
        """A Round or a raw marketId -> the integer marketId, or None."""
        if x is None:
            return None
        if isinstance(x, int):
            return x
        mid = getattr(x, "market_id", None)
        try:
            return int(mid) if mid is not None else None
        except (TypeError, ValueError):
            return None

    def best(self, round_or_id, max_age_s: float) -> BookTop | None:
        """The book for a Round (preferred) or a raw marketId.

        BOTH forms are accepted. The arms pass the ROUND they are pricing,
        because Polymarket addresses a market by its slug and its two outcomes
        have separate books, so the wrapper's integer id does not exist there
        -- a Round is the one argument that means the same thing on either
        venue. `tops` itself stays keyed by marketId.

        Staleness is not an edge case here: a silently frozen feed would price
        every order off a dead book, which is strictly worse than the REST poll
        this replaces. Callers fall back rather than wait.
        """
        mid = self._as_market_id(round_or_id)
        if mid is None:
            return None
        t = self.tops.get(mid)
        if t is None or t.age_s() > max_age_s:
            return None
        return t

    #: Most topics to hold at once. Six markets, each with a live round and a
    #: next one, plus slack.
    MAX_TOPICS = 16

    #: Most markets to hold sockets for at once. Six families with a live round
    #: is six; the headroom absorbs a roll where the old and new round overlap.
    MAX_CONNECTIONS = 10
    #: How long a market keeps its socket after follow() stops asking for it.
    #: A market mid-roll drops out of the poll for a beat, and tearing its
    #: connection down and back up each time is the churn the old additive
    #: `follow` existed to avoid.
    CLOSE_AFTER_ABSENT_S = 90.0

    def follow(self, rounds_or_ids) -> None:
        """Declare the markets that should have a live book.

        AUTHORITATIVE (with a grace period), where this used to be purely
        additive. That additive design was not a preference -- the topic was a
        CONNECT-TIME parameter of the single shared socket, so any change to
        the set re-handshaked EVERY market (63 reconnects in three minutes when
        six were followed). With one socket per market that constraint is gone:
        adding or dropping one market touches only its own connection, so a
        round that has rolled can now actually release its slot instead of
        holding one until MAX_TOPICS evicted it.
        """
        now = time.time()
        added = False
        for x in rounds_or_ids:
            m = self._as_market_id(x)
            if not m:
                continue
            if m not in self._seen:
                added = True
            self._seen[m] = now
        if added:
            self._resub.set()

    def stop(self) -> None:
        self._stop.set()
        self._resub.set()

    def _wanted(self) -> set:
        """Markets that should hold a socket right now."""
        now = time.time()
        want = {m for m, ts in self._seen.items()
                if now - ts <= self.CLOSE_AFTER_ABSENT_S}
        for m in [m for m, ts in self._seen.items()
                  if now - ts > self.CLOSE_AFTER_ABSENT_S]:
            del self._seen[m]
        if len(want) > self.MAX_CONNECTIONS:
            # keep the most recently asked-for
            want = set(sorted(want, key=lambda m: self._seen[m],
                              reverse=True)[:self.MAX_CONNECTIONS])
        return want

    def _reconcile(self) -> None:
        want = self._wanted()
        for m, t in list(self._tasks.items()):
            if m not in want or t.done():
                self._tasks.pop(m, None)
                self._live.discard(m)
                if not t.done():
                    t.cancel()
        for m in want:
            if m not in self._tasks:
                self._tasks[m] = asyncio.create_task(self._run_one(m))

    async def run(self) -> None:
        """Supervise one connection per followed market."""
        try:
            while not self._stop.is_set():
                self._reconcile()
                try:
                    await asyncio.wait_for(self._resub.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                self._resub.clear()
        finally:
            for t in self._tasks.values():
                t.cancel()
            self._tasks.clear()
            self._live.clear()
            self.connected = False

    async def _run_one(self, mid: int) -> None:
        """One market, one socket, reconnecting with backoff until cancelled."""
        backoff = 1.0
        while not self._stop.is_set():
            qs = self.signer._sign({"random": secrets.token_hex(8),
                                    "topic": TOPIC_FMT.format(mid)})
            try:
                async with self.session.ws_connect(
                    f"{WS_URL}?{qs}",
                    headers={"X-MBX-APIKEY": self.api_key},
                    heartbeat=30.0, timeout=20,
                ) as ws:
                    self._live.add(mid)
                    self.connected = True
                    backoff = 1.0
                    log.info("book ws connected: %s (%d live)", mid,
                             len(self._live))
                    while not self._stop.is_set():
                        # NOT `async for msg in ws`: a market goes SILENT the
                        # moment its round ends, and a blocking iterator would
                        # never notice cancellation through the quiet.
                        try:
                            msg = await asyncio.wait_for(ws.receive(),
                                                         timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                        if msg.type in (aiohttp.WSMsgType.CLOSED,
                                        aiohttp.WSMsgType.CLOSING,
                                        aiohttp.WSMsgType.ERROR):
                            break
                        if msg.type is not aiohttp.WSMsgType.TEXT:
                            continue
                        self._ingest(msg.data)
            except asyncio.CancelledError:
                raise
            except Exception as e:                       # noqa: BLE001
                log.warning("book ws %s dropped (%s: %s); retry in %.0fs",
                            mid, type(e).__name__, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self._live.discard(mid)
                self.connected = bool(self._live)

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
