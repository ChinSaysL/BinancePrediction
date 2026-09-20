"""The shadow arm has to actually run -- and must never be able to order.

Two properties that only an integration test can hold:

  * `_shot15` sits ABOVE the `self.mode == "record"` early return in `_record`,
    because observing is the entire point of the arm. A unit test of
    `edge/shot15.py` cannot see that placement, and moving the call below that
    line would silence the arm in exactly the mode it was built for.
  * it writes decision rows and NEVER reaches `_place_order`. This arm is below
    its own evidence bar (day-clustered t +1.64 against t>=2), so "cannot cost
    money" is a property worth pinning rather than assuming.
"""
from __future__ import annotations

import asyncio
import pathlib
import tempfile
import time
from types import SimpleNamespace

import pytest

from btcpredict.feeds.market_feed import Outcome, Round
from btcpredict.main import Bot
from btcpredict.models import PriceHistory
from btcpredict.pricing.vol import VolEstimator
from btcpredict.risk.guards import RiskGuard
from btcpredict.store.db import Store

SPOT = 80800.0
STRIKE = 80700.0          # spot is ABOVE it -> the ITM side is Up


class FakeSpot:
    price = SPOT
    ts_ms = 0
    age_s = 0.0

    def is_fresh(self, max_age_s: float) -> bool:
        return True


def make_round(*, tau_s: float = 450.0, slug: str = "btc-updown-15m-1789000000",
               up: float = 0.65, down: float = 0.38) -> Round:
    """A 15m round `tau_s` from expiry, priced so the Up ask sits below P*.

    The default book is 0.65/0.38 -- it sums to 1.03, i.e. COHERENT. A pair
    summing under $1.00 is corrupt data and the arm refuses it, so a test that
    wants a fire must not use one.
    """
    now_ms = int(time.time() * 1000)
    return Round(
        slug=slug, market_topic_id=1, market_id=2, condition_id="c",
        symbol="BTCUSDT", start_ms=now_ms - int((900 - tau_s) * 1000),
        end_ms=now_ms + int(tau_s * 1000),
        strike=STRIKE, end_price=None, price_feed_id=None,
        feed_provider="CHAINLINK", fee_rate_bps=200, liquidity=5000.0,
        trade_volume=1000.0, status="OPEN", trading_status="OPEN",
        live_status="OPEN",
        outcomes=[Outcome(name="Up", price=up, chance=up, token_id="1",
                          market_outcome_id=11, winner=None),
                  Outcome(name="Down", price=down, chance=down, token_id="2",
                          market_outcome_id=12, winner=None)],
    )


def make_bot(store, mode: str = "record") -> Bot:
    bot = Bot.__new__(Bot)
    bot.mode = mode
    bot.store = store
    bot.book = None
    bot.broker = SimpleNamespace(mode="paper")
    bot.open_fills = {}
    bot.bankroll = 80.0
    bot.symbols = ["BTCUSDT"]
    bot.spots = {"BTCUSDT": FakeSpot()}
    bot.histories = {"BTCUSDT": PriceHistory()}
    for i in range(5):
        bot.histories["BTCUSDT"].add_tick(SPOT + i)
    vol = VolEstimator(lam=0.97, floor=0.00015, lookback=240)
    vol.add(0.0)
    bot.vols = {"BTCUSDT": vol}
    bot.model = SimpleNamespace(
        name="stub",
        predict=lambda ctx: SimpleNamespace(emitted=False, p_up=0.5,
                                            reason="stub"))
    bot.up_model = None
    bot.guard = RiskGuard(daily_loss_cap=0.0, max_consecutive_losses=5,
                          max_open_rounds=1, daily_loss_cap_pct=0.20,
                          bankroll=lambda: bot.bankroll)
    bot._candle = {}
    bot._streak = {}
    bot._order_cooldown_until = 0.0
    # A WARM 1Hz spot buffer. The cell computes its own sigma from this (900s
    # of 1-second returns); without it the arm refuses with "sigma buffer thin"
    # and nothing downstream is exercised. Prices wobble so the variance is
    # non-zero -- a flat buffer gives sigma 0 and refuses too.
    now_ms = int(time.time() * 1000)
    bot._ticks = {"BTCUSDT": [
        (now_ms - (900 - i) * 1000, SPOT + (i % 7) - 3.0) for i in range(900)
    ]}

    def _boom(*a, **k):
        raise AssertionError("the shadow arm must never place an order")

    bot._place_order = _boom
    return bot


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = Store(pathlib.Path(d) / "t.db")
        yield s
        s.close()


def shot_rows(store):
    return [r for r in store.query("SELECT * FROM decisions")
            if (r["reason"] or "").startswith("shot15")]


def run(bot, rnd, trade=False):
    asyncio.run(bot._record(rnd, "BTCUSDT", trade=trade, primary=True))


# --- it runs where it is supposed to ---------------------------------------

