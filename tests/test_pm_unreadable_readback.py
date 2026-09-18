"""An order that was SENT and cannot be READ BACK is not an order that failed.

LIVE, 2026-09-17 19:09:12, `btc-updown-5m-1789671900` Down:

    CROSS ... sweep_px=0.320 (spot 76584.98 vs strike 76585.14, tau 50s)
    ERROR cross order failed ... UnexpectedResponseError:
          account trades response items malformed

The FAK had MATCHED at the venue (3.1 shares @ 0.335, $1.05). The round settled
Down -- the side we bought -- and the venue paid $3.13. The bot booked NOTHING:
no `fills` row, no `settlements` row, a released round slot, the day's P&L short
by ~$2.08, and a real position sitting outside every cap the ledger enforces.

The cause is a LAZY PAGINATOR. `list_account_trades()` / `list_positions()` do
not perform the HTTP call when they are called; they do it when they are
ITERATED. So a `try` around the call guards nothing, and a malformed response
raised out of `_trades_since` -- whose contract has always been "return None
when the venue will not say" -- through `_confirm`, through `buy_fak`, and out
of the arm's order path, where `except Exception` logged "order failed" and
wrote the whole thing off.

Three rules, and the tests below pin one each:
  1. a READ must not raise (both list endpoints),
  2. a sent order that cannot be read back is reconciled against the POSITION
     LIST before being voided -- the wallet is what actually holds the shares,
  3. an exception in the order path must not give the round's slot back, because
     whether an order reached the venue is UNKNOWN, not "no".
"""
from __future__ import annotations

import asyncio
import pathlib
import tempfile
import time
from types import SimpleNamespace

import pytest

from btcpredict.execution.base import Fill
from btcpredict.feeds.market_feed import Outcome, Round
from btcpredict.main import Bot
from btcpredict.risk.guards import RiskGuard
from btcpredict.store.db import Store
from btcpredict.venues.polymarket.broker import PmBroker, PmBrokerConfig


class UnexpectedResponseError(Exception):
    """Stand-in for the SDK's -- matched by nothing, because the point is that
    it must never reach a matcher in the first place."""


class ExplodingPaginator:
    """A lazy Paginator: calling it is fine, ITERATING it raises.

    This is the shape of the live failure, and it is why a `try` around the
    call is not a guard. `_records()` iterates, so the raise happened inside
    the function that promised to return None.
    """

    items = None

    def __iter__(self):
        raise UnexpectedResponseError("account trades response items malformed")


class Rows:
    def __init__(self, rows):
        self.items = list(rows)

    def __iter__(self):
        return iter(self.items)


class FakeClient:
    def __init__(self, *, trades=None, positions=None, raise_on_iter=False):
        self.trades = trades if trades is not None else ExplodingPaginator()
        self.positions = positions if positions is not None else Rows([])
        self.raise_on_iter = raise_on_iter

    def list_account_trades(self, token_id=None):
        return self.trades

    def list_positions(self):
        return self.positions


def make_broker(client):
    logs: list[str] = []
    br = PmBroker(book=None, client=client, cfg=PmBrokerConfig(),
                  log=logs.append, creds={})
    br._ensure_client = lambda: br._client
    br.logs = logs
    return br


class TestAReadNeverRaises:
    def test_the_trade_read_gives_up_instead_of_exploding(self):
        """The root cause of the write-off, on its own."""
        br = make_broker(FakeClient())
        assert br._trades_since("tok-up", time.time() - 60.0) is None
        assert any("trade read" in m for m in br.logs), (
            "a read that cannot be parsed must SAY so -- silence is how this "
            "shipped")

    def test_an_unparsable_ROW_is_no_answer_rather_than_a_partial_total(self):
        """A partial total is worse than none: it books a fraction of a fill as
        if it were the whole one, and then the settle path cannot correct it."""
        rows = [SimpleNamespace(asset_id="tok-up", size="garbage",
                                price=0.335, matched_at=time.time())]
        br = make_broker(FakeClient(trades=Rows(rows)))
        assert br._trades_since("tok-up", time.time() - 60.0) is None

    def test_a_good_read_still_works(self):
        rows = [SimpleNamespace(asset_id="tok-up", size=3.1, price=0.335,
                                matched_at=time.time())]
        br = make_broker(FakeClient(trades=Rows(rows)))
        got = br._trades_since("tok-up", time.time() - 60.0)
        assert got is not None
        shares, px, _fee = got
        assert shares == pytest.approx(3.1)
        assert px == pytest.approx(0.335)

    def test_the_position_read_gives_up_instead_of_exploding(self):
        """`settle()` now asks this question to decide whether an unverified
        fill was real, so a raise here would become "the position does not
        exist" -- i.e. voiding a fill the venue actually filled."""
        br = make_broker(FakeClient(positions=ExplodingPaginator()))
        assert br._position_size("tok-up") == 0.0


