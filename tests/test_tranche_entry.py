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


class TestATrancheRoundSettlesAsOneTrade:
    """Live 2026-09-24 05:05: one round, two legs, printed `trades=2` and two
    SETTLE lines whose pnl was each a leg (+0.5850, +0.6276), never the trade
    (+1.2126). The ledger keeps a row per fill; everything that COUNTS is per
    round."""

    @staticmethod
    def _bot(tmp_path, won_side="Up"):
        from btcpredict.calibration.report import PnLSummary
        from btcpredict.execution.paper import PaperBroker
        from btcpredict.risk.guards import RiskGuard
        from btcpredict.store.db import Store
        store = Store(tmp_path / "t.db")
        for i, (px, stake) in enumerate(((0.706, 1.51), (0.833, 2.93))):
            store.insert("fills", {
                "decision_id": 1, "slug": "r1", "ts_ms": 1000 + i,
                "mode": "paper", "side": "Up", "requested_px": px,
                "filled_px": px, "shares": stake / px, "stake": stake,
                "fee_paid": 0.01, "order_id": f"p{i}", "raw": ""})
        store.upsert_round({"slug": "r1", "start_ms": 0, "end_ms": 1,
                            "market_topic_id": 99, "winner": won_side})
        bot = Bot.__new__(Bot)
        bot.mode = "paper"; bot.store = store; bot.open_fills = {}
        bot.bankroll = 100.0; bot._bankroll_drift = 0
        bot.guard = RiskGuard(daily_loss_cap=100.0, max_consecutive_losses=5,
                              max_open_rounds=1)
        bot.broker = PaperBroker(); bot.pnl = PnLSummary()
        sent = []
        bot.telegram = SimpleNamespace(settled=lambda **kw: sent.append(kw))
        bot._restore_open_positions()
        return bot, store, sent

    def test_a_winning_round_is_one_trade(self, tmp_path):
        bot, store, sent = self._bot(tmp_path, "Up")
        asyncio.run(bot.settle_due())
        assert bot.pnl.n_trades == 1, "one round, one trade"
        assert bot.pnl.staked == pytest.approx(4.44)
        rows = store.query("SELECT * FROM settlements WHERE slug='r1'")
        assert len(rows) == 2, "the ledger still keeps a row per fill"
        want = sum(r["pnl"] for r in rows)     # the round = the sum of its legs
        assert want > 1.0
        assert bot.pnl.pnl == pytest.approx(want, abs=1e-9)
        assert len(sent) == 1 and sent[0]["pnl"] == pytest.approx(want, abs=1e-9)

    def test_a_losing_round_is_ONE_loss_in_the_streak(self, tmp_path):
        bot, store, sent = self._bot(tmp_path, "Down")
        asyncio.run(bot.settle_due())
        assert bot.pnl.n_trades == 1
        assert bot.guard.state.consecutive_losses == 1
        rows = store.query("SELECT * FROM settlements WHERE slug='r1'")
        assert bot.pnl.pnl == pytest.approx(sum(r["pnl"] for r in rows))
        assert len(sent) == 1


def test_legs_settling_on_different_passes_still_count_once(tmp_path):
    """Live 05:45: the two legs of one round settled 0.7s apart (separate
    passes) and printed two SETTLE lines. The partial round is carried until
    no leg of it is open, then counted once."""
    bot, store, sent = TestATrancheRoundSettlesAsOneTrade._bot(tmp_path, "Up")
    real = bot.broker.settle
    calls = {"n": 0}

    async def settle(fill, winner):
        calls["n"] += 1
        if calls["n"] == 2:
            return None                  # second leg unreadable on pass 1
        return await real(fill, winner)
    bot.broker.settle = settle
    asyncio.run(bot.settle_due())
    assert bot.pnl.n_trades == 0 and sent == [], "nothing counted mid-round"
    asyncio.run(bot.settle_due())
    assert bot.pnl.n_trades == 1 and len(sent) == 1
    rows = store.query("SELECT * FROM settlements WHERE slug='r1'")
    assert len(rows) == 2
    assert bot.pnl.pnl == pytest.approx(sum(r["pnl"] for r in rows))
