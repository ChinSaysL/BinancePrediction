"""The ladder must be maintained by DELTAS, and never priced below the touch.

THE INCIDENT THESE TESTS PIN (measured live 2026-09-17). `Touch.sweep()` walks
`Touch.asks`, the level ladder. `PmBook._apply_delta` handled ~1,250 messages/s
but updated only `bid`/`ask`/`*_size` and carried `prev.bids`/`prev.asks`
forward verbatim -- so the ladder was rebuilt ONLY by a `book` snapshot, i.e.
once per socket rather than once per message.

The cross arm prices its entry, sizes it, gates it, computes its LIMIT CEILING
(`buf` is a function of the sweep price) and records its "decided" price -- all
from that sweep. So once the market had moved away from the price at
subscription time, the arm decided on a price that no longer existed and sent a
limit derived from it. Live, in one log line:

    CROSS bnb-updown-15m-... Down sweep_px=0.400 (spot 725.12 vs strike 725.15)
    cross exec bnb-updown-15m-... Down: decided 0.4000 limit 0.42 -> NO FILL
        in 0.93s | committed $0 of $1.00 (0%) | book now 0.6000
    pm FAK no fill ...: no orders found to match with FAK order.

The 0.4000 is the frozen ladder; the 0.6000 is `Touch.ask`, which was live. The
venue was right to kill the order: at a limit of 0.42 with the best ask at 0.60
there was nothing to match. That is the whole gate->fill gap -- it was never
order latency and never adverse selection.

Two properties, and the SECOND is the load-bearing one:

1. A delta updates the ladder at the price it names (absolute size, 0 = gone).
2. `levels()` NEVER returns a level below `Touch.ask`. The touch is the venue's
   own best ask, restated on every message, so a level below it has already been
   taken. Pinning it makes a stale ladder fail SAFE (price high, or refuse)
   instead of OPTIMISTIC (quote a book that is gone).
"""
from __future__ import annotations

import json

import pytest

from btcpredict.venues.polymarket.book import KEEP_LEVELS, PmBook, Touch

TOKEN = "48124750328626668496203837084683052980745111792981631822909675647510676383645"


def _snapshot(asks, bids=((0.01, 1000.0),), ts: str = "1789575965396") -> str:
    return json.dumps({
        "event_type": "book", "asset_id": TOKEN, "timestamp": ts,
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
    })


def _change(px, sz, side, bb, ba, ts: str = "1789575966396") -> str:
    return json.dumps({"event_type": "price_change", "price_changes": [
        {"asset_id": TOKEN, "price": str(px), "size": str(sz), "side": side,
         "best_bid": str(bb), "best_ask": str(ba)}], "timestamp": ts})


def _book_with(asks, bids=((0.01, 1000.0),)) -> PmBook:
    b = PmBook()
    b._ingest(_snapshot(asks, bids))
    return b


class TestTheLiveIncident:
    """The exact logged sequence: decide on a level that has been taken."""

    def test_a_move_away_from_the_snapshot_moves_the_sweep(self) -> None:
        """Pre-fix this returned 0.4000 and the FAK was killed at a 0.42 limit.

        The snapshot is taken at the start of the round (0.40/0.41 asks). The
        market then trades up to 0.60 and the venue reports it on a
        `price_change`. Priced off the frozen ladder the arm decides 0.4000,
        sets `ceil_side = round(0.40 + buf, 2) = 0.42`, and posts a limit that
        cannot cross a 0.60 ask.
        """
        b = _book_with([(0.40, 100.0), (0.41, 200.0)])
        assert b.best(TOKEN, 60.0).ask == 0.40
        # The 0.40/0.41 levels are taken (size 0) and a new best ask prints.
        b._ingest(_change(0.40, 0, "SELL", 0.59, 0.60))
        b._ingest(_change(0.41, 0, "SELL", 0.59, 0.60))
        b._ingest(_change(0.60, 500.0, "SELL", 0.59, 0.60))

        t = b.best(TOKEN, 60.0)
        assert (t.bid, t.ask) == (0.59, 0.60)
        px, fillable = t.sweep(1.0)
        assert px == pytest.approx(0.60), (
            f"the sweep priced {px}, i.e. a book that is gone; the live limit "
            "ceiling is derived from this number")
        assert fillable == pytest.approx(1.0)
        assert round(min(0.90, round(px + 0.02, 2)), 2) == pytest.approx(0.62), (
            "the derived ceiling must now cross the 0.60 ask")

    def test_the_touch_is_never_quoted_from_below(self) -> None:
        """The removals above are the venue being polite. Do not rely on them.

        A missed `size 0` delta is exactly what a `_drain()` mid-backlog skip
        produces, and the drain's own comment calls itself safe because "a
        snapshot replaces the ladder whole" -- which was the assumption this
        bug falsified. Here the dead 0.40/0.41 levels are never removed, and
        the entry only ever names the NEW price.
        """
        b = _book_with([(0.40, 100.0), (0.41, 200.0)])
        b._ingest(_change(0.60, 500.0, "SELL", 0.59, 0.60))
        t = b.best(TOKEN, 60.0)
        assert t.ask == 0.60
        assert [p for p, _ in t.levels()] == [0.60], (
            "a level at 0.40 is still in `asks` and would be quoted")
        assert t.sweep(1.0)[0] == pytest.approx(0.60)

    def test_the_levels_guard_holds_on_a_touch_it_did_not_build(self) -> None:
        """Belt and braces: the guard does not trust the ingest to prune.

        `Touch` is the read surface for the arms, so the invariant is stated
        on the READ -- any future path that builds a ladder (a REST snapshot, a
        recorder replay, a test fixture) inherits it for free.
        """
        t = Touch(asset_id=TOKEN, bid=0.59, bid_size=10.0, ask=0.60,
                  ask_size=500.0, ts_ms=0, recv_ms=0,
                  asks=((0.40, 100.0), (0.41, 200.0), (0.61, 50.0)))
        assert t.levels() == [(0.60, 500.0), (0.61, 50.0)]
        assert t.sweep(1.0)[0] == pytest.approx(0.60)


