"""The pre-order unbooked check sat on the ORDER'S critical path.

`_unbooked_round_tokens` runs in the gate chain immediately before the order is
sent, and it is a signed GET: 60 ms median against an 8 ms network RTT. The
full decision -> venue createTime is 290 ms (~60 here, ~52 get_quote, ~60
place-order). An arm racing a crossing has its whole edge inside ~100 ms, so
this was a fifth of the budget spent on a value no fresher than the 1 s poll
that consumes it.
"""
import asyncio

import pytest

import btcpredict.config as cfg
from btcpredict.main import Bot


def _bot(pos_seq):
    b = Bot.__new__(Bot)
    b.calls = 0

    class _Brk:
        mode = "live"
        async def positions(_self):
            b.calls += 1
            return pos_seq[min(b.calls - 1, len(pos_seq) - 1)]

    b.broker = _Brk()
    return b


def _toks(b, **kw):
    return asyncio.run(b._venue_position_tokens(**kw))


class TestTheCache:
    def test_off_by_default_every_call_refetches(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "venue_position_cache_s", 0.0)
        b = _bot([[{"tokenId": "a"}]])
        _toks(b); _toks(b); _toks(b)
        assert b.calls == 3, "0 must mean always refetch, the old behaviour"

    def test_within_the_ttl_it_reuses(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "venue_position_cache_s", 5.0)
        b = _bot([[{"tokenId": "a"}]])
        assert _toks(b) == {"a"}
        assert _toks(b) == {"a"}
        assert b.calls == 1, "the second read must not hit the venue"

    def test_max_age_zero_forces_ground_truth(self, monkeypatch):
        """Settlement and claim sweeps must never read a cached list."""
        monkeypatch.setattr(cfg.settings, "venue_position_cache_s", 5.0)
        b = _bot([[{"tokenId": "a"}], [{"tokenId": "b"}]])
        assert _toks(b) == {"a"}
        assert _toks(b, max_age_s=0) == {"b"}
        assert b.calls == 2


class TestItCannotHideAPositionWeJustOpened:
    def test_booking_a_fill_invalidates(self, monkeypatch):
        """The case the check exists for -- an order that filled without being
        booked -- must still read a fresh list on the next attempt."""
        monkeypatch.setattr(cfg.settings, "venue_position_cache_s", 5.0)
        b = _bot([[{"tokenId": "a"}], [{"tokenId": "a"}, {"tokenId": "b"}]])
        assert _toks(b) == {"a"}
        b._invalidate_venue_positions()
        assert _toks(b) == {"a", "b"}
        assert b.calls == 2

    def test_book_fill_calls_the_invalidation(self):
        """Pinned in the source: an invalidation that is never called is worse
        than no cache, because it reads as safe."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "btcpredict" / "main.py").read_text()
        body = src[src.index("async def _book_fill"):]
        body = body[:body.index("\n    async def ", 10)]
        assert "self._invalidate_venue_positions()" in body

    def test_a_venue_error_does_not_poison_the_cache(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "venue_position_cache_s", 5.0)
        b = Bot.__new__(Bot)

        class _Brk:
            mode = "live"
            async def positions(_s):
                raise RuntimeError("venue down")

        b.broker = _Brk()
        assert _toks(b) is None
        assert getattr(b, "_vpos_cache", None) is None, (
            "a failed read must not be cached as 'no positions'"
        )
