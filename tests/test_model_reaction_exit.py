"""The MODEL arm's exit: sell when the price REACTS against us.

MEASURED 2026-09-22 on the Binance era only (301 rounds / 2.06 days, 44,056
ticks) -- see config.model_exit_on_reaction for the table. Where the spot has
just reacted against our side the venue's BID still sits ABOVE the position's
true worth (0.7652 against a realised win rate of 0.7011 at the 5bps/20s
trigger), so selling is the profit; everywhere else the bid sits below it.

These pin the parts that are easy to get wrong:
  * the trigger is a STATE, checked every tick -- not an event. A reaction that
    persists must be visible on every tick of it, because a one-shot arming
    would wait for a second occurrence that never comes (the exact trap
    `_cross_exit_check` documents for the flip rule);
  * it only fires when we are AHEAD. A position already behind has nothing to
    protect and is not what the measurement covers;
  * "no history" is (False, 0.0), never a refusal;
  * the two arms' exit flags do not enable each other.
"""
from __future__ import annotations

import asyncio
import math
import pathlib
import tempfile
import time
from types import SimpleNamespace

import pytest

from btcpredict.config import settings
from btcpredict.feeds.market_feed import Outcome, Round
from btcpredict.main import Bot
from btcpredict.store.db import Store

STRIKE = 77000.0


def make_round(strike=STRIKE):
    now = int(time.time() * 1000)
    return Round(
        slug="btc-updown-5m-1789000000", market_topic_id=1, market_id=2,
        condition_id="c", symbol="BTCUSDT", start_ms=now - 180_000,
        end_ms=now + 180_000, strike=strike, end_price=None,
        price_feed_id=None, feed_provider=None, fee_rate_bps=200,
        liquidity=5000.0, trade_volume=1000.0, status="OPEN",
        trading_status="OPEN", live_status="OPEN",
        outcomes=[Outcome(name="Up", price=0.30, chance=0.30, token_id="1",
                          market_outcome_id=11, winner=None),
                  Outcome(name="Down", price=0.70, chance=0.70, token_id="2",
                          market_outcome_id=12, winner=None)])


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = Store(pathlib.Path(d) / "t.db")
        yield s
        s.close()


def make_bot(store, r=None):
    bot = Bot.__new__(Bot)
    bot.mode = "paper"
    bot.store = store
    bot.book = None
    bot._ticks = {}
    bot.open_fills = {}
    bot._exit_watch = {}
    bot._round = r or make_round()
    return bot


def series(bot, r, ticks):
    """`ticks` = [(seconds_ago, price), ...]; all inside the round.

    ⛔ THE AGES MATTER. `_reaction_against` compares the spot now against the
    spot at or before now - look_s, so a fixture whose whole history is only
    19 s long has NOTHING on the far side of the 20 s cut and reports
    (False, 0.0) -- which silently looks like "the rule does not fire".
    """
    now = int(time.time() * 1000)
    bot._ticks["BTCUSDT"] = [(now - int(a * 1000), p) for a, p in ticks]


def fade(bot, r, ago_price, now_price, *, far_s=30, far_n=3, near_s=10,
         near_n=3):
    """A path that sat at `ago_price` before the lookback and is at
    `now_price` after it: the shape the trigger is defined on."""
    ts = [(far_s - i * 2, ago_price) for i in range(far_n)]
    ts += [(near_s - i * 3, now_price) for i in range(near_n)]
    series(bot, r, ts)


