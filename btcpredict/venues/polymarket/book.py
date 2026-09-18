"""Polymarket CLOB book -> the same `BookTop` shape the Binance feed produced.

WHY A SEPARATE FEED, AND WHAT IT MEASURED
-----------------------------------------
The Binance prediction wrapper's book was 1 tick wide with $1-3 resting at the
touch, FOK-only matching, and a 249 ms venue hold. Polymarket's is a real CLOB:
verified 2026-09-16 on `btc-updown-5m`, the touch had **thousands of shares**
resting at the extreme levels and a 0.01 spread, with a public, unauthenticated
book endpoint.

Two venue facts that differ from the wrapper and that the caller must respect:

1. **The two tokens have their OWN books.** The Binance wrapper exposed one book
   with an exact complement (`down_ask = 1 - up_bid`, verified there). Here the
   Down token is a separate instrument, so buying Down pays *its own* ask, which
   is not guaranteed to equal `1 - up_bid`. This feed therefore keys books by
   ASSET ID and never derives one side from the other. A `Touch` is that
   token's own best bid/ask, and `up_bid`/`up_ask` on the `BookTop` below mean
   "this token's bid/ask" -- the complement properties on `BookTop` are NOT
   valid on this path.
2. **Minimum order size is 5 SHARES**, not $1 (`orderMinSize` 5 in gamma,
   `minimum_order_size` 5 in the CLOB record). At 0.82 that is $4.10. A $1
   stake is not placeable on this venue at all -- the caller must size from the
   venue floor up, not from the bankroll down.

Message rate is the other thing to respect: measured **8124 messages in 12 s**
(~680/s) for two assets, versus ~114/s on the wrapper. The ingest therefore
keeps the TOUCH always (every `price_change` carries `best_bid`/`best_ask`) and
only keeps top-N LEVELS from snapshots; nothing is recomputed per message that
does not have to be.

⛔ AND THE RATE IS WHAT OOM-KILLED THE BOT, so read this before touching the
receive loop (measured 2026-09-17). With all nine families followed (18 assets)
the rate is **24,988 messages / 20.0 s = 1,249/s** (0.79 MB/s, p50 0.6 kB, max
51 kB) -- one message per `price_change`, not one per asset. The loop used to
wrap EVERY message in `asyncio.wait_for(ws.receive(), timeout=5.0)`, and a
py-spy profile of the ballooning process reads **115 samples inside
`asyncio/tasks.py` (wait_for) against 9 inside `_ingest`**: the consumer was
building a Task and a timer per message instead of applying the book, so it fell
behind. aiohttp's reader then held the difference as objects, RSS grew at a
measured **~67 MB/s** (1.38 -> 1.70 -> 2.05 GB in ten seconds), and the cgroup
OOM killer took the process at its 2 GB cap --
`anon-rss:2088220kB, constraint=CONSTRAINT_MEMCG,
oom_memcg=/system.slice/btcpredict.service` -- every ~4 minutes, which is a
restart loop that looks like a crash for no reason.
THE FIX, and do not undo it: a BARE `receive()`. Liveness is `heartbeat=20.0`
(aiohttp closes the socket when pings go unanswered, which surfaces as an
exception out of `receive()`), NOT a per-message timer. `max_backlog` +
`_drain()` sit on top as a ceiling on what one slow tick can cost.

Protocol, as observed (do not edit from memory -- re-probe if this breaks):
    url  wss://ws-subscriptions-clob.polymarket.com/ws/market
    sub  {"assets_ids": ["<token>", "<token>"], "type": "market"}
    ->   {"event_type": "book", "asset_id": ..., "timestamp": "1789575965396",
          "hash": ..., "bids": [{"price": "0.01", "size": "11360.15"}, ...],
          "asks": [...]}                                    # full snapshot
         {"event_type": "price_change", "price_changes": [
            {"asset_id": ..., "price": "0.01", "size": "11355.15",
             "side": "BUY"|"SELL", "best_bid": "0.53", "best_ask": "0.54"}],
          "timestamp": ...}
         {"event_type": "last_trade_price", ...}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BOOK_URL = "https://clob.polymarket.com/book?token_id=%s"

#: The venue answers a default client agent with 403 on several endpoints.
#: Sent on every REST read here so a blocked request can never be mistaken for
#: an empty book.
UA = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json",
}

#: Levels kept per side for the depth gates (`sweep`, `ask_dollars`). The
#: snapshot carries the whole ladder; keeping 10 matches what the wrapper's
#: BookTop held so the same gates see a comparable object.
KEEP_LEVELS = 10


def _f(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Touch:
    """One token's top of book, at the moment we received it.

    `bid_size`/`ask_size` are SHARES resting at the touch -- the caller sizes
    against these, because on this venue the minimum is in shares.
    """

    asset_id: str
    bid: float
    bid_size: float
    ask: float
    ask_size: float
    ts_ms: int                 # the venue's own timestamp
    recv_ms: int               # when we saw it
    bids: tuple = ()           # (price, size) descending
    asks: tuple = ()           # (price, size) ascending

    def age_s(self, now_ms: int | None = None) -> float:
        return ((now_ms or int(time.time() * 1000)) - self.recv_ms) / 1000.0

    def ask_dollars(self) -> float:
        return self.ask * self.ask_size

    def levels(self) -> list[tuple[float, float]]:
        """Ask levels a taker can still hit, best first.

        ⛔ THE FIRST LINE OF THIS FUNCTION IS THE FIX FOR THE GATE->FILL GAP
        (measured live 2026-09-17). `ask` is the venue's OWN best ask, carried
        on every `price_change`, so by definition nothing rests BELOW it -- a
        level below it has already been taken. The ladder, by contrast, is
        built from a `book` snapshot. Pricing a sweep off a ladder that still
        holds a level under the touch quotes a book that is gone, and the
        order that follows cannot fill. Live, the cross arm logged
            decided 0.4000 limit 0.42 -> NO FILL in 0.93s | book now 0.6000
        and the venue answered `no orders found to match`.

        The touch is therefore pinned explicitly rather than trusted to be
        `asks[0]`, and every level at or below it is dropped.
        """
        if self.ask <= 0:
            return []
        deep = [lv for lv in self.asks if lv[1] > 0 and lv[0] > self.ask + 1e-9]
        if self.ask_size > 0:
            return [(self.ask, self.ask_size), *deep]
        # Touch size unknown (an empty ladder, or a delta that never landed on
        # the touch): the `deep` levels are all ABOVE the touch, so a sweep
        # over them prices HIGH -- conservative, and never optimistic.
        return deep

    def sweep(self, stake: float) -> tuple[float, float]:
        """(volume-weighted price, dollars fillable) for spending `stake`.

        Same contract as `BookTop.sweep` on the wrapper, and the same reason:
        gating on the touch prices an order against liquidity that may be one
        tick wide. On this venue the levels behind the touch are usually real
        (thousands of shares), but the gate should still measure rather than
        assume.
        """
        spend = shares = 0.0
        for px, sz in self.levels():
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
class PmBook:
    """Live top-of-book per Polymarket token, one WS socket for both tokens.

    Deliberately mirrors `feeds.prediction_book.PredictionBook`'s public shape
    (`best`, `follow`, `run`, `messages`, `connected`) so the recorder and the
    arms can be pointed at either venue without a second code path.
    """

    tops: dict[str, Touch] = field(default_factory=dict)
    messages: int = 0
    #: Frames dropped by the backlog guard. Counted so "is the venue outrunning
    #: us?" is a number in the log rather than an assumption.
    skipped: int = 0
    #: The venue's OWN timestamp on the newest frame applied. This is the only
    #: cheap answer to "is the socket behind?": `Touch.recv_ms` is stamped when
    #: WE processed a frame, so it stays tiny even while the frames themselves
    #: are stale -- which is exactly how a lagging book passed a freshness gate
    #: on 2026-09-17 (`venue 0.14/0.20 | ours touch 0.1100`).
    last_ts_ms: int = 0
    connected: bool = False
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _assets: tuple[str, ...] = ()
    _resub: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task | None = None
    #: Throttle for the drain log; see `_log_drain`.
    _last_drain_log: float = 0.0
    #: Whether the socket has ever connected in this process; gates one INFO.
    _connected_once: bool = False
    #: Fallback poll: None means "no REST polling, WS only".
    _poll_url: str | None = None

    # -- read path ---------------------------------------------------------
    def best(self, asset_id: str | None, max_age_s: float) -> Touch | None:
        """The token's touch, or None if missing or older than `max_age_s`.

        Staleness fails CLOSED, exactly as on the wrapper: a frozen feed that
        prices every order off a dead book is worse than no price at all.
        """
        if not asset_id:
            return None
        t = self.tops.get(str(asset_id))
        if t is None or t.age_s() > max_age_s:
            return None
        return t

    def ask(self, asset_id: str | None, max_age_s: float) -> float | None:
        t = self.best(asset_id, max_age_s)
        return None if t is None else t.ask

    def mid(self, asset_id: str | None, max_age_s: float) -> float | None:
        t = self.best(asset_id, max_age_s)
        return None if t is None else (t.bid + t.ask) / 2.0

    # -- ingest ------------------------------------------------------------
    def lag_ms(self, now_ms: int | None = None,
               asset_id: str | None = None) -> int:
        """How far the newest VENUE timestamp we hold sits behind our clock.

        Distinct from `Touch.age_s()`, and the distinction is the whole point:
        `recv_ms` measures when the frame reached US, so a consumer that is
        0.9 s behind still reports an age of a few milliseconds. This measures
        the frames instead. Both are needed; only this one can fail.

        ⛔ PASS `asset_id` TO ASK ABOUT ONE TOKEN, and do not trust the global
        answer for a single market. Measured 2026-09-17: the GLOBAL figure read
        121 ms while the round being priced was FOUR MINUTES old -- some other
        asset was current and kept the maximum high, which is exactly the
        question a per-order check has to answer and a global one cannot.
        Returns -1 when that asset has no frame at all.
        """
        now = now_ms or int(time.time() * 1000)
        if asset_id is not None:
            t = self.tops.get(str(asset_id))
            if t is None or t.ts_ms <= 0:
                return -1
            return max(0, now - t.ts_ms)
        if self.last_ts_ms <= 0:
            return 0
        return max(0, now - self.last_ts_ms)
    @staticmethod
    def _bump(levels: tuple, px: float, sz: float, *, reverse: bool) -> tuple:
        """The ladder with the level at `px` set to an ABSOLUTE `sz` shares.

        `price_change.size` is the resting size at that price, not an increment
        -- observed on this venue: a snapshot's 11360.15 at 0.01 came back as
        11355.15 in the next change, i.e. -5 shares reported as 11355.15.
        `sz == 0` means the level is gone and is dropped.
        """
        out = [(p, s) for (p, s) in levels if s > 0 and abs(p - px) > 1e-9]
        if sz > 0:
            out.append((px, sz))
        out.sort(key=lambda lv: lv[0], reverse=reverse)
        return tuple(out[:KEEP_LEVELS])

    def _apply_delta(self, ch: dict, now_ms: int, msg_ts: int = 0) -> None:
        """Maintain the touch AND THE LADDER from one `price_change` entry.

        ⛔ THE LADDER IS THE BUG THIS FUNCTION USED TO HAVE (measured live
        2026-09-17). It updated `bid`/`ask`/`*_size` and then carried
        `prev.bids`/`prev.asks` forward VERBATIM, so the ladder was rebuilt
        only by a `book` snapshot -- once per socket, not once per message
        (~1,250 messages/s across the 18 assets).

        `Touch.sweep()` walks the ladder, and the cross arm prices its entry,
        sizes it, gates it, computes its LIMIT CEILING (`buf` is a function of
        the sweep price) and records its "decided" price all from that sweep.
        So once the market had moved away from the price at subscription time,
        the arm decided on a price that no longer existed AND sent a limit
        derived from it:
            decided 0.4000 limit 0.42 -> NO FILL in 0.93s | book now 0.6000
        with the venue answering `no orders found to match`. That is the whole
        gate->fill gap. It was never latency and never adverse selection.

        The entry is a change to ONE level and carries an absolute size, so
        both numbers are kept exactly with no replay.
        """
        aid = str(ch.get("asset_id") or "")
        if not aid:
            return
        prev = self.tops.get(aid)
        bb, ba = _f(ch.get("best_bid")), _f(ch.get("best_ask"))
        if not bb and not ba and "best_bid" not in ch and "best_ask" not in ch:
            return
        px, sz, side = _f(ch.get("price")), _f(ch.get("size")), ch.get("side")
        bid_sz = prev.bid_size if prev else 0.0
        ask_sz = prev.ask_size if prev else 0.0
        bids = prev.bids if prev else ()
        asks = prev.asks if prev else ()
        if 0.0 < px < 1.0:
            if side == "BUY":
                bids = self._bump(bids, px, sz, reverse=True)
            elif side == "SELL":
                asks = self._bump(asks, px, sz, reverse=False)
        # ⛔ A SIDE IS ONLY TOUCHED WHEN THE ENTRY ACTUALLY SPEAKS ABOUT IT.
        # `best_bid`/`best_ask` of 0 means **no bids / no asks**, which this
        # venue really does near a round's end -- `book.py`'s own note records
        # "Up 0 bids / 99 asks while Down had 99 bids / 0 asks". The old guard
        # returned early for EITHER side being 0, so every frame for a
        # one-sided book was dropped and the touch froze at its last two-sided
        # value: the live log's `cross btc-updown-15m Down not taken: no ask on
        # the book` with an all-zero `cross fine` is that freeze.
        # An ABSENT key is different from a zero one: absent means "unchanged",
        # zero means "empty", and conflating them would wipe a live ladder.
        if "best_bid" in ch:
            if bb > 0:
                bids = tuple(lv for lv in bids
                             if lv[1] > 0 and lv[0] <= bb + 1e-9)
                hit = next((s for p, s in bids if abs(p - bb) < 1e-9), None)
                if hit is not None:
                    bid_sz = hit
                elif abs(bb - (prev.bid if prev else 0.0)) > 1e-9:
                    bid_sz = 0.0
            else:
                bids, bid_sz = (), 0.0
        else:
            bb = prev.bid if prev else 0.0
            if not prev or prev.bid <= 0:
                bids, bid_sz = (), 0.0
        if "best_ask" in ch:
            if ba > 0:
                asks = tuple(lv for lv in asks
                             if lv[1] > 0 and lv[0] >= ba - 1e-9)
                hit = next((s for p, s in asks if abs(p - ba) < 1e-9), None)
                if hit is not None:
                    ask_sz = hit
                elif abs(ba - (prev.ask if prev else 0.0)) > 1e-9:
                    ask_sz = 0.0
            else:
                asks, ask_sz = (), 0.0
        else:
            # Not restated in this entry: carry the side forward verbatim.
            ba = prev.ask if prev else 0.0
        self.tops[aid] = Touch(
            asset_id=aid, bid=bb, bid_size=bid_sz, ask=ba, ask_size=ask_sz,
            # ⛔ THE MESSAGE'S TIMESTAMP, NOT THE ENTRY'S. `price_changes`
            # entries carry asset_id/price/size/side/best_bid/best_ask and NO
            # timestamp -- it sits on the enclosing message -- so this read
            # `None` on every delta and `Touch.ts_ms` was silently 0 for any
            # touch built from one. Latent until `lag_ms(asset_id=...)` needed
            # it; the global `lag_ms()` uses the message and was unaffected,
            # which is exactly how a per-token lag could read "no data" while
            # the aggregate read a healthy 121 ms.
            ts_ms=msg_ts or int(_f(ch.get("timestamp"))), recv_ms=now_ms,
            bids=bids, asks=asks,
        )

    def _apply_snapshot(self, d: dict, now_ms: int) -> None:
        aid = str(d.get("asset_id") or "")
        if not aid:
            return
        def side_levels(key: str, reverse: bool) -> tuple:
            out = []
            for lv in (d.get(key) or []):
                px, sz = _f(lv.get("price")), _f(lv.get("size"))
                if px > 0 and sz > 0:
                    out.append((px, sz))
            out.sort(key=lambda lv: lv[0], reverse=reverse)
            return tuple(out[:KEEP_LEVELS])

        bids = side_levels("bids", True)     # descending, best first
        asks = side_levels("asks", False)    # ascending, best first
        # ONE SIDED IS A REAL STATE HERE, not a parse failure: this venue's
        # books empty one side near a round's end (measured: "Up 0 bids / 99
        # asks while Down had 99 bids / 0 asks"). The old guard returned early,
        # so a one-sided snapshot taught us NOTHING and the previous two-sided
        # touch stayed, looking live. `Touch.levels()` already returns [] when
        # there is no ask, which is what makes the arm refuse honestly.
        if not bids and not asks:
            return
        self.tops[aid] = Touch(
            asset_id=aid,
            bid=bids[0][0] if bids else 0.0,
            bid_size=bids[0][1] if bids else 0.0,
            ask=asks[0][0] if asks else 0.0,
            ask_size=asks[0][1] if asks else 0.0,
            ts_ms=int(_f(d.get("timestamp"))), recv_ms=now_ms,
            bids=bids, asks=asks,
        )

    @staticmethod
    def _split(raw: str) -> tuple[tuple[str, ...], list[dict]]:
        """(assets this frame names, its parsed messages).

        A frame is NOT an asset. One `price_change` carries a list of entries,
        each with its own `asset_id`, so a single frame can move several books
        at once -- which is why a drain cannot keep "the newest frame" and call
        the state current for anything but the one asset it happened to name.
        """
        try:
            d = json.loads(raw)
        except (ValueError, TypeError):
            return (), []
        msgs = [m for m in (d if isinstance(d, list) else [d])
                if isinstance(m, dict)]
        aids: list[str] = []
        for m in msgs:
            if m.get("asset_id"):
                aids.append(str(m["asset_id"]))
            for ch in (m.get("price_changes") or []):
                if isinstance(ch, dict) and ch.get("asset_id"):
                    aids.append(str(ch["asset_id"]))
        return tuple(aids), msgs

    def _ingest(self, raw: str) -> None:
        self._ingest_msgs(self._split(raw)[1])

    def _ingest_msgs(self, msgs: list[dict]) -> None:
        now_ms = int(time.time() * 1000)
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            # `book` snapshots are identified by their levels, not by
            # `event_type`: the first snapshot on a fresh subscription arrives
            # without one (observed 2026-09-16).
            if "bids" in msg or "asks" in msg:
                self._apply_snapshot(msg, now_ms)
            elif msg.get("event_type") == "price_change":
                mts = int(_f(msg.get("timestamp")))
                for ch in (msg.get("price_changes") or []):
                    if isinstance(ch, dict):
                        self._apply_delta(ch, now_ms, mts)
            elif msg.get("event_type") == "last_trade_price":
                # Deliberately ignored: a print is not a quote, and this
                # venue's displayed price misled the wrapper's arms already.
                pass
            else:
                continue
            # The VENUE's clock, not ours. Advances only when a frame is APPLIED,
            # so it stops advancing exactly when this loop is starved -- which
            # is what `lag_ms()` reports and `Touch.age_s()` cannot.
            ts = int(_f(msg.get("timestamp")))
            if ts > self.last_ts_ms:
                self.last_ts_ms = ts
            self.messages += 1

    def _ingest_ok(self, raw: str) -> bool:
        """True when the message actually moved a book (drives recv_ms)."""
        before = self.messages
        self._ingest(raw)
        return self.messages > before

    # -- lifecycle ---------------------------------------------------------
    def follow(self, asset_ids) -> None:
        want = tuple(str(a) for a in asset_ids if a)
        if want != self._assets:
            self._assets = want
            self._resub.set()

    def stop(self) -> None:
        self._stop.set()
        self._resub.set()

    async def run(self) -> None:
        """Supervise the socket; reconnect with backoff until stopped."""
        backoff = 1.0
        while not self._stop.is_set():
            if not self._assets:
                try:
                    await asyncio.wait_for(self._resub.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                self._resub.clear()
                continue
            try:
                await self._run_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:                       # noqa: BLE001
                log.warning("pm book dropped (%s: %s); retry in %.0fs",
                            type(e).__name__, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self.connected = False

    async def _run_once(self) -> None:
        import aiohttp

        # `total=None`: the per-message timeout used to live in this loop (see
        # the bare `receive()` below) and it is what killed the process.
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None)) as s:
            async with s.ws_connect(WS_URL, heartbeat=20.0) as ws:
                await ws.send_json({"assets_ids": list(self._assets),
                                    "type": "market"})
                self.connected = True
                # INFO ONCE, then debug. The asset set changes at every round
                # roll (and once per family as it comes up), so at boot this
                # fires three times in five seconds -- `connected (14 assets)`
                # / `(16)` / `(18)`. The FIRST one is worth keeping at INFO: it
                # is the only line that proves the feed ever came up, and a
                # silent feed is otherwise indistinguishable from a quiet one.
                if not self._connected_once:
                    self._connected_once = True
                    log.info("pm book ws connected (%d assets)",
                             len(self._assets))
                else:
                    log.debug("pm book ws connected (%d assets)",
                              len(self._assets))
                while not self._stop.is_set():
                    if self._resub.is_set():
                        # The asset set changed: this socket's subscription is
                        # fixed at connect time, so rebuild it.
                        self._resub.clear()
                        return
                    # ⛔ A BARE `receive()`. THIS IS THE OOM FIX (2026-09-17).
                    #
                    # This venue pushes ~1,250 messages/s across the 18 assets of
                    # the 9 live families (measured: 24,988 msgs / 20.0 s, 0.79
                    # MB/s, p50 0.6 kB, max 51 kB -- one message per
                    # `price_change`, not one per asset). The loop used to wrap
                    # every single message in `asyncio.wait_for(..., timeout=5.0)`,
                    # and a py-spy profile of the ballooning process reads **115
                    # samples inside `asyncio/tasks.py` (wait_for) against 9
                    # inside `_ingest`**: the consumer spent its time building a
                    # Task and a timer per message instead of applying the book.
                    #
                    # When the consumer falls behind, aiohttp's reader queue
                    # absorbs the difference as PYTHON OBJECTS, and RSS grew at a
                    # measured **~67 MB/s** (1.38 -> 1.70 -> 2.05 GB in ten
                    # seconds) until the cgroup OOM killer took the process at
                    # exactly its 2 GB cap: `anon-rss:2088220kB,
                    # constraint=CONSTRAINT_MEMCG, oom_memcg=...btcpredict.service`,
                    # every ~4 minutes, which is the restart loop the operator saw.
                    #
                    # Liveness does NOT need a per-message timer: `heartbeat=20.0`
                    # closes the socket when the venue stops answering pings, and
                    # that surfaces right here as an exception out of `receive()`.
                    msg = await ws.receive()
                    if msg.type.name != "TEXT":
                        if msg.type.name in ("CLOSE", "CLOSING", "CLOSED",
                                             "ERROR"):
                            return
                        continue
                    self._ingest(msg.data)
                    # Belt and braces: if the venue ever outruns any single
                    # consumer, skip the MIDDLE of the backlog instead of
                    # retaining it. Every asset's state after a drain is its
                    # own NEWEST frame, never an older one -- see `_drain`.
                    if self._pending(ws) > self.max_backlog:
                        drained = await self._drain(ws)
                        if drained:
                            self.skipped += drained
                            self._log_drain(drained)

    #: Frames aiohttp may hold before this loop drains them.
    #:
    #: ⚠️ THIS IS NOT THE LAG, AND LOWERING IT DOES NOT REDUCE STALENESS.
    #: Measured 2026-09-17, same minute, same socket, only this number changed:
    #:     cap 400 (~0.32 s of this feed) -> venue lag  275-580 ms
    #:     cap  32 (~0.026 s)             -> venue lag  665-871 ms
    #: Six drains in 240 ms with the queue EMPTY after every one, and the newest
    #: frame in it still stamped 700 ms ago. The frames themselves arrive that
    #: late; no consumer design removes it. The box's clock is NTP-synchronised
    #: (`timedatectl`: "System clock synchronized: yes"), so this is delivery
    #: latency, not skew.
    #:
    #: What the cap DOES buy is memory, so it stays low. Correctness no longer
    #: depends on it -- `_drain` keeps every asset's own newest frame.
    max_backlog: int = 128

    #: A drain is ROUTINE: with this cap the loop drains several times a second
    #: and an INFO line per drain is its own outage (the operator's words, on
    #: 2026-09-17: "too much spam"). What is news is a drain that did NOT catch
    #: up, so the line is gated on the lag -- which is exactly the quantity the
    #: drain exists to protect -- and throttled on top. `self.skipped` keeps
    #: counting every drained frame for whoever wants the raw number.
    #:
    #: 300ms is ~4 signa of the ~120ms floor measured on this feed, so it fires
    #: on a real stall and not on the ordinary jitter around it.
    lag_alarm_ms: int = 300
    lag_alarm_every_s: float = 60.0

    def _log_drain(self, drained: int) -> None:
        """Say something ONLY when the drain failed to catch the socket up.

        Silent by default. The routine case is a drain that works, and logging
        it buries the lines that matter -- which on this feed is the `shadow
        book` delta and the FAK outcome.

        `venue lag` is the number to watch when the alarm does fire: it is the
        venue's own clock against ours on the newest frame APPLIED, so it rises
        exactly when this loop is starved and falls the instant a drain catches
        up. `Touch.age_s()` cannot show that, and it is why a quarter of a
        dollar of staleness went unnoticed for a whole session.
        """
        lag = self.lag_ms()
        if lag < self.lag_alarm_ms:
            return
        now = time.monotonic()
        if now - self._last_drain_log < self.lag_alarm_every_s:
            return
        self._last_drain_log = now
        log.warning("pm book ws lagging: venue lag %dms after draining %d "
                    "frames (total %d)", lag, drained, self.skipped)

    @staticmethod
    def _pending(ws) -> int:
        try:
            return len(ws._reader._buffer)
        except Exception:                                # noqa: BLE001
            return -1

    async def _drain(self, ws) -> int:
        """Drop the middle of a backlog, keeping the NEWEST FRAME PER ASSET.

        ⛔ THE OLD VERSION KEPT THE NEWEST FRAME, PERIOD, and that is the bug
        this replaces. Its comment claimed the skipped frames "are superseded by
        construction" -- true only for the ONE asset the newest frame happens to
        name, and this feed carries 18. Measured live 2026-09-17:

            08:49:31,453 POST /order                       (the 1.01s round trip)
            08:49:31,457 pm book ws behind by 400 frames; skipped 420
            08:49:31,509 pm book ws behind by 400 frames; skipped 412
            08:49:31,794 shadow book: venue 0.14/0.20 | ours touch 0.1100

        The consumer stalled for the order, ~830 frames queued, and the drain
        left 17 of the 18 books at whatever state their last-processed frame
        gave them -- 9 cents behind, which is why `limit 0.13` could not cross
        a real ask of 0.20.

        A frame is NOT an asset (see `_split`), so the correct rule is: apply a
        frame only if it is the LAST frame naming at least one of its assets.
        Every such frame is applied, in arrival order, so the final state for
        every asset is its newest
        -- and the intermediate frames, which is what the backlog is made of,
        are dropped without ever being applied.
        """
        raws: list[str] = []
        # Bounded: a producer that outruns this loop must not turn a drain into
        # an unbounded read. 4x the cap is ~1.3 s of this feed.
        limit = 4 * self.max_backlog
        while self._pending(ws) > 0 and len(raws) < limit:
            m = await ws.receive()
            if m.type.name == "TEXT":
                raws.append(m.data)
            elif m.type.name in ("CLOSE", "CLOSING", "CLOSED", "ERROR"):
                break
        if not raws:
            return 0
        # Parse ONCE. `_ingest(raw)` re-parses, so a drain that parsed for the
        # asset key and then again to apply was paying twice for every frame --
        # and a drain is the hot path whenever the socket is busy.
        split = [self._split(r) for r in raws]
        assets = [a for a, _ in split]
        last: dict[str, int] = {}
        for i, aids in enumerate(assets):
            for a in aids:
                last[a] = i
        for i in range(len(raws)):
            # A frame naming no asset (or nothing we track) cannot be keyed, so
            # it is applied rather than silently dropped -- it is free, and
            # guessing would be worse than the cost.
            if not assets[i] or any(last.get(a) == i for a in assets[i]):
                self._ingest_msgs(split[i][1])
        return len(raws)

    # -- REST fallback -----------------------------------------------------
    @classmethod
    async def fetch_top(cls, session, asset_id: str,
                        timeout: float = 5.0) -> "Touch | None":
        """The VENUE's own book for one token, read fresh. Changes nothing.

        ⛔ THIS IS THE ONLY THING THAT CAN TELL US WHICH OF OUR TWO NUMBERS IS
        WRONG, and on 2026-09-17 they disagreed by 45 cents. The arm logged it
        itself:

            cross bnb-updown-15m-... Down: $1.00 sweeps to 0.610
                (touch 0.160, +0.450)

        `touch` is `Touch.ask`, maintained by every `price_change`; the 0.610
        comes from the ladder, maintained by `book` snapshots. Both cannot be
        right, and our own two fields agreeing with each other would prove
        nothing -- which is exactly why the old "shadow quote" was worthless
        (it called `broker.get_quote`, documented "no venue call", and so
        echoed the decision price back: `fresh px 0.4000 after 0ms`).

        Deliberately does NOT write to `self.tops`: a probe must never be able
        to move the book the arms trade on. The parse is shared with
        `_apply_snapshot` via a throwaway instance so the top-N cap and the
        one-sided-book rule are the same as the live path's.
        """
        aid = str(asset_id or "")
        if not aid:
            return None
        try:
            async with session.get(BOOK_URL % aid, headers=UA,
                                   timeout=timeout) as r:
                if r.status != 200:
                    log.warning("pm probe %s -> HTTP %s", aid[:12], r.status)
                    return None
                d = await r.json()
        except Exception as exc:                          # noqa: BLE001
            log.info("pm probe %s failed: %s: %s", aid[:12],
                     type(exc).__name__, exc)
            return None
        probe = cls()
        probe._apply_snapshot({"asset_id": aid,
                               "bids": d.get("bids"), "asks": d.get("asks"),
                               "timestamp": d.get("timestamp")},
                              int(time.time() * 1000))
        return probe.tops.get(aid)

    async def poll_once(self, session, asset_ids, timeout: float = 10.0) -> int:
        """One REST sweep of the listed books. Returns how many were updated.

        Used by the recorder, which does not need a socket per market and is
        rate-limit sensitive; the trading path uses `run()`.

        `UA` IS REQUIRED, and its absence used to be invisible: this venue
        answers the default client agent with 403 on several endpoints, and
        the handler below swallowed it into "nothing updated". A silent
        one-sided or empty book is indistinguishable from a market that has
        genuinely gone one-sided -- which this venue's books DO, near the end
        of a round (measured: Up 0 bids / 99 asks while Down had 99 bids / 0
        asks). So failures are logged now, and the count is returned.
        """
        n = 0
        now_ms = int(time.time() * 1000)
        for aid in asset_ids:
            try:
                async with session.get(BOOK_URL % aid, headers=UA,
                                       timeout=timeout) as r:
                    if r.status != 200:
                        log.warning("pm book %s -> HTTP %s", str(aid)[:12],
                                    r.status)
                        continue
                    d = await r.json()
            except Exception as exc:                     # noqa: BLE001
                log.warning("pm book %s failed: %s: %s", str(aid)[:12],
                            type(exc).__name__, exc)
                continue
            # The snapshot can legitimately carry ONE empty side. That is a
            # real market state, not a parse failure, and `Touch.levels()`
            # already filters non-positive levels.
            self._apply_snapshot({"asset_id": aid,
                                  "bids": d.get("bids"),
                                  "asks": d.get("asks"),
                                  "timestamp": d.get("timestamp")}, now_ms)
            n += 1
        return n


def verify_mirror(up: Touch | None, down: Touch | None) -> dict:
    """How far are the two tokens' own books from complementary?

    The wrapper exposed an exact mirror (`down_ask = 1 - up_bid`, verified
    there). On Polymarket the Down token is its own instrument, so the mirror
    is an EMPIRICAL question and this is the function that answers it: the
    caller must not price one side off the other's complement without it.
    """
    if up is None or down is None:
        return {}
    return {
        "down_ask_minus_1m_upbid": down.ask - (1.0 - up.bid),
        "down_bid_minus_1m_upask": down.bid - (1.0 - up.ask),
        "up_ask_plus_down_bid": up.ask + down.bid,
        "up_bid_plus_down_ask": up.bid + down.ask,
    }


@dataclass(frozen=True)
class RoundTop:
    """One ROUND's top of book in the wrapper's `BookTop` shape.

    Each side reports **its own token's** book. The wrapper derived Down as
    `1 - up_bid`; that identity is NOT valid here (see verify_mirror), so it is
    never used -- `ask("Down")` is the Down token's own ask.
    """

    up: Touch | None
    down: Touch | None

    def _t(self, side: str) -> Touch | None:
        return self.down if str(side).strip().lower() == "down" else self.up

    def age_s(self, now_ms: int | None = None) -> float:
        """The OLDER of the two sides.

        A round with one fresh side is not a fresh round: the arms price a
        side AND gate its depth, so reporting the younger age would let a
        half-dead book pass a staleness check it should fail.
        """
        ages = [t.age_s(now_ms) for t in (self.up, self.down) if t is not None]
        return max(ages) if ages else float("inf")

    def ask(self, side: str) -> float:
        t = self._t(side)
        return t.ask if t is not None else 0.0

    def bid(self, side: str) -> float:
        t = self._t(side)
        return t.bid if t is not None else 0.0

    def ask_dollars(self, side: str) -> float:
        t = self._t(side)
        return t.ask_dollars() if t is not None else 0.0

    def levels(self, side: str) -> list[tuple[float, float]]:
        t = self._t(side)
        return t.levels() if t is not None else []

    def sweep(self, side: str, stake: float) -> tuple[float, float]:
        t = self._t(side)
        return t.sweep(stake) if t is not None else (0.0, 0.0)

    # The wrapper's `BookTop` exposes these four as attributes, and the
    # recorder reads them directly (`up_ask = stale.up_ask`). Derived from the
    # tokens' OWN books here, never from each other's complement.
    @property
    def up_bid(self) -> float:
        return self.up.bid if self.up is not None else 0.0

    @property
    def up_ask(self) -> float:
        return self.up.ask if self.up is not None else 0.0

    @property
    def down_bid(self) -> float:
        return self.down.bid if self.down is not None else 0.0

    @property
    def down_ask(self) -> float:
        return self.down.ask if self.down is not None else 0.0


class PmBookView:
    """The `PredictionBook` surface over `PmBook`, keyed by the ROUND.

    WHY THIS EXISTS. Every arm reads the book as `wsb.best(<round>, age)` and
    then `top.ask(side)` / `top.ask_dollars(side)` / `top.sweep(side, stake)`
    -- the wrapper's shape, where ONE book covers both outcomes because Down is
    `1 - up_bid`. On Polymarket each outcome is its OWN token with its own book
    and the venue's key is a token id.

    Without this adapter that mismatch was SILENT, which is the whole reason it
    is documented this loudly. Measured 2026-09-16 on the live VPS: `to_round`
    left `market_id` None, so the poll's `wsb.follow(...)` built an empty id
    list, `self.ws_book` stayed None, and `self._pm_book` -- a socket that
    connected fine and ingested ~680 messages/s -- had NO READER anywhere in
    the tree. Every arm fell back to displayed prices, and every family except
    the primary was refused by `_cross_tick` ("no fresh websocket book
    off-primary"): 34 such refusals in one evening.
    """

    def __init__(self, book: PmBook) -> None:
        self.book = book
        self._assets: tuple[str, ...] = ()

    @property
    def messages(self) -> int:
        return self.book.messages

    @property
    def connected(self) -> bool:
        return self.book.connected

    @staticmethod
    def _tokens(r) -> list[str]:
        """The round's two token ids, UP FIRST. The order is part of the
        subscription key, so it must not depend on dict iteration."""
        out: list[str] = []
        for side in ("up", "down"):
            tok = getattr(getattr(r, side, None), "token_id", None)
            if tok:
                out.append(str(tok))
        return out

    def follow(self, rounds) -> None:
        """Subscribe to the live round of every family handed in.

        The asset list is a CONNECT-TIME parameter of one socket, so changing
        the set forces a handshake -- hence the comparison rather than an
        unconditional call, and hence a deterministic order so an unchanged set
        never re-handshakes. The set changes at a roll, which for the three 5m
        families is the same boundary (one handshake), plus one for the 15m
        family.
        """
        want: list[str] = []
        for r in rounds or ():
            want += self._tokens(r)
        key = tuple(want)
        if key and key != self._assets:
            self._assets = key
            self.book.follow(key)

    def best(self, r, max_age_s: float) -> RoundTop | None:
        toks = self._tokens(r)
        if len(toks) != 2:
            return None
        up = self.book.best(toks[0], max_age_s)
        down = self.book.best(toks[1], max_age_s)
        if up is None and down is None:
            return None
        return RoundTop(up=up, down=down)

    def stop(self) -> None:
        self.book.stop()


def _book_top_side(top, side: str) -> float:
    """`ask`/`bid` on either book shape, or 0.0 when unknown."""
    try:
        v = top.ask(side) if side else 0.0
    except TypeError:
        v = 0.0
    return float(v or 0.0)


if __name__ == "__main__":  # pragma: no cover - live smoke check
    import urllib.request
    from btcpredict.venues.polymarket import markets as mk

    cur, nxt = mk.current_and_next()
    if cur is None:
        raise SystemExit("no live round")
    book = PmBook()

    async def main():
        import aiohttp
        async with aiohttp.ClientSession() as s:
            await book.poll_once(s, [cur.up_token, cur.down_token])
        book.follow([cur.up_token, cur.down_token])
        task = asyncio.create_task(book.run())
        await asyncio.sleep(6)
        task.cancel()
        up = book.best(cur.up_token, 5)
        down = book.best(cur.down_token, 5)
        for lab, t in (("Up", up), ("Down", down)):
            if t is None:
                print("%-5s no book" % lab); continue
            print("%-5s bid %.3f x%.1f   ask %.3f x%.1f   (age %.2fs, %d bids/%d asks)"
                  % (lab, t.bid, t.bid_size, t.ask, t.ask_size, t.age_s(),
                     len(t.bids), len(t.asks)))
            print("      sweep $5 -> avg %.4f, $%.2f fillable; $5 at touch = %.2f shares"
                  % (*t.sweep(5.0), 5.0 / t.ask))
        print("mirror:", verify_mirror(up, down))
        print("messages:", book.messages, "| venue floor: %.0f SHARES = $%.2f at this ask"
              % (cur.min_order, cur.min_order * (up.ask if up else 0)))

    asyncio.run(main())
