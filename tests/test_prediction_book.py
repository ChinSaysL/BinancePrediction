"""The websocket order book: parsing, the complete-set derivation, staleness.

Every field here was verified against the live venue on 2026-09-13, because
the published docs are wrong in three places (see prediction_book's module
docstring). These tests pin the ERRATA, not the docs.
"""
from __future__ import annotations

import json
import time

import pytest

from btcpredict.feeds.prediction_book import BookTop, PredictionBook

MID = 10930648


def frame(up_bid=0.46, up_ask=0.47, bid_sz=100.0, ask_sz=200.0, mid=MID,
          msg_type="orderbook", env_type="DATA"):
    body = json.dumps({
        "msgType": msg_type, "marketId": mid,
        "updateTimestampMs": int(time.time() * 1000),
        "asks": [[up_ask, ask_sz], [up_ask + 0.01, 50.0]],
        "bids": [[up_bid, bid_sz], [up_bid - 0.01, 50.0]],
    })
    return json.dumps({"type": env_type, "topic": f"x_{mid}", "data": body})


def book():
    return PredictionBook(api_key="k", api_secret="s", session=None, signer=None)


class TestTheCompleteSetDerivation:
    """The feed carries ONE book -- the Up token. Verified live: WS bid/ask
    matched the REST Up book exactly (distance 0.000) while the Down book sat
    0.140 away. Down is derived, and the identity holds on 99.5% of 126k
    recorded quotes."""

    def test_down_ask_is_one_minus_up_bid(self):
        t = BookTop(up_bid=0.46, up_ask=0.47, up_ask_size=1, up_bid_size=1,
                    ts_ms=0, recv_ms=0)
        assert t.down_ask == pytest.approx(0.54)

    def test_down_bid_is_one_minus_up_ask(self):
        t = BookTop(up_bid=0.46, up_ask=0.47, up_ask_size=1, up_bid_size=1,
                    ts_ms=0, recv_ms=0)
        assert t.down_bid == pytest.approx(0.53)

    def test_the_two_asks_sum_above_one(self):
        """Both sides are asks, so they carry the spread twice -- this is the
        sanity check that the derivation has not been inverted into bids."""
        t = BookTop(up_bid=0.46, up_ask=0.47, up_ask_size=1, up_bid_size=1,
                    ts_ms=0, recv_ms=0)
        assert t.up_ask + t.down_ask > 1.0

    def test_ask_dollars_flips_with_the_side(self):
        """Buying Down consumes the Up BID queue, because a Down buy is an Up
        sell. Reading the ask queue for a Down order would size against
        liquidity that is not there."""
        t = BookTop(up_bid=0.40, up_ask=0.60, up_ask_size=10.0,
                    up_bid_size=3.0, ts_ms=0, recv_ms=0)
        assert t.ask_dollars("Up") == pytest.approx(0.60 * 10.0)
        assert t.ask_dollars("Down") == pytest.approx((1 - 0.40) * 3.0)


class TestIngest:
    def test_a_data_frame_updates_the_top(self):
        b = book()
        b._ingest(frame(up_bid=0.46, up_ask=0.47))
        t = b.tops[MID]
        assert (t.up_bid, t.up_ask) == (0.46, 0.47)
        assert b.messages == 1

    def test_command_frames_are_ignored(self):
        """The venue sends {"type":"COMMAND",...,"subType":"REGISTER"} on
        connect; treating it as a book would raise."""
        b = book()
        b._ingest(json.dumps({"type": "COMMAND", "data": "SUCCESS",
                              "subType": "REGISTER", "code": "00000000"}))
        assert b.tops == {} and b.messages == 0

    def test_a_one_sided_book_is_refused(self):
        b = book()
        b._ingest(json.dumps({"type": "DATA", "data": json.dumps(
            {"msgType": "orderbook", "marketId": MID,
             "updateTimestampMs": 1, "asks": [[0.5, 1.0]], "bids": []})}))
        assert b.tops == {}, "a half book cannot price either side"

    @pytest.mark.parametrize("bad", [
        "not json", "{}", '{"type":"DATA"}',
        '{"type":"DATA","data":"not json"}',
        '{"type":"DATA","data":"{\\"msgType\\":\\"other\\"}"}',
    ])
    def test_malformed_frames_never_raise(self, bad):
        """This runs inside the receive loop; an exception here kills the feed
        and silently reverts every arm to the 1s poll."""
        b = book()
        b._ingest(bad)
        assert b.tops == {}

    def test_prices_arrive_as_numbers_not_strings(self):
        """Docs errata: the published schema says strings. Live sends numbers.
        Parsing must accept either."""
        b = book()
        b._ingest(json.dumps({"type": "DATA", "data": json.dumps(
            {"msgType": "orderbook", "marketId": MID, "updateTimestampMs": 1,
             "asks": [["0.47", "200"]], "bids": [["0.46", "100"]]})}))
        assert b.tops[MID].up_ask == pytest.approx(0.47)


class TestStaleness:
    def test_a_fresh_book_is_returned(self):
        b = book()
        b._ingest(frame())
        assert b.best(MID, max_age_s=2.0) is not None

    def test_a_stale_book_is_refused(self):
        """The whole point of the fallback: a frozen feed pricing orders off a
        dead book is worse than the REST poll it replaced."""
        b = book()
        b._ingest(frame())
        b.tops[MID].recv_ms -= 10_000
        assert b.best(MID, max_age_s=2.0) is None

    def test_an_unknown_market_is_refused(self):
        assert book().best(999, max_age_s=2.0) is None

    def test_a_missing_market_id_is_refused(self):
        assert book().best(None, max_age_s=2.0) is None