class TestTheReactionTrigger:
    def test_a_reaction_against_us_fires(self, store):
        """Ahead of the strike, but the last 20 s have gone the wrong way."""
        r = make_round()
        bot = make_bot(store, r)
        # was 40 bps above the strike, now 20 bps above -> ~-20 bps
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)
        hit, mv = bot._reaction_against(r, "Up")
        assert hit is True and mv < -settings.model_exit_rev_bps, (hit, mv)

    def test_the_same_move_is_a_HOLD_when_it_is_in_our_favour(self, store):
        r = make_round()
        bot = make_bot(store, r)
        fade(bot, r, STRIKE * 1.0020, STRIKE * 1.0040)
        hit, mv = bot._reaction_against(r, "Up")
        assert hit is False and mv > 0, (hit, mv)

    def test_nothing_to_protect_when_we_are_BEHIND(self, store):
        """The rule protects a LEAD; a position already losing is not its job."""
        r = make_round()
        bot = make_bot(store, r)
        fade(bot, r, STRIKE * 0.9980, STRIKE * 0.9960)
        assert bot._reaction_against(r, "Up") == (False, 0.0)

    def test_the_side_matters(self, store):
        """Down is ahead here, so DOWN can react and Up cannot."""
        r = make_round()
        bot = make_bot(store, r)
        fade(bot, r, STRIKE * 0.9960, STRIKE * 0.9980)
        assert bot._reaction_against(r, "Down")[0] is True
        assert bot._reaction_against(r, "Up") == (False, 0.0)

    def test_a_small_wobble_is_not_a_reaction(self, store):
        """Below the threshold the venue has already repriced -- the whole
        point of the >=5bps measurement."""
        """Below the threshold the venue has already repriced -- the whole
        point of the >=5bps measurement."""
        r = make_round()
        bot = make_bot(store, r)
        bump = settings.model_exit_rev_bps / 2.0 / 1e4
        fade(bot, r, STRIKE * (1.0040 + bump), STRIKE * 1.0040)
        assert bot._reaction_against(r, "Up")[0] is False

    def test_it_is_a_STATE_not_an_event(self, store):
        """Every tick of a persisting reaction must report it.

        This is the trap the cross arm documents: an EVENT-armed rule needs a
        second occurrence, and a sustained move produces exactly one.
        """
        r = make_round()
        bot = make_bot(store, r)
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)
        for _ in range(3):
            assert bot._reaction_against(r, "Up")[0] is True

    def test_no_history_is_no_opinion_not_a_refusal(self, store):
        r = make_round()
        bot = make_bot(store, r)
        bot._ticks = {}
        assert bot._reaction_against(r, "Up") == (False, 0.0)
        series(bot, r, [(1, STRIKE * 1.0020), (0, STRIKE * 1.0040)])
        assert bot._reaction_against(r, "Up") == (False, 0.0)

    def test_ticks_before_the_round_are_ignored(self, store):
        """The REFERENCE price must come from this round.

        `now` was bounded to the round but `past` was not, so the first measured
        move of every round could be a phantom reaction against the PREVIOUS
        round's spot.
        """
        r = make_round()
        bot = make_bot(store, r)
        now = int(time.time() * 1000)
        bot._ticks["BTCUSDT"] = [
            (r.start_ms - 60_000, STRIKE * 1.0100),      # previous round
            (now - 30_000, STRIKE * 1.0040),
            (now - 10_000, STRIKE * 1.0020),
            (now, STRIKE * 1.0020)]
        hit, mv = bot._reaction_against(r, "Up")
        # against the in-round 30 s reference this is -20 bps; against the
        # previous round's 1.0100 it would read -79 bps.
        assert hit is True and -25.0 < mv < -15.0, mv

    @pytest.mark.parametrize("strike", [None, 0.0, -1.0])
    def test_an_impossible_strike_is_no_opinion(self, store, strike):
        r = make_round(strike=strike)
        bot = make_bot(store, r)
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)
        assert bot._reaction_against(r, "Up") == (False, 0.0)


