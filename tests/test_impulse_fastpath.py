"""The fast impulse path: the trigger must run on the feed's clock, not the poll.

Measured 2026-09-12 from the VPS: the venue answers in 5.3 ms (three round trips
= 16 ms) while the poll loop looked at the price once every 1002 ms. The poll
interval WAS the trigger-to-order latency -- a move visible on the websocket was
acted on at the next poll, ~500 ms later on average -- and the side being bought
keeps drifting for seconds after the impulse, so that delay is paid in the fill
price. `Bot._impulse_tick` evaluates the same statistic (a |window| bps move in
the underlying) the moment the window closes.

What these tests pin down: it fires, it fires once per round, it reaches the
order path exactly once, and -- the property that makes it safe to run on every
websocket update -- it does NOTHING (no broker call at all) when it does not
fire.
"""
from __future__ import annotations

import asyncio
import math
import pathlib
import tempfile
import time
from collections import deque
from types import SimpleNamespace

import pytest

from btcpredict.execution.paper import PaperBroker
from btcpredict.feeds.market_feed import Outcome, Round
from btcpredict.main import Bot
from btcpredict.risk.guards import RiskGuard
from btcpredict.store.db import Store

SPOT = 77000.0
BANKROLL = 80.0


class FakeFeed:
    """The three things the trigger reads, plus the wake-up it waits on."""

    def __init__(self, ticks: list[tuple[int, float]]):
        self.ticks = deque(ticks)
        self.price = ticks[-1][1] if ticks else None
        self.ts_ms = ticks[-1][0] if ticks else 0
        self.age_s = 0.0
        self.waits = 0

    def is_fresh(self, max_age_s: float) -> bool:
        return self.price is not None

    async def wait_tick(self, timeout: float) -> bool:
        self.waits += 1
        return False                    # nothing more arrived


def moving_ticks(bps: float, *, end_ms: int | None = None,
                 step_ms: int = 100) -> list[tuple[int, float]]:
    """A window that ends with a `bps` move: flat for 2 s, then the move.

    `impulse_bps` reads the newest point against the newest point at least
    `window_s` older, so the reference has to sit a full second back.
    """
    end = end_ms if end_ms is not None else int(time.time() * 1000)
    out = [(end - 2000 + i * step_ms, SPOT) for i in range(20)]
    out.append((end - 1000, SPOT))
    out.extend((end - 1000 + i * step_ms, SPOT * math.exp(bps / 1e4 * i / 10))
               for i in range(1, 11))
    return out


def make_round(*, tau_s: float = 120.0) -> Round:
    now_ms = int(time.time() * 1000)
    return Round(
        slug="btc-updown-5m-1789000000", market_topic_id=1, market_id=2,
        condition_id="c", symbol="BTCUSDT",
        start_ms=now_ms - 180_000, end_ms=now_ms + int(tau_s * 1000),
        strike=SPOT, end_price=None, price_feed_id=None,
        feed_provider=None, fee_rate_bps=200, liquidity=5000.0,
        trade_volume=1000.0, status="OPEN", trading_status="OPEN",
        live_status="OPEN",
        outcomes=[Outcome(name="Up", price=0.40, chance=0.40, token_id="1",
                          market_outcome_id=11, winner=None),
                  Outcome(name="Down", price=0.60, chance=0.60, token_id="2",
                          market_outcome_id=12, winner=None)],
    )


def make_bot(store, ticks, *, broker=None, ask=(0.40, 0.60)) -> Bot:
    bot = Bot.__new__(Bot)                      # skip network setup
    bot.mode = "paper"
    bot.store = store
    bot.book = None                             # -> skips the venue-position gate
    bot.broker = broker if broker is not None else PaperBroker()
    bot.open_fills = {}
    bot.bankroll = BANKROLL
    bot.symbols = ["BTCUSDT"]
    feed = FakeFeed(ticks)
    bot.spot = feed                             # what the trigger reads
    bot.spots = {"BTCUSDT": feed}               # what _idle's shutdown uses
    bot._round = make_round()
    bot._asks = ask
    bot._sigma = 0.00015
    bot._impulse_fired = set()
    bot._order_cooldown_until = 0.0             # _place_order writes this
    bot._stop = asyncio.Event()                 # _idle checks it every wait
    bot.guard = RiskGuard(daily_loss_cap=0.0, max_consecutive_losses=5,
                          max_open_rounds=1, daily_loss_cap_pct=0.20,
                          bankroll=lambda: BANKROLL)
    return bot


