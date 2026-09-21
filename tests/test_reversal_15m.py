"""Unit tests for the 15m Reversal Arm driven by 5m reversals."""
from __future__ import annotations

import asyncio
import pathlib
import tempfile
import time
from collections import deque
import pytest

from btcpredict.config import (
    reversal_15m_families,
    reversal_15m_traded,
    settings,
)
from btcpredict.edge.signal import Signal
from btcpredict.execution.paper import PaperBroker
from btcpredict.feeds.market_feed import Outcome, Round
from btcpredict.main import (
    OTHER_ARM_MODES,
    Bot,
    arm_of,
    pool_for_mode,
)
from btcpredict.risk.guards import RiskGuard
from btcpredict.store.db import Store


def test_reversal_15m_config():
    assert settings.reversal_15m_enabled is True
    assert "btc-updown-15m" in reversal_15m_families()
    assert "eth-updown-15m" in reversal_15m_families()
    assert reversal_15m_traded("btc-updown-15m") is True
    assert reversal_15m_traded("eth-updown-15m") is True
    assert reversal_15m_traded("doge-updown-15m") is False


def test_pool_for_mode_and_arm_of():
    assert "reversal_15m" in OTHER_ARM_MODES
    reserved, pool = pool_for_mode("reversal_15m")
    assert reserved is False
    assert pool == "reversal_15m"

    sig = Signal(side="Up", quote=0.55, fair=0.66, edge=0.11, hurdle=0.0,
                 ev_per_dollar=0.10, act=True, reason="reversal_15m into btc-updown-15m-123")
    assert arm_of(sig) == "REVERSAL_15M"


class FakeSpotFeed:
    def __init__(self, price: float):
        self.price = price
        self.ts_ms = int(time.time() * 1000)
        self.ticks = deque([(self.ts_ms, price)])

    def is_fresh(self, max_age_s: float) -> bool:
        return True


def make_round(slug: str, start_ms: int, end_ms: int, strike: float,
               up_token: str = "tok_up", down_token: str = "tok_dn") -> Round:
    return Round(
        slug=slug, market_topic_id=1, market_id=2, condition_id="c",
        symbol="BTCUSDT", start_ms=start_ms, end_ms=end_ms, strike=strike,
        end_price=None, price_feed_id=None, feed_provider=None, fee_rate_bps=200,
        liquidity=5000.0, trade_volume=1000.0, status="OPEN", trading_status="OPEN",
        live_status="OPEN",
        outcomes=[Outcome(name="Up", price=0.50, chance=0.50, token_id=up_token,
                          market_outcome_id=11, winner=None),
                  Outcome(name="Down", price=0.50, chance=0.50, token_id=down_token,
                          market_outcome_id=12, winner=None)],
    )


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = Store(pathlib.Path(d) / "t.db")
        yield s
        s.close()


def make_test_bot(store) -> Bot:
    bot = Bot.__new__(Bot)
    bot.mode = "paper"
    bot.store = store
    bot.book = None
    bot.broker = PaperBroker()
    bot.open_fills = {}
    bot.bankroll = 100.0
    bot.symbols = ["BTCUSDT"]
    bot._ticks = {}
    bot.guard = RiskGuard(daily_loss_cap=0.0, max_consecutive_losses=5,
                          max_open_rounds=5, daily_loss_cap_pct=0.0,
                          bankroll=lambda: 100.0)
    bot._order_cooldown_until = 0.0
    bot.ws_book = None
    bot._reversal_15m_fired = {}
    bot._reversal_15m_5m_fired = set()
    return bot