class TestTheExitCheck:
    @staticmethod
    def _armed(bot, r, side="Up", **over):
        now = int(time.time() * 1000)
        bot._exit_watch[r.slug] = {
            "fid": 7, "side": side, "token": "t", "shares": 3.0,
            "stake": 2.0, "fee": 0.01, "entry_ms": now - 30_000,
            "rule": over.pop("rule", "react"), "done": 0, "flip_ms": None,
        }
        bot.open_fills[7] = (r.slug, SimpleNamespace())
        return bot

    def _run(self, bot, r):
        calls = []

        async def fake_exec(r_, w, *, tau, sig_name, confirmed=None,
                            shares=None):
            calls.append((r_.slug, w["side"], round(tau, 1), sig_name))

        bot._cross_exit_execute = fake_exec
        fired = asyncio.run(bot._model_exit_check(r))
        return fired, calls

    def test_it_sells_on_a_reaction(self, store):
        r = make_round()
        bot = self._armed(make_bot(store, r), r)
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)
        fired, calls = self._run(bot, r)
        assert fired is True
        assert calls and calls[0][3] == "react" and calls[0][1] == "Up"

    def test_it_holds_when_the_price_is_with_us(self, store):
        r = make_round()
        bot = self._armed(make_bot(store, r), r)
        fade(bot, r, STRIKE * 1.0020, STRIKE * 1.0040)
        assert self._run(bot, r) == (False, [])

    def test_the_flag_switches_it_off(self, store, monkeypatch):
        monkeypatch.setattr(settings, "model_exit_on_reaction", False)
        r = make_round()
        bot = self._armed(make_bot(store, r), r)
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)
        assert self._run(bot, r) == (False, [])

    def test_a_cross_position_is_not_touched(self, store):
        """The two arms share the sale path but never each other's trigger."""
        r = make_round()
        bot = self._armed(make_bot(store, r), r, rule="flip")
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)
        assert self._run(bot, r) == (False, [])

    def test_no_watch_is_a_no_op(self, store):
        r = make_round()
        bot = make_bot(store, r)
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)
        assert self._run(bot, r) == (False, [])

    def test_a_settled_position_drops_its_watch(self, store):
        r = make_round()
        bot = self._armed(make_bot(store, r), r)
        bot.open_fills.clear()
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)
        assert self._run(bot, r) == (False, [])
        assert r.slug not in bot._exit_watch

    def test_the_minimum_hold_delays_a_fresh_fill(self, store):
        r = make_round()
        bot = self._armed(make_bot(store, r), r)
        bot._exit_watch[r.slug]["entry_ms"] = int(time.time() * 1000)
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)
        assert self._run(bot, r) == (False, [])

    def test_no_exit_into_the_bell(self, store):
        r = make_round()
        r.end_ms = int(time.time() * 1000) + 10_000
        bot = self._armed(make_bot(store, r), r)
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)
        assert self._run(bot, r) == (False, [])


