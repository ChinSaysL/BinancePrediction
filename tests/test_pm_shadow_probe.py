"""The fire-time probe must read the VENUE, not echo our own book back.

WHY THIS EXISTS. The "shadow quote" was written to answer "would a current
reference have been accepted at fire time?", and it could not answer anything.
It called `broker.get_quote()`, whose own docstring reads "the wrapper's quote
shape (NO VENUE CALL)": it re-read OUR book, through the same `Touch.sweep()`
the decision itself used, and returned the decision price to the log --

    shadow quote bnb-updown-15m-... Down: fresh px 0.4000 after 0ms

A 0 ms "fresh" quote is the tell. A measurement that cannot disagree with the
thing it audits is not a measurement, and this one went on certifying a ladder
that was 45 cents out of date:

    cross bnb-updown-15m-... Down: $1.00 sweeps to 0.610 (touch 0.160, +0.450)

Both numbers came from us. Nothing on the box could say which was dead.
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib

import pytest

from btcpredict.venues.polymarket.book import PmBook

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ROOT / "btcpredict" / "main.py"

TOKEN = "2142747268841947922058145992212420839994106551647125213235616395159528206079"


def _shadow_node() -> ast.AsyncFunctionDef:
    for n in ast.walk(ast.parse(MAIN.read_text())):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_shadow_quote":
            return n
    pytest.fail("_shadow_quote is gone: this guard would be measuring nothing")


def _called_names(node: ast.AST) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            fn = n.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name:
                out.add(name)
    return out


class _Resp:
    def __init__(self, payload, status=200):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload


class _Session:
    """Minimal aiohttp-shaped session; records the URLs it was asked for."""

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.urls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        return _Resp(self.payload, self.status)

    def closed(self):                                     # pragma: no cover
        return False


def _book_payload(asks, bids=((0.15, 40.0),)):
    return {"timestamp": "1789630140010",
            "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}


class TestTheProbe:
    def test_it_reads_the_venue_and_parses_its_book(self) -> None:
        s = _Session(_book_payload([(0.16, 500.0), (0.17, 900.0)]))
        t = asyncio.run(PmBook.fetch_top(s, TOKEN))
        assert t is not None
        assert (t.bid, t.ask) == (0.15, 0.16)
        assert t.sweep(50.0) == pytest.approx((0.16, 50.0))
        assert s.urls == [f"https://clob.polymarket.com/book?token_id={TOKEN}"]

    def test_it_cannot_move_the_book_the_arms_trade_on(self) -> None:
        """A probe that writes to `tops` would be a second, unvetted feed."""
        live = PmBook()
        live._ingest(json.dumps({"event_type": "book", "asset_id": TOKEN,
                                 "timestamp": "1",
                                 "bids": [{"price": "0.60", "size": "10"}],
                                 "asks": [{"price": "0.61", "size": "10"}]}))
        before = live.best(TOKEN, 60.0)
        s = _Session(_book_payload([(0.16, 500.0)]))
        asyncio.run(PmBook.fetch_top(s, TOKEN))
        after = live.best(TOKEN, 60.0)
        assert (after.bid, after.ask) == (before.bid, before.ask) == (0.60, 0.61)

    def test_a_probe_failure_is_not_an_exception(self) -> None:
        """It runs beside the fire path; it must never be able to raise into it."""
        assert asyncio.run(PmBook.fetch_top(_Session({}, 403), TOKEN)) is None

    def test_a_one_sided_venue_book_is_reported_not_discarded(self) -> None:
        """No asks is a real state, and it must reach the caller as one.

        This venue empties one side near a round's end (measured: "Up 0 bids /
        99 asks while Down had 99 bids / 0 asks"). Returning None -- or the
        previous two-sided touch -- would hide it; `ask == 0` lets the arm
        refuse honestly and `sweep` return nothing.
        """
        t = asyncio.run(PmBook.fetch_top(_Session(_book_payload([])), TOKEN))
        assert t is not None and t.ask == 0.0 and t.bid == pytest.approx(0.15)
        assert t.levels() == []
        assert t.sweep(1.0) == (0.0, 0.0)

    def test_an_empty_token_short_circuits(self) -> None:
        s = _Session(_book_payload([(0.16, 500.0)]))
        assert asyncio.run(PmBook.fetch_top(s, "")) is None
        assert s.urls == []


class TestTheShadowQuoteIsNotATautology:
    def test_it_does_not_call_the_local_quote(self) -> None:
        """`get_quote` is documented "no venue call"; that is the bug."""
        assert "get_quote" not in _called_names(_shadow_node()), (
            "the shadow quote went back to echoing our own book back; it would "
            "certify a stale ladder again")

    def test_it_calls_the_venue_probe(self) -> None:
        assert "fetch_top" in _called_names(_shadow_node())

    def test_the_comparison_is_logged_not_left_implicit(self) -> None:
        """The verdict has to be readable without subtracting two floats.

        `venue <.4f>/.<4f>` and `ours <.4f>/.<4f>` are BOTH sides of both books,
        and `spread ... vs venue ...` is what separates a feed that fell BEHIND
        (whole book shifted) from a ladder that is wrong on one side. Measured
        2026-09-17 08:49 the line was `venue ask 0.2000 | ours touch 0.1100`
        with nothing in it to say which of those two readings applied.
        """
        src = ast.unparse(_shadow_node())
        for needle in ("venue ", "ours ", "ours sweep", "age", "spread"):
            assert needle in src, f"the log line no longer reports {needle!r}"

    def test_it_reports_both_sides_of_our_own_round(self) -> None:
        """One number cannot separate LAG from A SWAPPED SIDE.

        Measured 2026-09-17 the venue read 0.82/0.83 while our Up read 0.57/0.58,
        and 12 s later 0.75/0.78 against 0.56/0.60 -- a CONSTANT ~0.7 ratio. An
        old book drifts; the wrong side keeps a fixed relationship. Logging our
        other side makes the two one subtraction apart.
        """
        src = ast.unparse(_shadow_node())
        assert "ours_other" in src, (
            "without our own other side, a swapped token and a 0.5 s lag look "
            "identical in the log")