def test_reversal_15m_fires_and_books(store, monkeypatch):
    monkeypatch.setattr(settings, "reversal_15m_weekdays_only", False)
    bot = make_test_bot(store)
    now_ms = int(time.time() * 1000)

    # 5m round: started 120s ago, ends in 180s (tau 180s)
    strike_5 = 100.0
    r5 = make_round("btc-updown-5m-100", now_ms - 120_000, now_ms + 180_000, strike_5)
    # 15m round: started 600s ago, ends in 300s (tau 300s <= 360s max)
    r15 = make_round("btc-updown-15m-100", now_ms - 600_000, now_ms + 300_000, strike_5)

    feed = FakeSpotFeed(99.95)  # currently below strike (Down)
    bot.spots = {"BTCUSDT": feed}

    # Setup history in self._ticks:
    # 100s ago: spot 99.95 (Down)
    # 80s ago: spot 100.05 (first_cross -> Up)
    # 40s ago (40s > 25s wait): spot 99.95 (reversal -> Down)
    # now: spot 99.95 (Down)
    t_100s = now_ms - 100_000
    t_80s = now_ms - 80_000
    t_40s = now_ms - 40_000
    bot._ticks["BTCUSDT"] = [
        (t_100s, 99.95),
        (t_80s, 100.05),  # first cross: Up at tau = (180_000 + 80_000)/1000 = 260s (in [60, 270])
        (t_40s, 99.95),   # reversal: Down after 40s wait (> 25s)
        (now_ms, 99.95),
    ]

    bot._mkt = {
        "btc-updown-5m": {"round": r5, "feed": feed, "symbol": "BTCUSDT", "asks": (0.50, 0.50)},
        "btc-updown-15m": {"round": r15, "feed": feed, "symbol": "BTCUSDT", "asks": (0.45, 0.55), "sigma": 0.0002},
    }

    async def run():
        await bot._reversal_15m_tick("btc-updown-15m")
        # Give async background task time to run
        if hasattr(bot, "_order_tasks") and bot._order_tasks:
            await asyncio.gather(*list(bot._order_tasks))

    asyncio.run(run())

    # Verify decision was logged
    decisions = store.conn.execute("SELECT * FROM decisions WHERE slug=?", (r15.slug,)).fetchall()
    assert len(decisions) == 1
    assert decisions[0]["side"] == "Down"

    # Verify fill was booked with mode="reversal_15m"
    fills = store.conn.execute("SELECT * FROM fills WHERE slug=?", (r15.slug,)).fetchall()
    assert len(fills) == 1
    assert fills[0]["mode"] == "reversal_15m"
    assert fills[0]["side"] == "Down"

    # Verify guard pool is occupied
    assert r15.slug in bot.guard.state.pool_rounds["reversal_15m"]


def test_reversal_15m_wait_gate(store, monkeypatch):
    """If elapsed time since first cross is < 25s, it must NOT fire."""
    monkeypatch.setattr(settings, "reversal_15m_weekdays_only", False)
    bot = make_test_bot(store)
    now_ms = int(time.time() * 1000)

    strike_5 = 100.0
    r5 = make_round("btc-updown-5m-200", now_ms - 50_000, now_ms + 250_000, strike_5)
    r15 = make_round("btc-updown-15m-200", now_ms - 300_000, now_ms + 600_000, strike_5)

    feed = FakeSpotFeed(99.0)
    bot.spots = {"BTCUSDT": feed}

    # First cross 15 seconds ago (< 25s)
    t_30s = now_ms - 30_000
    t_15s = now_ms - 15_000
    bot._ticks["BTCUSDT"] = [
        (t_30s, 99.0),
        (t_15s, 101.0),  # first cross Up
        (now_ms, 99.0),   # reversed Down after only 15s
    ]

    bot._mkt = {
        "btc-updown-5m": {"round": r5, "feed": feed, "symbol": "BTCUSDT", "asks": (0.50, 0.50)},
        "btc-updown-15m": {"round": r15, "feed": feed, "symbol": "BTCUSDT", "asks": (0.45, 0.55)},
    }

    async def run():
        await bot._reversal_15m_tick("btc-updown-15m")
        if hasattr(bot, "_order_tasks") and bot._order_tasks:
            await asyncio.gather(*list(bot._order_tasks))

    asyncio.run(run())

    decisions = store.conn.execute("SELECT * FROM decisions WHERE slug=?", (r15.slug,)).fetchall()
    assert len(decisions) == 0


def test_reversal_15m_deduplication(store, monkeypatch):
    """Once fired, it must not fire again in the same 15m or 5m round."""
    monkeypatch.setattr(settings, "reversal_15m_weekdays_only", False)
    bot = make_test_bot(store)
    now_ms = int(time.time() * 1000)

    strike_5 = 100.0
    r5 = make_round("btc-updown-5m-300", now_ms - 120_000, now_ms + 180_000, strike_5)
    r15 = make_round("btc-updown-15m-300", now_ms - 600_000, now_ms + 300_000, strike_5)

    feed = FakeSpotFeed(99.95)
    bot.spots = {"BTCUSDT": feed}
    bot._ticks["BTCUSDT"] = [
        (now_ms - 100_000, 99.95),
        (now_ms - 80_000, 100.05),
        (now_ms - 40_000, 99.95),
        (now_ms, 99.95),
    ]

    bot._mkt = {
        "btc-updown-5m": {"round": r5, "feed": feed, "symbol": "BTCUSDT", "asks": (0.50, 0.50)},
        "btc-updown-15m": {"round": r15, "feed": feed, "symbol": "BTCUSDT", "asks": (0.45, 0.55)},
    }

    async def run():
        # First tick -> fires
        await bot._reversal_15m_tick("btc-updown-15m")
        if hasattr(bot, "_order_tasks") and bot._order_tasks:
            await asyncio.gather(*list(bot._order_tasks))
        assert len(store.conn.execute("SELECT * FROM fills WHERE slug=?", (r15.slug,)).fetchall()) == 1

        # Second tick -> should NOT fire again
        await bot._reversal_15m_tick("btc-updown-15m")
        if hasattr(bot, "_order_tasks") and bot._order_tasks:
            await asyncio.gather(*list(bot._order_tasks))
        assert len(store.conn.execute("SELECT * FROM fills WHERE slug=?", (r15.slug,)).fetchall()) == 1

    asyncio.run(run())


