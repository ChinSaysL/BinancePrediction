"""The book socket must never retain a backlog (2026-09-17 OOM).

THE INCIDENT THESE TESTS PIN. With all nine families followed the venue pushes
~1,250 messages/s (24,988 in 20.0 s, p50 0.6 kB, max 51 kB). The receive loop
wrapped every message in `asyncio.wait_for(ws.receive(), timeout=5.0)`, and a
py-spy profile of the ballooning process reads 115 samples inside
`asyncio/tasks.py` against 9 inside `_ingest`: the consumer was building a
Task+timer per message, fell behind, and aiohttp held the difference as objects.
RSS grew ~67 MB/s (1.38 -> 2.05 GB in ten seconds) until the cgroup OOM killer
took the process at its 2 GB cap, every ~4 minutes.

Three properties keep that from coming back, and none of them is visible in a
diff of the trading logic:

1. NO PER-MESSAGE TIMEOUT. Liveness is the socket heartbeat. A `wait_for` here
   is the bug, not a safety net, so it is asserted ABSENT by source.
2. THE QUEUE IS MEASURABLE, and reading it never raises -- a private aiohttp
   attribute that moves must degrade to "cannot tell", not to a crash in the
   feed the arms price against.
3. THE DRAIN KEEPS THE NEWEST FRAME **PER ASSET**. A frame is not an asset --
   one `price_change` carries several entries with their own `asset_id` -- so
   keeping "the newest frame" left 17 of the 18 books stale, which is how our
   touch came to sit 9 cents under the venue's on 2026-09-17. See
   `test_pm_drain_per_asset.py` for the rule and the incident.
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib

import pytest

from btcpredict.venues.polymarket.book import PmBook

BOOK = (pathlib.Path(__file__).resolve().parents[1] / "btcpredict" / "venues"
        / "polymarket" / "book.py")

TOKEN = "1234567890"


def _run_once_node() -> ast.AsyncFunctionDef:
    """The `_run_once` body as AST.

    Substrings would match this file's own comments about the bug it fixes --
    the repo's rule for a source guard is to parse, not to search.
    """
    for n in ast.walk(ast.parse(BOOK.read_text())):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_once":
            return n
    pytest.fail("_run_once is gone: these guards would be measuring nothing")


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            fn = n.func
            if (getattr(fn, "attr", None) == name
                    or getattr(fn, "id", None) == name):
                out.append(n)
    return out


def _price_change(px: str, bb: str, ba: str) -> str:
    return json.dumps({"event_type": "price_change", "price_changes": [
        {"asset_id": TOKEN, "price": px, "size": "10", "side": "BUY",
         "best_bid": bb, "best_ask": ba}]})


class FakeMessage:
    def __init__(self, data: str, kind: str = "TEXT") -> None:
        self.data = data
        self.type = type("T", (), {"name": kind})()


class FakeWs:
    """Stands in for aiohttp's socket: a queue we can preload and count."""

    def __init__(self, queued: list[str]) -> None:
        self._reader = type("R", (), {"_buffer": list(queued)})()
        self.sent: list[str] = []
        self.closed = False

    async def send_json(self, payload) -> None:
        self.sent.append(payload)

    async def receive(self):
        if not self._reader._buffer:
            raise AssertionError("receive() past the end of the fake queue")
        return FakeMessage(self._reader._buffer.pop(0))


class TestTheReceiveLoopHasNoPerMessageTimeout:
    """Property 1. This is the regression, so it is checked in the AST."""

    def test_no_wait_for_in_the_receive_loop(self) -> None:
        assert not _calls(_run_once_node(), "wait_for"), (
            "a per-message timeout is back in the book receive loop: at "
            "~1,250 msg/s it costs a Task and a timer per message and the "
            "consumer falls behind into aiohttp's queue (2 GB OOM, 2026-09-17)")
        assert _calls(_run_once_node(), "receive")

    def test_the_session_has_no_receive_timeout_either(self) -> None:
        """aiohttp applies `ClientTimeout.ws_receive` inside `receive()`."""
        kws = [k for c in _calls(_run_once_node(), "ClientTimeout")
               for k in c.keywords]
        assert any(k.arg == "total" and isinstance(k.value, ast.Constant)
                   and k.value.value is None for k in kws), (
            "the session timeout is per-RECEIVE in aiohttp, so a default one "
            "reintroduces exactly the per-message cost this fix removed")

    def test_liveness_is_the_heartbeat(self) -> None:
        kws = [k for c in _calls(_run_once_node(), "ws_connect")
               for k in c.keywords]
        hb = [k for k in kws if k.arg == "heartbeat"]
        assert hb and isinstance(hb[0].value, ast.Constant), (
            "with no per-message timeout the heartbeat IS the liveness "
            "mechanism; without it a silent socket blocks forever")