class TestNoSecondOrder:
    """⛔ THE AUDIT'S HEADLINE: a placed sale must be CONFIRMED, not repeated.

    `_cross_exit_execute` leaves the fill in `open_fills` and sets
    `w["pending"]` when the venue has not shown the sale yet. The reaction is a
    STATE, so it is still true on the next tick -- without a pending branch the
    watcher would sell the same shares twice, i.e. go naked short.
    """

    @staticmethod
    def _watched(store, r, **over):
        now = int(time.time() * 1000)
        bot = make_bot(store, r)
        bot._exit_watch[r.slug] = {
            "fid": 7, "side": "Up", "token": "t", "shares": 3.0, "stake": 2.0,
            "fee": 0.01, "entry_ms": now - 30_000, "rule": "react", "done": 0,
            "flip_ms": None, **over}
        bot.open_fills[7] = (r.slug, SimpleNamespace())
        return bot

    def test_a_pending_sale_is_NOT_re_sent(self, store):
        r = make_round()
        bot = self._watched(store, r, pending=(time.time(), 0.70, 3.0))
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)
        sells = []

        async def fake_exec(r_, w, *, tau, sig_name, confirmed=None):
            sells.append(sig_name)

        bot._cross_exit_execute = fake_exec
        bot._confirm_pending_sale = lambda r_, w: None      # venue not ready
        assert asyncio.run(bot._model_exit_check(r)) is True
        assert sells == [], "a pending sale must never send a second order"

    def test_a_pending_sale_is_confirmed_when_the_venue_catches_up(self, store):
        r = make_round()
        bot = self._watched(store, r, pending=(time.time(), 0.70, 3.0))
        got = []

        async def fake_exec(r_, w, *, tau, sig_name, confirmed=None):
            got.append((sig_name, confirmed))

        bot._cross_exit_execute = fake_exec
        bot._confirm_pending_sale = lambda r_, w: (3.0, 0.72, 0.01)
        assert asyncio.run(bot._model_exit_check(r)) is True
        assert got == [("react-confirm", (3.0, 0.72, 0.01))]

    def test_the_one_order_rail_stops_an_endless_retry(self, store):
        r = make_round()
        bot = self._watched(store, r, done=settings.cross_exit_max_per_position)
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)
        sells = []

        async def fake_exec(r_, w, *, tau, sig_name, confirmed=None):
            sells.append(sig_name)

        bot._cross_exit_execute = fake_exec
        assert asyncio.run(bot._model_exit_check(r)) is False
        assert sells == [] and r.slug not in bot._exit_watch

    def test_the_cross_watcher_ignores_a_model_watch(self, store):
        """A latent live-money bug the day cross is re-enabled."""
        r = make_round()
        bot = self._watched(store, r)
        bot._cross_exit_execute = lambda *a, **k: None
        bot._cross_signal = lambda *a, **k: ([], "fc")
        assert asyncio.run(bot._cross_exit_check(r, STRIKE, [])) is False


class TestAPartialExitStaysPartial:
    """⛔ The fraction must not become a full exit on the next tick.

    `_cross_exit_execute` SPLITS a partial sale: the sold leg settles and the
    remainder becomes a new open fill (with a new fid) that stays in
    `open_fills`. The reaction is a STATE, so it is still true -- without
    retiring the watch the very next tick sells the rest and a 25% exit silently
    becomes the 100% exit the fraction exists to avoid.
    """

    @staticmethod
    def _bot(store, r, frac):
        now = int(time.time() * 1000)
        bot = make_bot(store, r)
        bot._exit_watch[r.slug] = {
            "fid": 7, "side": "Up", "token": "t", "shares": 3.0, "stake": 2.0,
            "fee": 0.01, "entry_ms": now - 30_000, "rule": "react", "done": 0,
            "flip_ms": None}
        bot.open_fills[7] = (r.slug, SimpleNamespace())
        return bot

    def test_it_asks_the_venue_for_only_the_fraction(self, store, monkeypatch):
        monkeypatch.setattr(settings, "model_exit_frac", 0.25)
        r = make_round()
        bot = self._bot(store, r, 0.25)
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)
        seen = {}

        async def fake_exec(r_, w, *, tau, sig_name, confirmed=None,
                            shares=None):
            seen["shares"] = shares

        bot._cross_exit_execute = fake_exec
        asyncio.run(bot._model_exit_check(r))
        assert seen["shares"] == 3.0 * 0.25

    def test_a_full_fraction_sends_no_share_override(self, store, monkeypatch):
        monkeypatch.setattr(settings, "model_exit_frac", 1.0)
        r = make_round()
        bot = self._bot(store, r, 1.0)
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)
        seen = {}

        async def fake_exec(r_, w, *, tau, sig_name, confirmed=None,
                            shares=None):
            seen["shares"] = shares

        bot._cross_exit_execute = fake_exec
        asyncio.run(bot._model_exit_check(r))
        assert seen["shares"] is None

    def test_the_remainder_is_retired_so_it_cannot_be_sold_twice(
            self, store, monkeypatch):
        monkeypatch.setattr(settings, "model_exit_frac", 0.25)
        r = make_round()
        bot = self._bot(store, r, 0.25)
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)
        sales = []

        async def fake_exec(r_, w, *, tau, sig_name, confirmed=None,
                            shares=None):
            # what the real split leaves behind: a smaller open position
            sales.append(shares)
            w["shares"] = float(w["shares"]) - float(shares)

        bot._cross_exit_execute = fake_exec
        assert asyncio.run(bot._model_exit_check(r)) is True
        # the position is still open (the remainder), so the SECOND tick must
        # decline -- one sale per position.
        assert asyncio.run(bot._model_exit_check(r)) is False
        assert len(sales) == 1

    def test_a_full_exit_is_not_sold_twice(self, store, monkeypatch):
        """A full exit leaves `open_fills`, so the NEXT tick drops the watch
        without sending a second order -- the same shape the cross arm uses."""
        monkeypatch.setattr(settings, "model_exit_frac", 1.0)
        r = make_round()
        bot = self._bot(store, r, 1.0)
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)
        sales = []

        async def fake_exec(r_, w, *, tau, sig_name, confirmed=None,
                            shares=None):
            sales.append(sig_name)
            bot.open_fills.pop(w["fid"], None)      # the real path removes it

        bot._cross_exit_execute = fake_exec
        assert asyncio.run(bot._model_exit_check(r)) is True
        assert asyncio.run(bot._model_exit_check(r)) is False
        assert r.slug not in bot._exit_watch
        assert sales == ["react"]

    def test_the_share_override_reaches_the_venue(self, store, monkeypatch):
        """A source guard: the override must be USED, not just accepted."""
        src = pathlib.Path("btcpredict/main.py").read_text()
        assert "shares=float(shares if shares else w[\"shares\"])" in src


