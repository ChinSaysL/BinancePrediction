"""Percentage sizing must track live equity, not just the balance at startup."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from btcpredict.calibration.report import PnLSummary
from btcpredict.execution.base import Fill, Settlement
from btcpredict.main import Bot
from btcpredict.risk.guards import RiskGuard
from btcpredict.store.db import Store


class FakeLiveBook:
    """Stands in for LiveBroker: a mutable balance plus settlement."""

    mode = "live"

    def __init__(self, balance=76.0, *, fail=False):
        self._balance = balance
        self.fail = fail
        self.balance_calls = 0
        self.redeemed = 0

    async def balance(self):
        self.balance_calls += 1
        if self.fail:
            raise RuntimeError("venue hiccup")
        return self._balance

    async def settle(self, fill, winner):
        won = fill.side == winner
        payout = fill.shares if won else 0.0
        return Settlement(slug=fill.slug, won=won, payout=payout,
                          pnl=payout - fill.stake - fill.fee_paid, ts_ms=9999)

    async def redeem(self):
        self.redeemed += 1
        return {"status": "ok"}


def make_fill(slug="r1", side="Up", stake=3.0, shares=4.8):
    return Fill(slug=slug, side=side, requested_px=0.6, filled_px=0.625,
                shares=shares, stake=stake, fee_paid=0.05, order_id="L1",
                ts_ms=1000)


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = Store(Path(d) / "t.db")
        yield s
        s.close()


def make_bot(store, book, *, bankroll=76.0, open_fills=None):
    bot = Bot.__new__(Bot)                      # skip network setup
    bot.mode = "live"
    bot.store = store
    bot.book = book
    bot.broker = book
    bot.bankroll = bankroll
    bot.open_fills = dict(open_fills or {})
    bot.guard = RiskGuard(daily_loss_cap=0.0, max_consecutive_losses=5,
                          max_open_rounds=1, daily_loss_cap_pct=0.45,
                          bankroll=lambda: bot.bankroll)
    bot.pnl = PnLSummary()
    return bot


def test_refresh_moves_the_bankroll_to_the_live_balance(store):
    bot = make_bot(store, FakeLiveBook(91.25))
    asyncio.run(bot._refresh_bankroll())
    assert bot.bankroll == pytest.approx(91.25)


def test_refresh_is_skipped_while_a_position_is_open(store):
    """walletBalance excludes locked cash, so reading it while invested would
    understate equity and shrink the next stake below the intended fraction."""
    book = FakeLiveBook(20.0)                   # free cash only, rest is invested
    bot = make_bot(store, book, open_fills={"r1": (1, make_fill())})
    asyncio.run(bot._refresh_bankroll())
    assert bot.bankroll == pytest.approx(76.0)
    assert book.balance_calls == 0


def test_refresh_is_a_noop_outside_live_mode(store):
    book = FakeLiveBook(500.0)
    bot = make_bot(store, book)
    bot.mode = "paper"
    asyncio.run(bot._refresh_bankroll())
    assert bot.bankroll == pytest.approx(76.0)


@pytest.mark.parametrize("balance,fail", [(0.0, False), (50.0, True)])
def test_refresh_ignores_an_unreadable_or_broken_balance(store, balance, fail):
    book = FakeLiveBook(balance, fail=fail)
    bot = make_bot(store, book)
    asyncio.run(bot._refresh_bankroll())
    assert bot.bankroll == pytest.approx(76.0)  # the startup balance stands


def test_refresh_ignores_sub_cent_noise(store):
    bot = make_bot(store, FakeLiveBook(76.004))
    asyncio.run(bot._refresh_bankroll())
    assert bot.bankroll == pytest.approx(76.0)


def test_refresh_tolerates_a_bot_without_a_book(store):
    bot = make_bot(store, FakeLiveBook(80.0))
    del bot.book
    asyncio.run(bot._refresh_bankroll())        # must not raise
    assert bot.bankroll == pytest.approx(76.0)


def test_settlement_refreshes_the_bankroll_after_a_win(store):
    """End to end: settle -> redeem the winnings -> re-read equity."""
    book = FakeLiveBook(83.4)
    bot = make_bot(store, book, open_fills={"r1": (7, make_fill())})
    store.upsert_round({"slug": "r1", "start_ms": 0, "end_ms": 1,
                        "market_topic_id": 99, "winner": "Up"})
    asyncio.run(bot.settle_due())

    assert bot.open_fills == {}
    assert book.redeemed == 1                   # winnings claimed first
    assert bot.bankroll == pytest.approx(83.4)  # then equity re-read
    # the percentage stop now sizes off the new number rather than the old one
    assert bot.guard.effective_daily_cap() == pytest.approx(0.45 * 83.4)


def test_an_idle_tick_does_not_call_the_balance_endpoint(store):
    """One read per settled round, not one per 1 Hz tick."""
    book = FakeLiveBook(90.0)
    bot = make_bot(store, book)
    asyncio.run(bot.settle_due())
    assert book.balance_calls == 0
