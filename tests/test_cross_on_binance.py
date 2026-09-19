"""The cross arm must behave IDENTICALLY on Binance and on Polymarket.

WHY THIS FILE EXISTS. The operator moved off Binance on 2026-09-16 because
orders failed too often, and everything built since -- the venue re-base
(`cross_venue_take`), the padded decision band (`cross_ladder_band_pad`), the
prefetch -- was written against Polymarket's CLOB and tested only there.

⛔ THE BUG THIS CAUGHT. `venue_top()` is the Polymarket `/book` read, hardcoded.
On Binance it simply returns None, so `base_src` never becomes "venue" -- and
the pad's fail-closed branch refuses any order whose price it could not check
against the real book. With the live `CROSS_LADDER_BAND_PAD=0.10`, setting
`VENUE=binance` would have refused EVERY cross order, silently, while the arm
looked perfectly healthy in the log.

The two venues offer the same read under different names:
    Polymarket   GET /book?token_id=
    Binance      SAPI /w3w/wallet/prediction/order-book, via best_prices()
`Bot._venue_top` dispatches between them so the arm's logic is one code path.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from btcpredict.main import Bot


def _bot(monkeypatch, venue, *, best=(0.58, 0.61), raises=False, has_bp=True):
    monkeypatch.setattr("btcpredict.config.settings.venue", venue)
    b = Bot.__new__(Bot)
    calls = []

    async def best_prices(token, market_id):
        calls.append((token, market_id))
        if raises:
            raise RuntimeError("venue said no")
        return best

    b.broker = NS(mode="live") if not has_bp else NS(mode="live", best_prices=best_prices)
    b._calls = calls
    return b


R = NS(slug="btc-updown-5m-1", market_id=4242, strike=100.0)


class TestTheBinancePath:
    def test_it_reads_the_binance_order_book(self, monkeypatch) -> None:
        """Same role as the CLOB read: an independent view of the book we PAY
        into, which is the only source that can disagree with our ladder."""
        b = _bot(monkeypatch, "binance")
        got = asyncio.run(b._venue_top("TOK", R))
        assert got is not None
        assert got.ask == pytest.approx(0.61)
        assert got.bid == pytest.approx(0.58)
        assert b._calls == [("TOK", 4242)], "must pass the market id through"

    def test_it_does_not_call_the_polymarket_clob(self, monkeypatch) -> None:
        async def boom(_t):
            raise AssertionError("the Polymarket CLOB was called on Binance")
        monkeypatch.setattr("btcpredict.main.venue_top", boom)
        b = _bot(monkeypatch, "binance")
        assert asyncio.run(b._venue_top("TOK", R)) is not None

    def test_a_raising_broker_is_a_fallback_not_a_crash(self, monkeypatch) -> None:
        """An unreadable venue book is a fallback, never an exception -- the
        order path catches nothing here."""
        b = _bot(monkeypatch, "binance", raises=True)
        assert asyncio.run(b._venue_top("TOK", R)) is None

    def test_a_broker_without_best_prices_returns_none(self, monkeypatch) -> None:
        b = _bot(monkeypatch, "binance", has_bp=False)
        assert asyncio.run(b._venue_top("TOK", R)) is None

    def test_no_market_id_no_read(self, monkeypatch) -> None:
        """Binance keys the book on the market, not the token alone."""
        b = _bot(monkeypatch, "binance")
        assert asyncio.run(b._venue_top("TOK", NS(slug="s", market_id=None))) is None
        assert b._calls == []

    def test_a_zero_ask_is_not_a_book(self, monkeypatch) -> None:
        b = _bot(monkeypatch, "binance", best=(0.0, 0.0))
        assert asyncio.run(b._venue_top("TOK", R)) is None


class TestThePolymarketPathIsUnchanged:
    def test_polymarket_still_uses_the_clob_read(self, monkeypatch) -> None:
        seen = []

        async def fake(tok):
            seen.append(tok)
            return NS(bid=0.30, ask=0.62)
        monkeypatch.setattr("btcpredict.main.venue_top", fake)
        b = _bot(monkeypatch, "polymarket")
        got = asyncio.run(b._venue_top("TOK", R))
        assert got.ask == pytest.approx(0.62)
        assert seen == ["TOK"]
        assert b._calls == [], "must NOT call the Binance order book"


class TestThePadDoesNotBlockBinance:
    def test_the_pad_would_have_refused_every_order_before_this(self) -> None:
        """⛔ THE REGRESSION THIS FILE EXISTS FOR.

        `_place_order` refuses outright when the band is padded and the venue
        book could not be read (`base_src != "venue"`). That guard is correct
        -- a pad widens the DECISION band, so without the real book nothing
        checked the price we pay. But it is only safe because a venue read is
        ACTUALLY AVAILABLE. Hardcoding the Polymarket reader made that false on
        Binance, turning a safety net into a total refusal.

        Pinned structurally: the order path must go through the dispatcher.
        """
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "btcpredict" / "main.py").read_text()
        assert "vt = await self._venue_top(venue_token, r)" in src, (
            "the order path calls the Polymarket reader directly again: on "
            "Binance base_src can never be 'venue' and the padded band will "
            "refuse every order")
        assert "vt = await venue_top(venue_token)" not in src

    def test_the_prefetch_also_goes_through_the_dispatcher(self) -> None:
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "btcpredict" / "main.py").read_text()
        i = src.index("async def _prefetch_venue_top")
        j = src.index("def _take_prefetched_venue_top")
        assert "await self._venue_top(token, r)" in src[i:j]