class TestTheWiring:
    @staticmethod
    def _bot(store, **kw):
        bot = make_bot(store)
        bot.broker = SimpleNamespace(mode="live", sell_fak=lambda **_: None)
        bot._exit_watch = {}
        return bot

    def test_the_model_flag_arms_the_model_rule(self, store):
        bot = self._bot(store)
        armed = bot._arm_exit_watch(
            fid=1, slug="s", side="Up", token="t", shares=1.0, stake=2.0,
            fee=0.0, entry_ms=1, rule="react")
        assert armed is True
        assert bot._exit_watch["s"]["rule"] == "react"

    def test_the_model_flag_does_not_arm_the_cross_rule(self, store,
                                                        monkeypatch):
        monkeypatch.setattr(settings, "cross_exit_enabled", False)
        bot = self._bot(store)
        assert bot._arm_exit_watch(
            fid=1, slug="s", side="Up", token="t", shares=1.0, stake=2.0,
            fee=0.0, entry_ms=1, rule="flip") is False

    def test_the_cross_flag_does_not_arm_the_model_rule(self, store,
                                                        monkeypatch):
        monkeypatch.setattr(settings, "model_exit_on_reaction", False)
        monkeypatch.setattr(settings, "cross_exit_enabled", True)
        bot = self._bot(store)
        assert bot._arm_exit_watch(
            fid=1, slug="s", side="Up", token="t", shares=1.0, stake=2.0,
            fee=0.0, entry_ms=1, rule="react") is False

    def test_a_broker_that_cannot_sell_gets_no_watch(self, store):
        bot = make_bot(store)
        bot.broker = SimpleNamespace(mode="live")       # no sell_fak
        assert bot._arm_exit_watch(
            fid=1, slug="s", side="Up", token="t", shares=1.0, stake=2.0,
            fee=0.0, entry_ms=1, rule="react") is False

    def test_the_default_rule_is_the_cross_one(self, store, monkeypatch):
        """The restart-restore path calls this with no rule and must keep the
        cross arm's behaviour."""
        monkeypatch.setattr(settings, "cross_exit_enabled", True)
        bot = self._bot(store)
        assert bot._arm_exit_watch(
            fid=1, slug="s", side="Up", token="t", shares=1.0, stake=2.0,
            fee=0.0, entry_ms=1) is True
        assert bot._exit_watch["s"]["rule"] == "flip"