class TestItFollowsTheRollingRound:
    """The topic is a CONNECT-TIME parameter, so following a 5-minute market
    means re-handshaking. The failure mode this guards is silent: a market goes
    quiet the moment its round ends, so a receive loop that blocks on frames
    would never notice it should resubscribe."""

    def test_following_a_new_market_requests_a_resubscribe(self):
        b = book()
        b.follow([1])
        b._resub.clear()
        b.follow([2])
        assert b._resub.is_set()

    def test_following_the_same_market_does_not_churn_the_connection(self):
        b = book()
        b.follow([1])
        b._resub.clear()
        b.follow([1])
        assert not b._resub.is_set(), "re-handshaking every poll would be worse"

    def test_an_empty_market_list_is_ignored(self):
        b = book()
        b.follow([1])
        b._resub.clear()
        b.follow([])
        assert not b._resub.is_set()
        assert b._markets == (1,)

    def test_stop_releases_the_loop(self):
        b = book()
        b.stop()
        assert b._stop.is_set() and b._resub.is_set()


class TestSweepPricing:
    """Pricing the WHOLE order, not the touch.

    The bug: a live Down book held $0.10 at its best price and $2.61 one level
    behind. A cross gated on that touch (0.760) filled at 0.9008, because $1.09
    ate straight through to the limit. The touch is not a price you can buy any
    size at.
    """

    @staticmethod
    def t(asks=(), bids=()):
        a = asks or ((0.50, 10.0),)
        b = bids or ((0.49, 10.0),)
        return BookTop(up_bid=b[0][0], up_ask=a[0][0], up_ask_size=a[0][1],
                       up_bid_size=b[0][1], ts_ms=0, recv_ms=0,
                       asks=tuple(a), bids=tuple(b))

    def test_an_order_inside_the_touch_pays_the_touch(self):
        px, fill = self.t(asks=((0.50, 100.0),)).sweep("Up", 1.0)
        assert px == pytest.approx(0.50) and fill == pytest.approx(1.0)

    def test_an_order_past_the_touch_pays_the_weighted_average(self):
        # level 1 holds 1.0 share at 0.20, i.e. $0.20 of the order.
        # the remaining $0.80 buys 0.80/0.40 = 2.0 shares at 0.40.
        # 3.0 shares for $1.00 -> a volume-weighted 0.3333, NOT the 0.20 touch.
        px, fill = self.t(asks=((0.20, 1.0), (0.40, 100.0))).sweep("Up", 1.0)
        assert fill == pytest.approx(1.0)
        assert 0.20 < px < 0.40
        assert px == pytest.approx(1.0 / (1.0 + 0.80 / 0.40), rel=1e-6)

    def test_it_reports_how_much_the_book_can_actually_absorb(self):
        px, fill = self.t(asks=((0.50, 4.0),)).sweep("Up", 99.0)
        assert fill == pytest.approx(2.0), "only $2 is resting"
        assert px == pytest.approx(0.50)

    def test_an_empty_book_fills_nothing(self):
        px, fill = self.t(asks=()).sweep("Up", 1.0)
        b = BookTop(up_bid=0.4, up_ask=0.5, up_ask_size=0, up_bid_size=0,
                    ts_ms=0, recv_ms=0, asks=(), bids=())
        assert b.sweep("Up", 1.0) == (0.0, 0.0)

    def test_a_down_sweep_walks_the_up_bids_downward(self):
        """Up bids descend (0.60, 0.59, ...); as Down prices those ASCEND
        (0.40, 0.41, ...), which is the order a Down buyer consumes them."""
        b = self.t(bids=((0.60, 1.0), (0.59, 100.0)))
        assert b.levels("Down") == [(0.40, 1.0), (0.41, 100.0)]
        px, fill = b.sweep("Down", 1.0)
        assert 0.40 < px < 0.41

    def test_zero_and_negative_levels_are_skipped(self):
        b = self.t(asks=((0.0, 5.0), (0.30, 0.0), (0.50, 10.0)))
        px, fill = b.sweep("Up", 1.0)
        assert px == pytest.approx(0.50) and fill == pytest.approx(1.0)


class TestFollowIsAdditive:
    """Removals must not cost a handshake -- that was a live regression."""

    def _book(self):
        from btcpredict.feeds.prediction_book import PredictionBook
        b = PredictionBook.__new__(PredictionBook)
        b._markets = ()
        import asyncio
        b._resub = asyncio.Event()
        return b

    def test_a_new_market_resubscribes(self):
        b = self._book()
        b.follow([1, 2])
        assert set(b._markets) == {1, 2} and b._resub.is_set()

    def test_a_market_dropping_out_does_NOT_resubscribe(self):
        b = self._book()
        b.follow([1, 2, 3])
        b._resub.clear()
        b.follow([1, 2])              # 3's round rolled over
        assert not b._resub.is_set(), "a removal must not force a handshake"
        assert 3 in b._markets, "the dead topic just goes quiet; keep it"

    def test_flapping_market_handshakes_once_not_every_poll(self):
        b = self._book()
        b.follow([1, 2, 3])
        b._resub.clear()
        for _ in range(20):           # 3 flapping in and out, as live did
            b.follow([1, 2])
            b.follow([1, 2, 3])
        assert not b._resub.is_set()

    def test_the_set_is_bounded(self):
        from btcpredict.feeds.prediction_book import PredictionBook
        b = self._book()
        for i in range(PredictionBook.MAX_TOPICS * 3):
            b.follow([1000 + i])
        assert len(b._markets) <= PredictionBook.MAX_TOPICS