class Exploding:
    """A broker that fails the test if anything touches it."""

    mode = "paper"

    def __getattr__(self, name):
        raise AssertionError(f"the trigger touched the broker ({name}) "
                             f"without firing")


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = Store(pathlib.Path(d) / "t.db")
        yield s
        s.close()


@pytest.fixture
def impulse_mode(monkeypatch):
    monkeypatch.setattr("btcpredict.config.settings.impulse_enabled", True)
    monkeypatch.setattr("btcpredict.config.settings.entry_mode", "impulse")
    monkeypatch.setattr("btcpredict.config.settings.impulse_on_tick", True)
    monkeypatch.setattr("btcpredict.config.settings.impulse_window_s", 1.0)
    monkeypatch.setattr("btcpredict.config.settings.impulse_bps", 4.0)
    monkeypatch.setattr("btcpredict.config.settings.impulse_min_price", 0.05)
    monkeypatch.setattr("btcpredict.config.settings.impulse_max_price", 0.95)
    monkeypatch.setattr("btcpredict.config.settings.min_tau_s", 60.0)
    monkeypatch.setattr("btcpredict.config.settings.max_tau_s", 210.0)
    monkeypatch.setattr("btcpredict.config.settings.order_type", "market")
    monkeypatch.setattr("btcpredict.config.settings.order_max_attempts", 1)
    monkeypatch.setattr("btcpredict.config.settings.trade_on_candle_open", False)
    monkeypatch.setattr("btcpredict.config.settings.max_spot_age_s", 2.0)
    monkeypatch.setattr("btcpredict.config.settings.sizing_mode", "fraction")
    monkeypatch.setattr("btcpredict.config.settings.stake_fraction", 0.04)
    monkeypatch.setattr("btcpredict.config.settings.stake_tilt", "none")
    monkeypatch.setattr("btcpredict.config.settings.min_order_usdt", 1.0)
    monkeypatch.setattr("btcpredict.config.settings.stake_cap_usdt", 25.0)


def test_a_qualifying_move_buys_without_waiting_for_the_poll(store, impulse_mode):
    """The whole point: one call, no poll, an order out."""
    bot = make_bot(store, moving_ticks(5.0))
    asyncio.run(bot._impulse_tick())

    rows = bot.store.query("SELECT * FROM decisions")
    assert len(rows) == 1, "the trigger must record its decision"
    assert rows[0]["side"] == "Up", "a +5 bps move buys Up"
    assert "impulse" in rows[0]["reason"]
    assert bot.store.counts()["fills"] == 1, "and must reach the order path"


def test_a_negative_move_buys_down(store, impulse_mode):
    bot = make_bot(store, moving_ticks(-5.0))
    asyncio.run(bot._impulse_tick())
    rows = bot.store.query("SELECT * FROM decisions")
    assert len(rows) == 1 and rows[0]["side"] == "Down"


def test_it_does_nothing_at_all_when_there_is_no_impulse(store, impulse_mode):
    """This runs ~33 times a second, between polls: a quiet window must cost
    nothing. An Exploding broker proves it -- not a log assertion."""
    bot = make_bot(store, moving_ticks(1.0), broker=Exploding())
    asyncio.run(bot._impulse_tick())
    assert bot.store.counts()["decisions"] == 0
    assert bot.store.counts()["fills"] == 0


def test_one_impulse_buys_the_round_once(store, impulse_mode):
    """The window stays satisfied for as long as the move stays inside it, so
    the same impulse is re-evaluated dozens of times. It must buy once."""
    bot = make_bot(store, moving_ticks(5.0))
    for _ in range(30):
        asyncio.run(bot._impulse_tick())
    assert bot.store.counts()["fills"] == 1
    assert bot.store.counts()["decisions"] == 1