def test_runs_in_record_mode(store):
    """⛔ The call sits above the `mode == 'record'` return on purpose. If it is
    ever moved below, this is the test that says so."""
    bot = make_bot(store, mode="record")
    run(bot, make_round())
    rows = shot_rows(store)
    assert len(rows) == 1
    assert rows[0]["side"] == "Up"
    assert rows[0]["acted"] == 0


def test_runs_when_the_family_may_not_trade(store):
    """`trade=False` narrows which families may RISK MONEY. This one cannot,
    so it must still be observed."""
    bot = make_bot(store, mode="live")
    run(bot, make_round(), trade=False)
    assert len(shot_rows(store)) == 1


def test_never_orders_even_on_a_firing_signal(store):
    """_place_order raises if called; a firing shot must still not reach it."""
    bot = make_bot(store, mode="live")
    run(bot, make_round(), trade=False)
    row = shot_rows(store)[0]
    assert row["acted"] == 0
    assert row["stake"] == 0.0
    assert "shadow" in row["reason"]


# --- what gets written ------------------------------------------------------

def test_a_fire_records_fair_quote_and_edge(store):
    bot = make_bot(store, mode="record")
    run(bot, make_round())
    row = shot_rows(store)[0]
    assert row["quote"] == pytest.approx(0.65)
    assert row["fair"] > row["quote"]
    assert row["edge"] == pytest.approx(row["fair"] - row["quote"])
    assert row["edge"] >= 0.10
    assert row["hurdle"] is None, "this cell has no fee hurdle; see _shot15"
    assert row["strike"] == pytest.approx(STRIKE)


def test_refusals_inside_the_window_are_recorded(store):
    """The refusals are the funnel, and the funnel is what localised the
    Impulse Lag failure to one gate. An EV column alone could not have."""
    bot = make_bot(store, mode="record")
    # Coherent (1.04) but priced too near fair to clear min_edge. Asserted on
    # the "<" that only a REFUSAL carries: a firing reason also contains the
    # word "edge", so a substring match on it passes for the wrong reason.
    run(bot, make_round(up=0.95, down=0.09))
    rows = shot_rows(store)
    assert len(rows) == 1
    assert rows[0]["acted"] == 0
    assert "<" in rows[0]["reason"], rows[0]["reason"]


def test_tau_outside_the_window_is_not_logged(store):
    """A 15m round spends 600 of its 900 seconds outside the window and the bot
    polls every second, so logging those would add ~600 empty rows per round."""
    bot = make_bot(store, mode="record")
    run(bot, make_round(tau_s=800.0))
    assert shot_rows(store) == []


def test_other_families_are_ignored(store):
    bot = make_bot(store, mode="record")
    run(bot, make_round(slug="btc-updown-5m-1789000000", tau_s=280.0))
    assert shot_rows(store) == []


def test_mode_off_writes_nothing(store, monkeypatch):
    monkeypatch.setattr("btcpredict.config.settings.shot15_mode", "off")
    bot = make_bot(store, mode="record")
    run(bot, make_round())
    assert shot_rows(store) == []


# --- it must not be able to break the live loop -----------------------------

def test_an_exception_in_the_arm_does_not_break_recording(store, monkeypatch):
    """A shadow arm that has never risked a cent must not take down the live
    loop that has."""
    def boom(*a, **k):
        raise RuntimeError("synthetic")

    monkeypatch.setattr("btcpredict.main.edge_shot15.evaluate", boom)
    bot = make_bot(store, mode="record")
    run(bot, make_round())                       # must not raise
    assert shot_rows(store) == []
    assert store.counts()["quotes"] == 1, "the tape is still written"


# --- one entry per round ----------------------------------------------------

def test_a_round_fires_at_most_once(store):
    """⛔ The cell was MEASURED taking the first fire in a round and stopping,
    and a live arm gets one position per round anyway. Firing every poll would
    write ~300 rows a round and make any later trade count a count of POLLS --
    the tick-vs-round error that once read a 5m arm as +6.8%."""
    bot = make_bot(store, mode="record")
    rnd = make_round()
    for _ in range(5):
        run(bot, rnd)
    assert len(shot_rows(store)) == 1


def test_a_second_round_still_fires(store):
    """Deduplication is per slug, not a global latch."""
    bot = make_bot(store, mode="record")
    run(bot, make_round(slug="btc-updown-15m-1789000000"))
    run(bot, make_round(slug="btc-updown-15m-1789000900"))
    assert len(shot_rows(store)) == 2


def test_refusals_do_not_arm_the_latch(store):
    """A round that refused must still be able to fire later in its life."""
    bot = make_bot(store, mode="record")
    slug = "btc-updown-15m-1789000000"
    run(bot, make_round(slug=slug, up=0.95, down=0.09))   # refuses: edge small
    run(bot, make_round(slug=slug))                        # now it qualifies
    rows = shot_rows(store)
    assert len(rows) == 2
    assert "<" in rows[0]["reason"], "first was a refusal"
    assert rows[-1]["acted"] == 0 and "<" not in rows[-1]["reason"]