class TestTheLeadMustBeAtRisk:
    """`model_exit_max_z`: a bump is not a threat when the lead is still wide.

    REGRESSION CASE -- live 2026-09-23 11:29:43, `btc-updown-5m-1790162700`.
    A Down position entered at 0.647 was **6.73bps BELOW the strike** with tau
    15.4s and the arm's own sigma at that tick (`decisions.sigma_1m` 0.000384),
    i.e. z = 3.46 -- a decided round. A **+19-point bump inside a red candle**
    (spot 85497 -> 85516, still 57 points under an 85573.6 strike) moved the 20s
    window by 5.3bps and fired the exit, which then tried to sell the position
    at 0.93 (the venue killed the order and the position won). The trigger was
    the defect, not the execution.
    """

    AHEAD_BPS = 6.73          # how far below the strike the spot was
    MOVE_AGAINST = 5.3        # the 20s move that fired the rule
    SIGMA = 0.000384          # the arm's own estimate at that tick
    TAU = 15.4

    def _bot(self, store, *, ahead_bps=AHEAD_BPS, sigma=SIGMA, tau=TAU):
        r = make_round()
        r.end_ms = int(time.time() * 1000) + int(tau * 1000)
        bot = make_bot(store, r)
        bot._sigma = sigma
        # Down is AHEAD when the spot is BELOW the strike, and the move against
        # it is the spot RISING over the lookback.
        now = STRIKE * math.exp(-ahead_bps / 1e4)
        ago = STRIKE * math.exp(-(ahead_bps + self.MOVE_AGAINST) / 1e4)
        fade(bot, r, ago, now)
        return bot

    def test_the_gate_refuses_a_decided_round(self, store, monkeypatch):
        monkeypatch.setattr(settings, "model_exit_max_z", 2.5)
        bot = self._bot(store)
        assert bot._reaction_against(bot._round, "Down") == (False, 0.0)

    def test_without_the_gate_the_same_tick_fires(self, store, monkeypatch):
        """The gate is what changes it -- the trigger itself is untouched."""
        monkeypatch.setattr(settings, "model_exit_max_z", 0.0)
        bot = self._bot(store)
        hit, mv = bot._reaction_against(bot._round, "Down")
        assert hit is True and mv <= -settings.model_exit_rev_bps

    def test_a_narrow_lead_still_exits(self, store, monkeypatch):
        """0.9bps ahead at tau 15 is z ~ 0.46: a genuine threat, sell it."""
        monkeypatch.setattr(settings, "model_exit_max_z", 2.5)
        bot = self._bot(store, ahead_bps=0.9)
        assert bot._reaction_against(bot._round, "Down")[0] is True

    def test_a_missing_sigma_never_invents_a_refusal(self, store, monkeypatch):
        monkeypatch.setattr(settings, "model_exit_max_z", 2.5)
        bot = self._bot(store, sigma=0.0)
        assert bot._reaction_against(bot._round, "Down")[0] is True

    def test_the_gate_is_off_by_default(self, store):
        """The shipped default must be the pre-2026-09-23 rule exactly."""
        assert settings.model_exit_max_z == 0.0

    def test_the_check_runs_in_the_per_market_loop_not_the_decision_block(self):
        src = pathlib.Path("btcpredict/main.py").read_text()
        hook = src.index("await self._model_exit_check(r)")
        # It must be reachable on a tick where the model does NOT emit: the
        # decision block below `return`s whenever `pred.emitted` is false, and
        # those are exactly the ticks a dying thesis has to be sold on.
        assert hook < src.index("pred = self.model.predict("), \
            "the exit must be hooked before the model-emit gate"
        # and it must sit inside the per-market loop every tick already walks
        assert src.rindex("for prefix, sym, _ in active_markets()", 0, hook) > 0

    def test_the_default_is_the_measured_cell(self):
        assert settings.model_exit_on_reaction is True
        assert settings.model_exit_look_s == 20.0
        assert settings.model_exit_rev_bps == 5.0
        assert settings.model_exit_min_tau_s == 60.0

    def test_the_measured_table_sits_next_to_the_value(self):
        src = pathlib.Path("btcpredict/config.py").read_text()
        i = src.index("model_exit_on_reaction: bool =")
        block = src[max(0, i - 4000):i]
        for tok in ("+0.0641", "+0.0317", "-0.0166", "0.7011", "0.7652",
                    "round-clustered t +1.98", "KILL CRITERION"):
            assert tok in block, tok