def test_a_side_outside_the_price_band_is_left_alone(store, impulse_mode):
    """0.97 is the model's tail, not an impulse entry."""
    bot = make_bot(store, moving_ticks(5.0), broker=Exploding(), ask=(0.97, 0.03))
    asyncio.run(bot._impulse_tick())
    assert bot.store.counts()["decisions"] == 0


def test_tau_outside_the_window_is_left_alone(store, impulse_mode):
    bot = make_bot(store, moving_ticks(5.0), broker=Exploding())
    bot._round = make_round(tau_s=30.0)         # inside the last minute
    asyncio.run(bot._impulse_tick())
    assert bot.store.counts()["decisions"] == 0


def test_the_impulse_may_take_a_round_the_model_already_holds(store,
                                                             impulse_mode):
    """CHANGED 2026-09-13: this asserted the opposite.

    Arms are independent -- one arm's position must never refuse another's.
    The model arm enters at tau 60-210 and holds to the bell, which spans this
    arm's entire window, so a shared slot meant whichever fired first silenced
    the other for the round. Measured on the cross arm, the same collision cost
    52% of its triggers at an EV indistinguishable from the ones that got
    through; there is no reason to think the impulse arm differs.

    Total exposure is bounded by the DAILY CAP, not by the slot count, and each
    arm still cannot double its own position: check_pool dedups per round
    inside the pool.
    """
    bot = make_bot(store, moving_ticks(10.0))    # a real broker: it should fire
    bot.guard.on_open(bot._round.slug)           # the MODEL got here first
    asyncio.run(bot._impulse_tick())
    assert bot.store.counts()["decisions"] == 1, (
        "the impulse has its own budget and must not inherit the model's")


def test_the_impulse_refuses_a_round_IT_already_holds(store, impulse_mode):
    """Independence is not licence to double: the dedup is per pool."""
    bot = make_bot(store, moving_ticks(10.0), broker=Exploding())
    bot.guard.on_open(bot._round.slug, pool="impulse")
    asyncio.run(bot._impulse_tick())
    assert bot.store.counts()["decisions"] == 0


def test_the_impulse_refuses_once_ITS_OWN_budget_is_full(store, impulse_mode):
    """Even on a DIFFERENT round: impulse_max_open caps THIS arm's exposure."""
    bot = make_bot(store, moving_ticks(10.0), broker=Exploding())
    bot.guard.on_open("btc-updown-5m-some-other-round", pool="impulse")
    asyncio.run(bot._impulse_tick())
    assert bot.store.counts()["decisions"] == 0


def test_the_MAIN_arms_budget_being_full_does_not_stop_it(store, impulse_mode):
    """The other half of independence: a full main pool is not this arm's
    problem. max_open_rounds bounds the model arm alone."""
    bot = make_bot(store, moving_ticks(10.0))
    bot.guard.on_open("btc-updown-5m-some-other-round")   # main pool full
    asyncio.run(bot._impulse_tick())
    assert bot.store.counts()["decisions"] == 1


def test_record_mode_cannot_trade_because_its_round_is_never_cached(store,
                                                                    impulse_mode):
    """`_record` caches `_round` only when it is allowed to trade, so record mode
    has nothing for the fast path to act on. This is the guard, not a setting."""
    bot = make_bot(store, moving_ticks(5.0), broker=Exploding())
    bot._round = None                           # what record mode always leaves
    asyncio.run(bot._impulse_tick())
    assert bot.store.counts()["decisions"] == 0


def test_the_loop_waits_on_the_feed_and_keeps_evaluating_the_trigger(store,
                                                                     impulse_mode,
                                                                     monkeypatch):
    """_idle must wake on the price feed, not on the clock: with a feed that
    never reports a tick it evaluates once and returns, and the budget is not
    burned sleeping."""
    bot = make_bot(store, moving_ticks(1.0))
    calls = []

    async def spy():
        calls.append(time.time())

    monkeypatch.setattr(bot, "_impulse_tick", spy)
    t0 = time.time()
    asyncio.run(bot._idle(0.30))
    assert calls, "the trigger must be evaluated at least once per wait"
    assert time.time() - t0 < 0.30, "a feed that reports no tick must not stall"
    assert bot.spot.waits == 1, "and it must actually wait on the feed"


