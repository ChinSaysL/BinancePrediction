"""TRANCHE ENTRY: buy a third now, the rest later only if spot is still ahead.

Every SELL-based exit measured loses (the book keeps underpricing our token
after entry), so losers are made cheaper by never being fully BOUGHT. These
pin the parts that are easy to get wrong with real money:
  * off means the previous behaviour exactly (one order, the whole stake);
  * each leg clears the venue minimum, or the stake goes out whole;
  * the add-on is decided ONCE -- never twice, whatever the venue does;
  * it buys only when spot is on our side of the strike, the guard allows an
    add, the ask is under the cap, and the first leg is still held.
"""
from __future__ import annotations

import asyncio
import math
import time
from types import SimpleNamespace

import pytest

from btcpredict.config import settings
from btcpredict.main import Bot

STRIKE = 80000.0
SLUG = "btc-updown-5m-1790000000"


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "model_tranche_enabled", True)
    monkeypatch.setattr(settings, "model_tranche_first_frac", 0.34)
    monkeypatch.setattr(settings, "model_tranche_delay_s", 15.0)
    monkeypatch.setattr(settings, "model_tranche_max_px", 0.92)
    monkeypatch.setattr(settings, "min_order_usdt", 1.0)


class TestTheSplit:
    def test_off_is_the_old_behaviour(self, monkeypatch):
        monkeypatch.setattr(settings, "model_tranche_enabled", False)
        assert Bot.__new__(Bot)._tranche_split(3.7) == (3.7, 0.0)

    def test_a_third_now_the_rest_later(self, on):
        first, rest = Bot.__new__(Bot)._tranche_split(3.7)
        assert first == pytest.approx(3.7 * 0.34)
        assert rest == pytest.approx(3.7 - 3.7 * 0.34)

    def test_the_first_leg_is_raised_to_the_venue_minimum(self, on):
        first, rest = Bot.__new__(Bot)._tranche_split(2.4)
        assert first == 1.0 and rest == pytest.approx(1.4)

    def test_a_stake_too_small_for_two_legs_goes_out_whole(self, on):
        assert Bot.__new__(Bot)._tranche_split(1.8) == (1.8, 0.0)


def _bot(*, spot, ask=0.80, guard_ok=True, due_in=-1.0, tau_s=120.0,
         held=True, watch=None):
    bot = Bot.__new__(Bot)
    now_ms = int(time.time() * 1000)
    bot._ticks = {"BTCUSDT": [(now_ms - 1000, spot), (now_ms, spot)]}
    bot.open_fills = {1: (SLUG, SimpleNamespace(side="Up"))} if held else {}
    bot._exit_watch = {SLUG: watch} if watch else {}
    bot._tranche_pending = {SLUG: {
        "side": "Up", "token": "tok", "rest": 2.44, "did": 9,
        "due": time.time() + due_in, "sigma": 0.0004, "fair": 0.72,
        "quote": 0.68, "edge": 0.04}}
    bot.guard = SimpleNamespace(
        check_add=lambda slug: (guard_ok, "ok" if guard_ok else "halted"))

    async def best_prices(token, market_id):
        return (ask - 0.01, ask)
    bot.book = SimpleNamespace(best_prices=best_prices)
    bot.bought = []
    bot.booked = []

    async def buy(**kw):
        bot.bought.append(kw)
        return SimpleNamespace(stake=kw["stake"], filled_px=kw["price"],
                               shares=kw["stake"] / kw["price"])
    bot.broker = SimpleNamespace(buy=buy, last_failure=None)

    async def book_fill(r, sig, fill, **kw):
        bot.booked.append((sig.reason, kw))
    bot._book_fill = book_fill
    r = SimpleNamespace(slug=SLUG, symbol="BTCUSDT", strike=STRIKE,
                        end_ms=now_ms + int(tau_s * 1000), market_id=1,
                        start_ms=now_ms - 180_000)
    return bot, r


AHEAD = STRIKE * (1 + 2e-4)       # +2bps: Up is winning
BEHIND = STRIKE * (1 - 2e-4)


class TestTheAddOn:
    def test_not_due_yet_does_nothing(self, on):
        bot, r = _bot(spot=AHEAD, due_in=+10.0)
        asyncio.run(bot._tranche_tick(r))
        assert bot.bought == [] and SLUG in bot._tranche_pending

    def test_ahead_buys_the_rest_with_the_cap(self, on):
        bot, r = _bot(spot=AHEAD)
        asyncio.run(bot._tranche_tick(r))
        assert len(bot.bought) == 1
        b = bot.bought[0]
        assert b["stake"] == pytest.approx(2.44) and b["max_px"] == 0.92
        assert b["side"] == "Up" and b["token_id"] == "tok"
        assert bot.booked and bot.booked[0][0] == "tranche add-on"
        # booked WITHOUT an exit watch (arm is not "model")
        assert bot.booked[0][1].get("arm") is None

    def test_behind_keeps_only_the_first_leg(self, on):
        bot, r = _bot(spot=BEHIND)
        asyncio.run(bot._tranche_tick(r))
        assert bot.bought == []

    def test_decided_once_never_twice(self, on):
        bot, r = _bot(spot=AHEAD)
        asyncio.run(bot._tranche_tick(r))
        asyncio.run(bot._tranche_tick(r))
        assert len(bot.bought) == 1 and SLUG not in bot._tranche_pending

    def test_a_behind_decision_is_final_too(self, on):
        bot, r = _bot(spot=BEHIND)
        asyncio.run(bot._tranche_tick(r))
        bot._ticks["BTCUSDT"].append((int(time.time() * 1000), AHEAD))
        asyncio.run(bot._tranche_tick(r))
        assert bot.bought == [], "a later recovery must not add after a no"

    def test_the_guard_can_refuse(self, on):
        bot, r = _bot(spot=AHEAD, guard_ok=False)
        asyncio.run(bot._tranche_tick(r))
        assert bot.bought == []

    def test_an_ask_above_the_cap_is_refused(self, on):
        bot, r = _bot(spot=AHEAD, ask=0.95)
        asyncio.run(bot._tranche_tick(r))
        assert bot.bought == []

    def test_not_at_the_bell(self, on):
        bot, r = _bot(spot=AHEAD, tau_s=4.0)
        asyncio.run(bot._tranche_tick(r))
        assert bot.bought == []

    def test_not_when_the_first_leg_is_gone(self, on):
        bot, r = _bot(spot=AHEAD, held=False)
        asyncio.run(bot._tranche_tick(r))
        assert bot.bought == []

    def test_not_while_the_first_leg_is_being_sold(self, on):
        bot, r = _bot(spot=AHEAD, watch={"selling": True})
        asyncio.run(bot._tranche_tick(r))
        assert bot.bought == []

    def test_down_side_reads_the_lead_the_other_way(self, on):
        bot, r = _bot(spot=BEHIND)          # spot below strike: Down is ahead
        bot._tranche_pending[SLUG]["side"] = "Down"
        asyncio.run(bot._tranche_tick(r))
        assert len(bot.bought) == 1 and bot.bought[0]["side"] == "Down"


def test_the_entry_hook_runs_every_tick():
    """The add-on is decided in the per-market loop, next to the exit check."""
    import pathlib
    src = pathlib.Path("btcpredict/main.py").read_text()
    i = src.index("await self._model_exit_check(r)\n")
    assert "await self._tranche_tick(r)" in src[i:i + 200]