class TestAFullExitIsRetriedUntilSold:
    """⛔ Operator, 2026-09-23: once a 100% exit fires, keep selling until it
    is gone. The rail used to retire the watch after ONE attempt -- a failed
    sale leaves every share, so `done = max` ran and 21:09 / 22:04 each got a
    single try before settling -$4.10 / -$3.90."""

    @staticmethod
    def _bot(store, r, **over):
        now = int(time.time() * 1000)
        bot = make_bot(store, r)
        bot._exit_watch[r.slug] = {
            "fid": 7, "side": "Up", "token": "t", "shares": 5.93, "stake": 3.9,
            "fee": 0.04, "entry_ms": now - 30_000, "rule": "react", "done": 0,
            "flip_ms": None, **over}
        bot.open_fills[7] = (r.slug, SimpleNamespace())
        return bot

    def test_a_failed_sale_is_retried_next_tick_even_if_the_rule_clears(
            self, store, monkeypatch):
        monkeypatch.setattr(settings, "model_exit_frac", 1.0)
        r = make_round()
        bot = self._bot(store, r)
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)     # the rule fires
        calls = []

        async def fake_exec(r_, w, *, tau, sig_name, confirmed=None,
                            shares=None):
            calls.append(sig_name)                          # the sale fails
            if len(calls) == 3:
                bot.open_fills.pop(w["fid"], None)          # third try sells

        bot._cross_exit_execute = fake_exec
        assert asyncio.run(bot._model_exit_check(r)) is True
        # the price recovers: the RULE no longer fires, the sale still retries
        fade(bot, r, STRIKE * 1.0020, STRIKE * 1.0040)
        assert asyncio.run(bot._model_exit_check(r)) is True
        assert asyncio.run(bot._model_exit_check(r)) is True
        assert calls == ["react", "retry", "retry"], calls
        # sold: the next tick drops the watch and sends nothing
        assert asyncio.run(bot._model_exit_check(r)) is False
        assert r.slug not in bot._exit_watch and len(calls) == 3

    def test_a_remainder_after_a_stepped_down_sale_is_sold_too(
            self, store, monkeypatch):
        monkeypatch.setattr(settings, "model_exit_frac", 1.0)
        r = make_round()
        bot = self._bot(store, r)
        fade(bot, r, STRIKE * 1.0040, STRIKE * 1.0020)
        sold = []

        async def fake_exec(r_, w, *, tau, sig_name, confirmed=None,
                            shares=None):
            part = min(2.0, w["shares"])                    # thin book
            sold.append(part)
            w["shares"] = round(w["shares"] - part, 6)
            w["done"] = w.get("done", 0) + 1
            if w["shares"] <= 1e-9:
                bot.open_fills.pop(w["fid"], None)

        bot._cross_exit_execute = fake_exec
        for _ in range(5):
            asyncio.run(bot._model_exit_check(r))
        assert abs(sum(sold) - 5.93) < 1e-9, sold

    def test_dust_under_the_venue_step_is_left_alone(self, store, monkeypatch):
        monkeypatch.setattr(settings, "model_exit_frac", 1.0)
        r = make_round()
        bot = self._bot(store, r, selling=True, shares=0.004)
        bot._cross_exit_execute = None                      # must not be called
        assert asyncio.run(bot._model_exit_check(r)) is False
        assert r.slug not in bot._exit_watch

    def test_it_stops_at_the_bell(self, store, monkeypatch):
        monkeypatch.setattr(settings, "model_exit_frac", 1.0)
        r = make_round()
        r.end_ms = int(time.time() * 1000) - 1000
        bot = self._bot(store, r, selling=True)
        bot._cross_exit_execute = None
        assert asyncio.run(bot._model_exit_check(r)) is False

    def test_a_partial_exit_still_gets_one_sale(self, store, monkeypatch):
        monkeypatch.setattr(settings, "model_exit_frac", 0.25)
        r = make_round()
        bot = self._bot(store, r, selling=True,
                        done=settings.cross_exit_max_per_position)
        bot._cross_exit_execute = None
        assert asyncio.run(bot._model_exit_check(r)) is False


