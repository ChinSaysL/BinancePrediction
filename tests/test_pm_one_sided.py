"""A one-sided book is a STATE, not a parse failure.

THE BUG THIS PINS (measured live 2026-09-17). Both ingest paths bailed out when
either side was missing:

    _apply_delta:     if bb <= 0 or ba <= 0: return
    _apply_snapshot:  if not bids or not asks: return   # one-sided: unusable

That is wrong for this venue, whose books really do empty one side near a
round's end -- `book.py`'s own note records "Up 0 bids / 99 asks while Down had
99 bids / 0 asks". The cost was not a wrong price but a FROZEN one: every frame
for a one-sided asset was dropped, so `Touch` kept whatever its last two-sided
frame said and went on looking live and fresh. Live, the arm asked for a side
the venue had no ask for and got an all-zero probe:

    09:29:52 cross btc-updown-15m-1789636500 Down not taken: no ask on the book
    09:29:53 cross fine btc-updown-15m-1789636500 Down refuse: 05=0.000 ... 100=0.000

Note the second line is the SIZING PROBE, not the book: with `ask == 0` the
probe correctly reports it can buy nothing. The refusal was right; the silence
about WHY was the bug.

THE RULE the tests below check from both sides:

    A side is touched only when the frame actually speaks about it.
      absent key  -> that side is UNCHANGED (the venue did not restate it)
      key == 0    -> that side is EMPTY   (the venue said so)
    and the previous side must never be carried forward as if it were live.
"""
from __future__ import annotations

import json

import pytest

from btcpredict.venues.polymarket.book import PmBook

UP = "214274726884"

_TS = 1_789_636_500_000


def _snapshot(asset: str, bids: list, asks: list, ts: int = _TS) -> str:
    return json.dumps({
        "event_type": "book", "asset_id": asset, "timestamp": str(ts),
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
    })


def _change(asset: str, *, px, sz, side, bb=None, ba=None, ts: int = _TS) -> str:
    """One frame, one entry. `None` means the venue did not send that key."""
    ch = {"asset_id": asset, "price": str(px), "size": str(sz), "side": side}
    if bb is not None:
        ch["best_bid"] = str(bb)
    if ba is not None:
        ch["best_ask"] = str(ba)
    return json.dumps({
        "event_type": "price_change", "timestamp": str(ts),
        "price_changes": [ch]})


def _two_sided(bk: PmBook) -> None:
    bk._ingest(_snapshot(UP, [(0.15, 100), (0.14, 100)], [(0.17, 100), (0.18, 100)]))


class TestDelta:
    def test_an_empty_side_empties_it_and_keeps_the_other(self) -> None:
        bk = PmBook()
        _two_sided(bk)
        # The venue reports NO BIDS: `best_bid` 0, the ask untouched.
        bk._ingest(_change(UP, px=0.18, sz=100, side="SELL", bb=0, ba=0.17))
        t = bk.tops[UP]
        assert t.bid == 0.0 and t.bid_size == 0.0
        assert not t.bids
        assert t.ask == 0.17 and t.ask_size > 0
        # The price the arm would have paid before this fix was the stale 0.17
        # from a book that still had bids. Now the miss is on the correct side.
        assert t.levels() == [(0.17, 100.0), (0.18, 100.0)]

    def test_no_asks_refuses_to_price_a_sweep(self) -> None:
        bk = PmBook()
        _two_sided(bk)
        bk._ingest(_change(UP, px=0.15, sz=100, side="BUY", bb=0.15, ba=0))
        t = bk.tops[UP]
        assert t.ask == 0.0 and not t.asks
        assert t.levels() == []
        assert t.sweep(1.0) == (0.0, 0.0)   # "no ask on the book", honestly
        assert t.bid == 0.15                # the bid side is not collateral

    def test_the_stale_two_sided_touch_is_not_retained(self) -> None:
        """The whole bug in one assertion: the ask must not survive the empty."""
        bk = PmBook()
        _two_sided(bk)
        assert bk.tops[UP].ask == pytest.approx(0.17)
        bk._ingest(_change(UP, px=0.15, sz=100, side="BUY", bb=0.15, ba=0))
        assert bk.tops[UP].ask != pytest.approx(0.17)

    def test_an_absent_key_means_unchanged_not_empty(self) -> None:
        """A delta that restates only the bid must not wipe the ask.

        `best_bid`/`best_ask` of 0 and a MISSING `best_bid`/`best_ask` are
        different statements, and conflating them would let one partial entry
        delete a live ladder.
        """
        bk = PmBook()
        _two_sided(bk)
        bk._ingest(_change(UP, px=0.15, sz=250, side="BUY", bb=0.15))  # no ba
        t = bk.tops[UP]
        assert t.ask == pytest.approx(0.17) and t.ask_size == pytest.approx(100.0)
        assert t.bid == pytest.approx(0.15) and t.bid_size == pytest.approx(250.0)

    def test_a_one_sided_frame_still_carries_the_venue_clock(self) -> None:
        """Dropping these frames also starved `lag_ms` of the only clock it has."""
        bk = PmBook()
        _two_sided(bk)
        before = bk.last_ts_ms
        bk._ingest(_change(UP, px=0.15, sz=10, side="BUY", bb=0.15, ba=0,
                           ts=_TS + 7_000))
        assert bk.last_ts_ms == before + 7_000

    def test_a_frame_without_either_key_is_still_ignored(self) -> None:
        bk = PmBook()
        _two_sided(bk)
        bk._ingest(json.dumps({
            "event_type": "price_change", "timestamp": str(_TS + 1000),
            "price_changes": [{"asset_id": UP, "price": "0.15", "size": "1",
                               "side": "BUY"}]}))
        # Nothing was claimed, so nothing changed -- in particular not to zero.
        assert bk.tops[UP].bid == pytest.approx(0.15)
        assert bk.tops[UP].ask == pytest.approx(0.17)


class TestSnapshot:
    def test_a_one_sided_snapshot_is_stored(self) -> None:
        bk = PmBook()
        bk._ingest(_snapshot(UP, [(0.84, 500)], []))
        t = bk.tops[UP]
        assert t.bid == 0.84 and t.bid_size == 500.0
        assert t.ask == 0.0 and t.ask_size == 0.0
        assert t.levels() == []

    def test_a_one_sided_snapshot_replaces_a_two_sided_one(self) -> None:
        bk = PmBook()
        _two_sided(bk)
        bk._ingest(_snapshot(UP, [(0.84, 500)], [], ts=_TS + 5_000))
        assert bk.tops[UP].ask == 0.0
        assert bk.tops[UP].bid == 0.84

    def test_a_snapshot_with_no_levels_at_all_is_ignored(self) -> None:
        """Not a book: an empty frame must not erase a live touch."""
        bk = PmBook()
        _two_sided(bk)
        bk._ingest(_snapshot(UP, [], [], ts=_TS + 5_000))
        assert bk.tops[UP].bid == pytest.approx(0.15)
        assert bk.tops[UP].ask == pytest.approx(0.17)

    def test_a_one_sided_snapshot_advances_the_venue_clock(self) -> None:
        bk = PmBook()
        bk._ingest(_snapshot(UP, [(0.84, 500)], [], ts=_TS + 3_000))
        assert bk.last_ts_ms == _TS + 3_000