def _unverified(shares=5.0, px=0.62, side="Up"):
    return Fill(slug="btc-updown-5m-1789671900", side=side,
                requested_px=px, filled_px=px, shares=shares,
                stake=shares * px, fee_paid=0.0, order_id="oid-1",
                ts_ms=int(time.time() * 1000), token_id="tok-up",
                raw="fak ack 400ms status=matched", unverified=True)


class TestThePositionListIsTheSecondSource:
    def _broker(self, *, held, trades=None):
        br = make_broker(FakeClient())
        br._trades_since = lambda *a, **k: trades
        br._position_size = lambda tok: held
        return br

    def test_a_held_token_confirms_the_fill_instead_of_voiding_it(self):
        """The live case: the trade file would not parse, the wallet held the
        shares, and the venue paid. Voiding is for a fill that never happened."""
        br = self._broker(held=3.1)
        fill = _unverified(shares=3.0, px=0.335, side="Down")
        s = asyncio.run(br.settle(fill, "Down"))
        assert not s.void, "a held position is a fill"
        assert fill.unverified is False
        assert fill.shares == pytest.approx(3.0), (
            "a position LARGER than what we booked does not inflate the fill")
        assert s.won and s.payout == pytest.approx(3.0)
        assert s.pnl == pytest.approx(3.0 - fill.stake - fill.fee_paid)

    def test_a_smaller_position_means_a_partial_fill_and_wins(self):
        br = self._broker(held=1.5)
        fill = _unverified(shares=5.0, px=0.62)
        s = asyncio.run(br.settle(fill, "Up"))
        assert not s.void
        assert fill.shares == pytest.approx(1.5)
        assert fill.stake == pytest.approx(1.5 * 0.62)

    def test_the_unreadable_fee_is_charged_from_the_schedule(self):
        """Booking a free fill because the venue's fee is unreadable is the
        optimistic direction, and this is a TAKER order."""
        br = self._broker(held=3.0)
        fill = _unverified(shares=3.0, px=0.335)
        asyncio.run(br.settle(fill, "Down"))
        assert fill.fee_paid > 0.0

    def test_nothing_held_anywhere_still_voids(self):
        """The contract this must NOT break: a fill the venue never made is
        still dropped, and the slot goes back with it."""
        br = self._broker(held=0.0)
        fill = _unverified()
        s = asyncio.run(br.settle(fill, "Up"))
        assert s.void and s.pnl == 0.0

    def test_a_READABLE_trade_record_still_wins_over_the_position(self):
        """The trade record is exact (shares, vwap, fee); the position is
        cumulative and cannot tell two fills apart. Order of preference holds."""
        br = self._broker(held=9.9, trades=(2.5, 0.40, 0.03))
        fill = _unverified(shares=5.0, px=0.62)
        asyncio.run(br.settle(fill, "Up"))
        assert fill.shares == pytest.approx(2.5)
        assert fill.filled_px == pytest.approx(0.40)
        assert fill.fee_paid == pytest.approx(0.03)


# ------------------------------------------------------------- the arm's rule

def _ticks(*prices, end_ms=None, step_ms=1000):
    end = end_ms if end_ms is not None else int(time.time() * 1000)
    n = len(prices)
    return [(end - (n - 1 - i) * step_ms, p) for i, p in enumerate(prices)]