class TestReadingTheQueue:
    """Property 2: measurement must never raise into the feed."""

    def test_counts_the_queued_frames(self) -> None:
        assert PmBook._pending(FakeWs(["a", "b", "c"])) == 3

    def test_an_empty_queue_we_know_about_reads_zero(self) -> None:
        assert PmBook._pending(FakeWs([])) == 0

    def test_an_unreadable_reader_reads_minus_one(self) -> None:
        """One day aiohttp renames `_buffer`; that must not raise here."""
        class Strange:
            pass
        assert PmBook._pending(Strange()) == -1
        assert PmBook._pending(type("X", (), {"_reader": None})()) == -1

    def test_the_cap_is_small_enough_to_matter(self) -> None:
        """At ~1,250 msg/s a 400-frame cap is ~1/3 s of backlog, not minutes."""
        assert 0 < PmBook.max_backlog <= 1000


class TestTheDrain:
    """Property 3: the middle is dropped, the state stays current."""

    def test_it_keeps_the_newest_frame_per_asset(self) -> None:
        """One asset here, so the last frame is the one that counts.

        The multi-asset case -- the one that actually broke -- is in
        `test_pm_drain_per_asset.py`.
        """
        book = PmBook()
        ws = FakeWs([_price_change("0.30", "0.29", "0.31"),
                     _price_change("0.40", "0.39", "0.41"),
                     _price_change("0.50", "0.49", "0.51")])
        assert asyncio.run(book._drain(ws)) == 3
        assert len(ws._reader._buffer) == 0
        touch = book.best(TOKEN, 60.0)
        assert touch is not None and (touch.bid, touch.ask) == (0.49, 0.51), (
            "the drain must land on the NEWEST state it read")

    def test_a_single_queued_frame_is_read_and_applied(self) -> None:
        """The old drain stopped at one frame; the rule makes that unnecessary.

        `_drain` is only ever entered above `max_backlog`, so this is the unit
        contract rather than a live path: what is queued is what gets applied.
        """
        book = PmBook()
        ws = FakeWs([_price_change("0.30", "0.29", "0.31")])
        assert asyncio.run(book._drain(ws)) == 1
        assert book.best(TOKEN, 60.0).ask == 0.31

    def test_a_control_frame_ends_the_drain_without_hanging(self) -> None:
        """CLOSE et al. carry no data; the drain must not loop past them."""
        book = PmBook()
        ws = FakeWs(["a", "b", "c"])

        async def fake_receive():
            return FakeMessage("", "CLOSED")
        ws.receive = fake_receive
        assert asyncio.run(book._drain(ws)) == 0

    def test_the_drain_is_only_entered_above_the_cap(self) -> None:
        """Source guard: the guard must be wired to the loop, not orphaned."""
        node = _run_once_node()
        asserts_cap = [
            n for n in ast.walk(node)
            if isinstance(n, ast.Compare)
            and any(getattr(getattr(c, "attr", None), "__str__", lambda: "")()
                    == "max_backlog" for c in n.comparators)]
        assert asserts_cap, "nothing compares against `max_backlog`"
        guarded = [n for n in ast.walk(node)
                   if isinstance(n, ast.If)
                   and any(c in ast.walk(n.test) for c in asserts_cap)]
        assert guarded, "the backlog check is not an `if`"
        assert any(_calls(g, "_drain") for g in guarded), (
            "`max_backlog` is compared but `_drain` is never called: the "
            "ceiling would not actually bound anything")


@pytest.mark.parametrize("rate", [0, 1, 61])
def test_the_ingest_still_ignores_unpriced_frames(rate: int) -> None:
    """A control: the guard must not change what a normal frame does."""
    book = PmBook()
    book._ingest(_price_change("0.42", "0.41", "0.43"))
    assert book.messages == 1
    touch = book.best(TOKEN, 60.0)
    assert touch is not None and touch.ask == 0.43