class TestTheLadder:
    def test_levels_always_start_at_the_touch(self) -> None:
        b = _book_with([(0.40, 100.0), (0.41, 200.0)])
        b._ingest(_change(0.55, 300.0, "SELL", 0.54, 0.55))
        t = b.best(TOKEN, 60.0)
        assert t.levels()[0] == (0.55, 300.0)

    def test_a_delta_below_the_touch_is_a_real_new_best_ask(self) -> None:
        """Downward moves are legitimate and must not be filtered out."""
        b = _book_with([(0.60, 500.0)])
        b._ingest(_change(0.55, 300.0, "SELL", 0.54, 0.55))
        t = b.best(TOKEN, 60.0)
        assert t.ask == 0.55
        assert t.levels() == [(0.55, 300.0), (0.60, 500.0)]
        # $165 sits at the touch, so a $200 sweep must walk into the 0.60 level
        # and price between the two rather than at the touch.
        px, fillable = t.sweep(200.0)
        assert fillable == pytest.approx(200.0)
        assert 0.55 < px < 0.60, (
            f"a sweep deeper than the touch priced {px}; it took only one level")

    def test_a_zero_size_delta_removes_the_level(self) -> None:
        b = _book_with([(0.40, 100.0), (0.41, 200.0)])
        b._ingest(_change(0.41, 0, "SELL", 0.39, 0.40))
        assert b.best(TOKEN, 60.0).levels() == [(0.40, 100.0)]

    def test_the_touch_size_follows_the_touch_price(self) -> None:
        """A carried-forward size belongs to the price it was quoted at."""
        b = _book_with([(0.40, 100.0), (0.41, 200.0)])
        b._ingest(_change(0.60, 500.0, "SELL", 0.59, 0.60))
        t = b.best(TOKEN, 60.0)
        assert (t.ask, t.ask_size) == (0.60, 500.0), (
            "the old 0.40 size must not be relabelled onto a 0.60 touch")
        assert (t.bid, t.bid_size) == (0.59, 0.0)

    def test_the_size_is_absolute_not_an_increment(self) -> None:
        """Observed on the venue: 11360.15 at 0.01 came back as 11355.15."""
        b = _book_with([(0.40, 11360.15)])
        b._ingest(_change(0.40, 11355.15, "SELL", 0.39, 0.40))
        assert b.best(TOKEN, 60.0).ask_size == pytest.approx(11355.15)

    def test_the_bid_side_is_maintained_the_same_way(self) -> None:
        b = _book_with([(0.40, 100.0)], bids=((0.39, 50.0),))
        b._ingest(_change(0.45, 20.0, "BUY", 0.45, 0.46))
        t = b.best(TOKEN, 60.0)
        assert (t.bid, t.bid_size) == (0.45, 20.0)
        assert t.bids[0] == (0.45, 20.0)

    def test_the_ladder_is_capped(self) -> None:
        b = _book_with([(0.40, 100.0)])
        for i in range(1, 40):
            b._ingest(_change(0.40 + i / 1000.0, 10.0, "SELL", 0.39, 0.40 + i / 1000.0))
        t = b.best(TOKEN, 60.0)
        assert len(t.asks) <= KEEP_LEVELS
        assert len(t.levels()) <= KEEP_LEVELS

    def test_a_nameless_delta_is_ignored(self) -> None:
        """A frame with no price cannot move a ladder it does not describe.

        It still restates `best_ask`, so the prune must run on every entry.
        """
        b = _book_with([(0.40, 100.0)])
        b._ingest(json.dumps({"event_type": "price_change", "price_changes": [
            {"asset_id": TOKEN, "size": "10", "side": "SELL",
             "best_bid": "0.39", "best_ask": "0.41"}]}))
        t = b.best(TOKEN, 60.0)
        assert t.ask == 0.41
        assert all(p >= 0.41 - 1e-9 for p, _ in t.levels()), (
            "the dead 0.40 level survived a frame that restated the touch")


class TestTheFallback:
    """When the ladder cannot describe the touch, refuse rather than invent."""

    def test_an_unknown_touch_size_prices_from_the_levels_above_it(self) -> None:
        """Never optimistic: the levels kept are all ABOVE the touch."""
        t = Touch(asset_id=TOKEN, bid=0.59, bid_size=0.0, ask=0.60,
                  ask_size=0.0, ts_ms=0, recv_ms=0,
                  asks=((0.58, 900.0), (0.61, 100.0)))
        assert t.levels() == [(0.61, 100.0)]
        assert t.sweep(10.0)[0] == pytest.approx(0.61)

    def test_an_empty_ladder_and_no_size_refuses(self) -> None:
        t = Touch(asset_id=TOKEN, bid=0.0, bid_size=0.0, ask=0.0, ask_size=0.0,
                  ts_ms=0, recv_ms=0, asks=())
        assert t.levels() == []
        assert t.sweep(1.0) == (0.0, 0.0)

    def test_a_touch_with_no_ladder_still_trades_on_the_touch(self) -> None:
        t = Touch(asset_id=TOKEN, bid=0.39, bid_size=10.0, ask=0.40,
                  ask_size=250.0, ts_ms=0, recv_ms=0, asks=())
        assert t.levels() == [(0.40, 250.0)]
        assert t.sweep(50.0) == pytest.approx((0.40, 50.0))