class FakeFeed:
    def __init__(self, t):
        self.ticks = t
        self.price = t[-1][1]
        self.ts_ms = t[-1][0]

    def is_fresh(self, max_age_s):
        return True


class TestAnExceptionDoesNotReleaseTheRound:
    def _bot(self, store):
        now = int(time.time() * 1000)
        r = Round(
            slug="btc-updown-5m-1789671900", market_topic_id=1, market_id=2,
            condition_id="c", symbol="BTCUSDT", start_ms=now - 180_000,
            end_ms=now + 20_000, strike=76585.14, end_price=None,
            price_feed_id=None, feed_provider=None, fee_rate_bps=200,
            liquidity=5000.0, trade_volume=1000.0, status="OPEN",
            trading_status="OPEN", live_status="OPEN",
            outcomes=[Outcome(name="Up", price=0.30, chance=0.30, token_id="1",
                              market_outcome_id=11, winner=None),
                      Outcome(name="Down", price=0.70, chance=0.70, token_id="2",
                              market_outcome_id=12, winner=None)])
        feed = FakeFeed(_ticks(76583.0, 76584.0, 76588.0))
        bot = Bot.__new__(Bot)
        bot.mode = "paper"
        bot.store = store
        bot.book = None
        bot.broker = SimpleNamespace(mode="paper")
        bot.bankroll = 50.0
        bot.open_fills = {}
        bot._primary_spot = feed
        bot.spot = feed
        bot.spots = {"BTCUSDT": feed}
        bot._round = r
        bot._asks = (0.70, 0.30)
        bot._sigma = 0.0002
        bot._cross_fired = {}
        bot._cross_day = ""
        bot._cross_count = 0
        bot._order_cooldown_until = 0.0
        bot._ticks = {}
        bot.guard = RiskGuard(daily_loss_cap=0.0, max_consecutive_losses=5,
                              max_open_rounds=1, daily_loss_cap_pct=0.20,
                              bankroll=lambda: 50.0)
        return bot, r

    def test_the_slot_is_HELD_when_the_order_path_raises(self, monkeypatch,
                                                         tmp_path):
        """Whether an order reached the venue is UNKNOWN, so the round must not
        be re-entered. Releasing the slot here is how the arm takes a second
        entry while the first order is live and unbooked -- exposure the caps
        cannot see, because the caps read a ledger with no row in it."""
        for k, v in (("cross_enabled", True), ("cross_look_s", 2.0),
                     ("cross_min_tau_s", 3.0), ("cross_max_tau_s", 30.0),
                     ("cross_max_price", 0.90), ("cross_min_price", 0.02)):
            monkeypatch.setattr("btcpredict.config.settings." + k, v)
        store = Store(pathlib.Path(tmp_path) / "t.db")
        bot, r = self._bot(store)
        assert bot._cross_level(r)[0] is not None

        async def boom(*a, **k):
            raise UnexpectedResponseError(
                "account trades response items malformed")

        bot._place_order = boom

        async def go():
            await bot._cross_tick()
            await asyncio.sleep(0)          # let the spawned order task run
            await asyncio.sleep(0)

        asyncio.run(go())
        assert bot._cross_fired.get(r.slug, 0) == 1, (
            "the round's slot was given back after an exception, so the arm "
            "would take a second entry into an unknown position")
        store.close()

    def test_the_slot_is_STILL_released_on_a_confirmed_no_fill(self, monkeypatch,
                                                               tmp_path):
        """The other half, and the reason this is not 'never release': a notional
        None from the order path IS a no-fill, and holding the slot there would
        throw away the round's late crossing for nothing."""
        for k, v in (("cross_enabled", True), ("cross_look_s", 2.0),
                     ("cross_min_tau_s", 3.0), ("cross_max_tau_s", 30.0),
                     ("cross_max_price", 0.90), ("cross_min_price", 0.02)):
            monkeypatch.setattr("btcpredict.config.settings." + k, v)
        store = Store(pathlib.Path(tmp_path) / "t.db")
        bot, r = self._bot(store)

        async def nofill(*a, **k):
            return None

        bot._place_order = nofill

        async def go():
            await bot._cross_tick()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(go())
        assert bot._cross_fired.get(r.slug, 0) == 0
        store.close()