def test_reversal_15m_guard_pool_and_settle(store, monkeypatch):
    """Check that pool_rounds['reversal_15m'] is properly tracked and released on settlement."""
    bot = make_test_bot(store)
    slug = "btc-updown-15m-400"
    reserved, pool = pool_for_mode("reversal_15m")
    assert pool == "reversal_15m"

    # Open position
    bot.guard.on_open(slug, reserved=reserved, pool=pool)
    assert slug in bot.guard.state.pool_rounds["reversal_15m"]

    # When max_open is 1, a second round in pool is refused
    allowed, why = bot.guard.check_pool("btc-updown-15m-401", pool="reversal_15m", max_open=1)
    assert allowed is False
    assert "max open rounds" in why

    # Settle
    bot.guard.on_settle(slug, pnl=+0.80, reserved=reserved, pool=pool)
    assert slug not in bot.guard.state.pool_rounds["reversal_15m"]


def test_reversal_15m_round_range_gate(store, monkeypatch):
    """If round range exceeds the configured maximum, reversal_15m must refuse."""
    monkeypatch.setattr(settings, "reversal_15m_weekdays_only", False)
    monkeypatch.setattr(settings, "reversal_15m_max_round_range_bps", 20.0)
    bot = make_test_bot(store)
    now_ms = int(time.time() * 1000)

    strike_5 = 100.0
    r5 = make_round("btc-updown-5m-500", now_ms - 120_000, now_ms + 180_000, strike_5)
    r15 = make_round("btc-updown-15m-500", now_ms - 600_000, now_ms + 300_000, strike_5)

    feed = FakeSpotFeed(99.0)
    bot.spots = {"BTCUSDT": feed}

    # Huge swing: 99.0 -> 105.0 -> 99.0 = 6% move = 600 bps!
    bot._ticks["BTCUSDT"] = [
        (now_ms - 100_000, 99.0),
        (now_ms - 80_000, 105.0),
        (now_ms - 40_000, 99.0),
        (now_ms, 99.0),
    ]

    bot._mkt = {
        "btc-updown-5m": {"round": r5, "feed": feed, "symbol": "BTCUSDT", "asks": (0.50, 0.50)},
        "btc-updown-15m": {"round": r15, "feed": feed, "symbol": "BTCUSDT", "asks": (0.45, 0.55)},
    }

    async def run():
        await bot._reversal_15m_tick("btc-updown-15m")
        if hasattr(bot, "_order_tasks") and bot._order_tasks:
            await asyncio.gather(*list(bot._order_tasks))

    asyncio.run(run())

    # Must be refused due to excessive round range
    decisions = store.conn.execute("SELECT * FROM decisions WHERE slug=?", (r15.slug,)).fetchall()
    assert len(decisions) == 0


def test_retry_directional_validity_abort(store, monkeypatch):
    """When a directional order retry runs, if spot has crossed to the other side, abort."""
    bot = make_test_bot(store)
    now_ms = int(time.time() * 1000)
    r = make_round("btc-updown-5m-600", now_ms - 120_000, now_ms + 180_000, 100.0)

    # Spot is currently 102.0 (Above strike), but signal was Down
    feed = FakeSpotFeed(102.0)
    bot.spots = {"BTCUSDT": feed}

    sig = Signal(side="Down", quote=0.50, fair=0.50, edge=0.0, hurdle=0.0,
                 ev_per_dollar=0.0, act=True, reason="cross into Down")

    # Mock broker that fails on attempt 1
    class FailingBroker(PaperBroker):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def buy(self, *args, **kwargs):
            self.calls += 1
            self.last_failure = "fak_unfilled"
            return None

    bot.broker = FailingBroker()
    monkeypatch.setattr(settings, "order_retry_delay_s", 0.001)
    monkeypatch.setattr(settings, "order_max_attempts", 3)
    monkeypatch.setattr(settings, "cross_ladder_band_pad", 0.0)

    async def run():
        fill = await bot._place_order(r, sig, fair_up=0.5, token="tok_dn", stake=1.0,
                                      limit_ceiling=0.65, skip_pretrade_gates=True)
        assert fill is None
        # Only 1 call because attempt 2 detected spot 102.0 > strike 100.0 for Down, and aborted!
        assert bot.broker.calls == 1

    asyncio.run(run())