class TestAPendingBinanceSaleIsConfirmed:
    """`LiveBroker.confirm_sale` is ASYNC; it used to be called bare, so a
    pending Binance sale raised TypeError on `got[0]` every tick."""

    def test_the_async_confirmation_is_awaited(self, store, monkeypatch):
        monkeypatch.setattr(settings, "model_exit_frac", 1.0)
        r = make_round()
        bot = TestAFullExitIsRetriedUntilSold._bot(
            store, r, selling=True, pending=(time.time(), 0.14, 5.93))

        async def confirm_sale(*, token_id, shares, since_ts, fallback_px=0.0):
            return (5.93, 0.14, 0.01)
        bot.broker = SimpleNamespace(confirm_sale=confirm_sale)
        got = []

        async def fake_exec(r_, w, *, tau, sig_name, confirmed=None,
                            shares=None):
            got.append((sig_name, confirmed))

        bot._cross_exit_execute = fake_exec
        assert asyncio.run(bot._model_exit_check(r)) is True
        assert got == [("react-confirm", (5.93, 0.14, 0.01))]

    def test_a_sale_never_confirmed_is_retried(self, store, monkeypatch):
        import btcpredict.main as m
        monkeypatch.setattr(settings, "model_exit_frac", 1.0)
        r = make_round()
        old = time.time() - m.PENDING_SALE_GIVEUP_S - 1
        bot = TestAFullExitIsRetriedUntilSold._bot(
            store, r, selling=True, pending=(old, 0.14, 5.93))

        async def confirm_sale(*, token_id, shares, since_ts, fallback_px=0.0):
            return None
        bot.broker = SimpleNamespace(confirm_sale=confirm_sale)
        calls = []

        async def fake_exec(r_, w, *, tau, sig_name, confirmed=None,
                            shares=None):
            calls.append(sig_name)

        bot._cross_exit_execute = fake_exec
        asyncio.run(bot._model_exit_check(r))       # gives up on the pending
        assert "pending" not in bot._exit_watch[r.slug] and calls == []
        asyncio.run(bot._model_exit_check(r))       # and retries the sale
        assert calls == ["retry"]

    def test_a_fresh_pending_sale_is_NOT_given_up(self, store, monkeypatch):
        monkeypatch.setattr(settings, "model_exit_frac", 1.0)
        r = make_round()
        bot = TestAFullExitIsRetriedUntilSold._bot(
            store, r, selling=True, pending=(time.time(), 0.14, 5.93))

        async def confirm_sale(*, token_id, shares, since_ts, fallback_px=0.0):
            return None
        bot.broker = SimpleNamespace(confirm_sale=confirm_sale)
        bot._cross_exit_execute = None
        assert asyncio.run(bot._model_exit_check(r)) is True
        assert "pending" in bot._exit_watch[r.slug]
