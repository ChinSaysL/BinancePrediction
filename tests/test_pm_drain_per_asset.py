"""A backlog drain must leave EVERY asset at its OWN newest frame.

THE BUG THIS PINS (measured live 2026-09-17). `_drain()` read the backlog and
kept the single NEWEST frame -- and applied it. Its comment justified that with
"the frames in between are superseded by construction", which is true only for
the ONE asset the newest frame happens to name. This feed carries **18 assets**,
so a drain left 17 of them at whatever state their last-processed frame gave.

The order path is what triggers it, because it stalls the consumer:

    08:49:31,453 POST /order                       (the 1.01s round trip)
    08:49:31,457 pm book ws behind by 400 frames; skipped 420
    08:49:31,509 pm book ws behind by 400 frames; skipped 412
    08:49:31,794 shadow book: venue 0.14/0.20 | ours touch 0.1100 (-0.0900)
    08:49:31     cross exec: decided 0.1143 limit 0.13 -> NO FILL
                 pm FAK: no orders found to match

Our book was 9 cents behind the venue's the moment we priced the order, so the
limit derived from it could not cross a real ask of 0.20. Every NO FILL in the
day's log sits at 0.13-0.61 (near the strike, where the price moves) and the
only 100% fills are 0.80 and 0.77 (deep ITM, where it does not).

THE RULE, which the tests below check from both sides: apply a frame only if it
is the LAST frame naming at least ONE of its assets; apply those in arrival
order. Every asset therefore ends on its own newest frame, and the intermediate
frames -- which is what a backlog is made of -- are never applied.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from btcpredict.venues.polymarket.book import PmBook


class FakeMessage:
    def __init__(self, data: str, kind: str = "TEXT") -> None:
        self.data = data
        self.type = type("T", (), {"name": kind})()


class FakeWs:
    def __init__(self, queued: list[str]) -> None:
        self._reader = type("R", (), {"_buffer": list(queued)})()

    async def receive(self):
        if not self._reader._buffer:
            raise AssertionError("receive() past the end of the fake queue")
        return FakeMessage(self._reader._buffer.pop(0))


def _snapshot(asset: str, ask: float, ts: int = 1_789_000_000_000) -> str:
    return json.dumps({
        "event_type": "book", "asset_id": asset, "timestamp": str(ts),
        "bids": [{"price": str(round(ask - 0.01, 4)), "size": "100"}],
        "asks": [{"price": str(ask), "size": "100"}],
    })


def _change(*entries, ts: int = 1_789_000_000_000) -> str:
    """One frame, one or more `price_changes` entries -- as the venue sends."""
    return json.dumps({
        "event_type": "price_change", "timestamp": str(ts),
        "price_changes": [
            {"asset_id": a, "price": str(px), "size": str(sz), "side": side,
             "best_bid": str(bb), "best_ask": str(ba)}
            for a, px, sz, side, bb, ba in entries]})


def _book_with(*assets: str) -> PmBook:
    b = PmBook()
    for i, a in enumerate(assets):
        b._ingest(_snapshot(a, 0.10 + 0.10 * i))
    return b


class TestTheLiveIncident:
    def test_a_drain_leaves_every_asset_at_its_own_newest_frame(self) -> None:
        """The 08:49 case, on three assets instead of eighteen.

        Pre-fix only C moved: A and B stayed at their snapshot prices, and A was
        the one being priced.
        """
        book = _book_with("A", "B", "C")
        ws = FakeWs([_change(("A", 0.50, 10, "SELL", 0.49, 0.50)),
                     _change(("B", 0.60, 10, "SELL", 0.59, 0.60)),
                     _change(("C", 0.70, 10, "SELL", 0.69, 0.70))])
        assert asyncio.run(book._drain(ws)) == 3
        assert book.best("A", 60).ask == pytest.approx(0.50)
        assert book.best("B", 60).ask == pytest.approx(0.60)
        assert book.best("C", 60).ask == pytest.approx(0.70)

    def test_the_sweep_the_order_is_priced_from_follows(self) -> None:
        """The point is the price the CEILING is derived from, not the field."""
        book = _book_with("A")
        ws = FakeWs([_change(("A", 0.11, 10, "SELL", 0.10, 0.11)),
                     _change(("A", 0.15, 10, "SELL", 0.14, 0.15)),
                     _change(("A", 0.20, 500, "SELL", 0.14, 0.20))])
        asyncio.run(book._drain(ws))
        px, _ = book.best("A", 60).sweep(1.0)
        assert px == pytest.approx(0.20), (
            "a stale sweep is what produced `limit 0.13` against a real 0.20 ask")


class TestTheRule:
    def test_only_the_last_frame_naming_an_asset_is_applied(self) -> None:
        book = _book_with("A")
        before = book.messages
        ws = FakeWs([_change(("A", 0.50, 10, "SELL", 0.49, 0.50)),
                     _change(("A", 0.55, 10, "SELL", 0.54, 0.55)),
                     _change(("A", 0.60, 10, "SELL", 0.59, 0.60))])
        asyncio.run(book._drain(ws))
        assert book.messages - before == 1, "the middle frames must not be applied"
        assert book.best("A", 60).ask == pytest.approx(0.60)

    def test_a_frame_newest_for_one_asset_is_applied_for_the_other(self) -> None:
        """A frame is NOT an asset: it may be dead for A and alive for B.

        Requiring EVERY asset in a frame to be un-superseded would drop this one
        and leave B stale -- the bug, one level down.
        """
        book = _book_with("A", "B")
        ws = FakeWs([_change(("A", 0.50, 10, "SELL", 0.49, 0.50),
                            ("B", 0.60, 10, "SELL", 0.59, 0.60)),
                     _change(("A", 0.55, 10, "SELL", 0.54, 0.55))])
        asyncio.run(book._drain(ws))
        assert book.best("A", 60).ask == pytest.approx(0.55), "the later frame wins"
        assert book.best("B", 60).ask == pytest.approx(0.60), "F was B's newest"

    def test_a_frame_naming_nothing_is_applied_rather_than_dropped(self) -> None:
        book = _book_with("A")
        before = book.messages
        junk = json.dumps({"event_type": "price_change", "timestamp": "1",
                           "price_changes": []})
        asyncio.run(book._drain(FakeWs([junk])))
        assert book.messages - before == 1, (
            "an unkeyable frame cannot be proven superseded, so it is applied")

    def test_a_control_frame_ends_the_drain_without_hanging(self) -> None:
        book = _book_with("A")
        ws = FakeWs(["a", "b", "c"])

        async def closed():
            return FakeMessage("", "CLOSED")
        ws.receive = closed
        assert asyncio.run(book._drain(ws)) == 0

    def test_the_drain_is_bounded(self) -> None:
        """A producer that outruns this loop must not turn a drain into a read."""
        book = _book_with("A")
        ws = FakeWs([_change(("A", 0.5, 1, "SELL", 0.49, 0.5))
                     for _ in range(20 * PmBook.max_backlog)])
        drained = asyncio.run(book._drain(ws))
        assert 0 < drained <= 4 * PmBook.max_backlog


class TestTheLagMeasure:
    def test_the_cap_bounds_memory_not_staleness(self) -> None:
        """⛔ LOWERING THE CAP DID NOT REDUCE THE LAG, so it must not be sold as
        if it does.

        Measured 2026-09-17, same minute and socket, only the cap changed:
            cap 400 (~0.32 s of this feed) -> venue lag 275-580 ms
            cap  32 (~0.026 s)             -> venue lag 665-871 ms
        Six drains in 240 ms with the queue EMPTY after each, and the newest
        frame in it still stamped 700 ms old. The frames arrive that late -- the
        box's clock is NTP-synchronised, so it is delivery latency, not skew --
        and no consumer design removes it. What the cap buys is memory; what
        keeps the book CORRECT is the per-asset rule, which holds at any cap.
        """
        # The invariant, at whatever cap is chosen: every asset ends on its own
        # newest frame. Exercised here against the live cap rather than a
        # fixture's, so a future "just lower the cap" edit is not mistaken for
        # a fix and a future raise does not silently reintroduce the bug.
        book = _book_with("A", "B")
        ws = FakeWs([_change(("A", 0.50, 10, "SELL", 0.49, 0.50)),
                     _change(("B", 0.60, 10, "SELL", 0.59, 0.60))])
        asyncio.run(book._drain(ws))
        assert book.best("A", 60).ask == pytest.approx(0.50)
        assert book.best("B", 60).ask == pytest.approx(0.60)
        assert PmBook.max_backlog <= 1000, (
            "a huge cap is a memory hazard; see the OOM note in `book.py`")

    def test_lag_reads_the_venue_clock_not_ours(self) -> None:
        """The property that made a 9-cent lag pass every freshness gate.

        `Touch.age_s()` is stamped when WE process a frame, so a consumer that
        is 0.9 s behind still reports milliseconds. `lag_ms()` measures the
        frames themselves, so it can actually fail.
        """
        book = PmBook()
        old = int(time.time() * 1000) - 900
        book._ingest(_change(("A", 0.5, 10, "SELL", 0.49, 0.5), ts=old))
        assert book.lag_ms() >= 700, "the frames are old"
        assert book.best("A", 60).age_s() < 1.0, "but they reached us just now"

    def test_lag_is_zero_before_anything_arrives(self) -> None:
        assert PmBook().lag_ms() == 0

    def test_per_token_lag_can_differ_from_the_global_one(self) -> None:
        """The global figure read 121 ms while the priced round was 4 min old.

        A stale token cannot hide behind a fresh neighbour, and an unknown
        token must say so (-1) rather than inherit the global answer.
        """
        book = PmBook()
        now = int(time.time() * 1000)
        book._ingest(_change(("FRESH", 0.5, 10, "SELL", 0.49, 0.5), ts=now))
        book._ingest(_change(("OLD", 0.5, 10, "SELL", 0.49, 0.5),
                             ts=now - 240_000))
        assert book.lag_ms() < 100, "the global answer tracks the newest frame"
        assert book.lag_ms(asset_id="FRESH") < 100
        assert book.lag_ms(asset_id="OLD") >= 239_000, (
            "a four-minute-old token must not be able to report as fresh")
        assert book.lag_ms(asset_id="NEVER-SEEN") == -1

    def test_lag_never_goes_negative_on_clock_skew(self) -> None:
        book = PmBook()
        book._ingest(_change(("A", 0.5, 10, "SELL", 0.49, 0.5),
                             ts=int(time.time() * 1000) + 60_000))
        assert book.lag_ms() == 0