def test_both_mode_still_runs_the_trigger_on_the_feed_clock(store, monkeypatch):
    """entry_mode="both" must keep the impulse on the websocket clock.

    The union arm only beats the model arm because the impulse gets to the
    round FIRST -- it is evaluated on every price update while the model waits
    for the next poll. `_idle` decides that, and it used to test
    `entry_mode == "impulse"` exactly, so adding "both" without touching it
    would have left the impulse arm dead in the water: no trigger, no entry,
    and the union would silently degrade to plain model mode.
    """
    monkeypatch.setattr("btcpredict.config.settings.entry_mode", "both")
    monkeypatch.setattr("btcpredict.config.settings.impulse_on_tick", True)
    bot = make_bot(store, moving_ticks(1.0))
    calls = []

    async def spy():
        calls.append(1)

    monkeypatch.setattr(bot, "_impulse_tick", spy)
    asyncio.run(bot._idle(0.30))
    assert calls, "both mode must evaluate the impulse trigger on the feed clock"
    assert bot.spot.waits == 1, "and it must wait on the feed, not the clock"


def test_model_mode_does_not_run_the_trigger_on_the_feed_clock(store, monkeypatch):
    """The other side of the same switch: plain model mode must NOT take the
    tick path, or every model run would start evaluating impulse entries."""
    monkeypatch.setattr("btcpredict.config.settings.entry_mode", "model")
    monkeypatch.setattr("btcpredict.config.settings.impulse_on_tick", True)
    bot = make_bot(store, moving_ticks(1.0))

    async def boom():
        raise AssertionError("model mode must not evaluate the impulse trigger")

    monkeypatch.setattr(bot, "_impulse_tick", boom)
    asyncio.run(bot._idle(0.05))
    assert bot.spot.waits == 0, "model mode waits on the clock, not the feed"


def test_a_tick_that_arrives_wakes_the_loop_and_fires(store, impulse_mode):
    """End to end on the loop's clock: the trigger runs when the price moves,
    without the poll interval elapsing."""
    bot = make_bot(store, moving_ticks(1.0))    # quiet to begin with
    bot._round = None                           # ... and armed only after a tick
    seen = []

    async def arm_then_report(timeout: float) -> bool:
        if seen:                                # one update, then the budget ends
            return False
        seen.append(1)
        bot._round = make_round()
        bot.spot.ticks = deque(moving_ticks(6.0))
        bot.spot.price = bot.spot.ticks[-1][1]
        return True

    bot.spot.wait_tick = arm_then_report
    asyncio.run(bot._idle(1.0))
    assert bot.store.counts()["fills"] == 1, "the wake-up must reach the order"


def test_the_arm_is_inert_when_the_flag_is_off(store, impulse_mode, monkeypatch):
    """REGRESSION 2026-09-13. This arm ran with NO enable flag at all.

    It is called unconditionally from the poll and the idle loop; the only
    thing keeping it silent was `guard.check()` refusing it because the MAIN
    arm held the single slot in max_open_rounds. Giving every arm its own pool
    removed that accident and it placed its first ever live order within hours
    -- an arm that measures -0.0479/$1 at a realistic fill.
    """
    monkeypatch.setattr("btcpredict.config.settings.impulse_enabled", False)
    bot = make_bot(store, moving_ticks(10.0), broker=Exploding())
    asyncio.run(bot._impulse_tick())
    assert bot.store.counts()["decisions"] == 0


def test_the_flag_defaults_to_off(monkeypatch):
    """It is negative at a realistic fill and has no live record: one order in
    the bot's entire history, placed by accident. Default OFF until measured."""
    import importlib
    from btcpredict import config
    assert config.Settings().impulse_enabled is False
