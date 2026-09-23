"""The MODEL arm's exit, SECOND HALF: sell once the lead is GONE.

THE CASE THE REACTION RULE CANNOT SEE. `_reaction_against` requires
`log(S/K)*sgn > 0` -- we must still be AHEAD -- so the moment the spot crosses
the strike the exit is disarmed for the rest of the round. A slow bleed also
never produces the move it needs:
  MEASURED LIVE 2026-09-22 23:17, `btc-updown-5m-1790118900` Up, $2 at 0.647:
  the worst 20s move against it over the WHOLE round was **-3.47bps** against a
  5.0 threshold. The exit was armed at 23:17:51 and fired ZERO times; the
  position settled `won=0 payout=0.0000 pnl=-2.0000` at 23:20:06.

WHAT IS PINNED HERE, and why each one is a live-money failure if it breaks:
  * OFF by default, and off means byte-for-byte the old rule (the revert);
  * the state must HOLD. An ungated trigger fires deep, where a $2 position's
    notional is under the venue's ~$1.50 MARKET minimum: measured 46-49 of its
    96 triggers are REFUSED, so it can only sell where it is not needed and the
    total collapses from +53.72 to +43.85. Gated, the row is identical to the
    live rule on the drawdown (-7.87) and better on the total (+55.29);
  * the clock RESETS when we come back ahead -- a wobble across the strike must
    not accumulate time it never spent behind it;
  * a sale is still ONE sale, named by the rule that fired.
"""
from __future__ import annotations

import asyncio
import pathlib
import tempfile
import time

import pytest

from btcpredict.config import settings
from btcpredict.store.db import Store

from test_model_reaction_exit import (STRIKE, make_bot, make_round,  # noqa: E402
                                      series)

BEHIND = 2.0        # settings.model_exit_behind_bps when enabled
HOLD = 5.0          # settings.model_exit_behind_hold_s


@pytest.fixture
def store():
    """The same fixture `test_model_reaction_exit` uses. A fixture is not
    importable with the module's names, so it is re-declared rather than
    reached for."""
    with tempfile.TemporaryDirectory() as d:
        s = Store(pathlib.Path(d) / "t.db")
        yield s
        s.close()


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(settings, "model_exit_behind_bps", BEHIND)
    monkeypatch.setattr(settings, "model_exit_behind_hold_s", HOLD)
    return BEHIND


def behind_by(bot, r, bps, *, side_spot="below"):
    """A history whose LAST spot is `bps` behind the strike for `side`=Up."""
    px = STRIKE * (1.0 - bps / 1e4) if side_spot == "below" \
        else STRIKE * (1.0 + bps / 1e4)
    series(bot, r, [(30, px), (20, px), (10, px), (2, px)])


def watch(**over):
    w = {"fid": 7, "side": "Up", "token": "1", "shares": 3.09, "stake": 2.0,
         "fee": 0.02, "rule": "react", "entry_ms": int(time.time() * 1000)
         - 60_000}
    w.update(over)
    return w


class TestTheGrindTrigger:
    def test_it_is_OFF_by_default_and_off_is_the_old_rule(self, store):
        """0.0 is the shipped value AND the revert: no clock, no opinion."""
        assert settings.model_exit_behind_bps == 0.0
        r = make_round()
        bot = make_bot(store, r)
        behind_by(bot, r, 30.0)
        assert bot._grind_against(r, watch()) == (False, 0.0)

    def test_behind_the_strike_arms_the_clock_and_does_not_fire_yet(
            self, store, armed):
        """The first tick behind STARTS the hold; it must not sell into it."""
        r = make_round()
        bot = make_bot(store, r)
        w = watch()
        behind_by(bot, r, 30.0)
        hit, bps = bot._grind_against(r, w)
        assert hit is False and bps == pytest.approx(30.0, abs=0.1), (hit, bps)
        assert w["behind_since"], "the clock must be running"

    def test_it_fires_once_the_state_has_HELD(self, store, armed):
        r = make_round()
        bot = make_bot(store, r)
        w = watch(behind_since=int(time.time() * 1000) - (HOLD + 1) * 1000)
        behind_by(bot, r, 30.0)
        hit, bps = bot._grind_against(r, w)
        assert hit is True and bps == pytest.approx(30.0, abs=0.1), (hit, bps)

    def test_a_shallow_wobble_does_not_arm_it(self, store, armed):
        """Below the threshold is the venue having repriced, not a lost lead."""
        r = make_round()
        bot = make_bot(store, r)
        w = watch()
        behind_by(bot, r, BEHIND / 2.0)
        assert bot._grind_against(r, w)[0] is False
        assert not w["behind_since"], "a sub-threshold tick is not behind time"

    def test_coming_back_ahead_RESETS_the_clock(self, store, armed):
        """A wobble across the strike must not accumulate behind-time."""
        r = make_round()
        bot = make_bot(store, r)
        w = watch()
        behind_by(bot, r, 30.0)
        bot._grind_against(r, w)
        assert w["behind_since"]
        # back on our side. THREE ticks: a thinner buffer is "no opinion" and
        # deliberately does not touch the clock, so a two-tick fixture would
        # pass the `hit is False` assert while leaving the clock running.
        series(bot, r, [(30, STRIKE * 1.03), (20, STRIKE * 1.02),
                        (10, STRIKE * 1.01)])
        assert bot._grind_against(r, w)[0] is False
        assert not w["behind_since"], "the clock must be cleared"
        # and behind again is a NEW clock, not a continuation
        behind_by(bot, r, 30.0)
        assert bot._grind_against(r, w)[0] is False

    def test_it_covers_the_deep_case_that_settled_at_MINUS_100(
            self, store, armed):
        """The 23:17 shape: a grind right through the strike, no reaction."""
        r = make_round()
        bot = make_bot(store, r)
        w = watch(behind_since=int(time.time() * 1000) - 20_000)
        behind_by(bot, r, 180.0)
        hit, bps = bot._grind_against(r, w)
        assert hit is True and bps > 100.0, (hit, bps)

    def test_the_OTHER_side_is_behind_when_we_hold_Down(self, store, armed):
        r = make_round()
        bot = make_bot(store, r)
        w = watch(side="Down", behind_since=int(time.time() * 1000) - 20_000)
        behind_by(bot, r, 30.0, side_spot="above")
        assert bot._grind_against(r, w)[0] is True

    def test_no_history_is_no_opinion_not_a_refusal(self, store, armed):
        r = make_round()
        bot = make_bot(store, r)
        bot._ticks = {}
        assert bot._grind_against(r, watch()) == (False, 0.0)


class TestTheExitFiresOnceNamedByTheRuleThatSawIt:
    def _arm(self, bot, r, **over):
        bot.open_fills = {7: (r.slug, object())}
        bot._exit_watch = {r.slug: watch(**over)}
        bot.ledger = []
        return bot._exit_watch[r.slug]

    def test_the_grind_half_sells_when_the_reaction_is_silent(
            self, store, armed, monkeypatch):
        r = make_round()
        bot = make_bot(store, r)
        w = self._arm(bot, r,
                      behind_since=int(time.time() * 1000) - 20_000)
        behind_by(bot, r, 60.0)          # well behind, and no reaction shape

        seen = {}

        async def fake(r_, w_, *, tau, sig_name, confirmed=None, shares=None):
            seen["sig"] = sig_name
            seen["shares"] = shares

        bot._cross_exit_execute = fake
        assert asyncio.run(bot._model_exit_check(r)) is True
        assert seen["sig"] == "grind", seen

    def test_the_reaction_half_still_wins_and_is_named_react(
            self, store, armed):
        """Both halves true: ONE sale, and it is the reaction's."""
        r = make_round()
        bot = make_bot(store, r)
        self._arm(bot, r)
        # ahead of the strike but fading: the reaction shape
        series(bot, r, [(30, STRIKE * 1.0040), (20, STRIKE * 1.0040),
                        (10, STRIKE * 1.0020), (2, STRIKE * 1.0020)])
        seen = {}

        async def fake(r_, w_, *, tau, sig_name, confirmed=None, shares=None):
            seen["sig"] = sig_name

        bot._cross_exit_execute = fake
        assert asyncio.run(bot._model_exit_check(r)) is True
        assert seen["sig"] == "react", seen

    def test_with_the_knob_OFF_the_grind_case_holds_the_position(
            self, store, monkeypatch):
        """The revert: the 23:17 trade is left armed and silent, as before."""
        monkeypatch.setattr(settings, "model_exit_behind_bps", 0.0)
        r = make_round()
        bot = make_bot(store, r)
        w = self._arm(bot, r,
                      behind_since=int(time.time() * 1000) - 20_000)
        behind_by(bot, r, 60.0)
        called = []

        async def fake(*a, **k):
            called.append(k)

        bot._cross_exit_execute = fake
        assert asyncio.run(bot._model_exit_check(r)) is False
        assert not called, "nothing may be sold with the half disabled"
        assert not w.get("halted"), "and nothing may be halted either"


CEILING = 60.0      # settings.model_exit_behind_max_tau_s (the shipped value)


class TestTheGrindIsLateOnly:
    """⛔ A 3bps deficit is DECISIVE at tau 30 and NOISE at tau 200.

    Measured on the live entry set (167 entries, flat $5, 15.2 days), grind
    3bps/5s, `tools/model_knob_sweep.py --behind-max-tau`:
        ceiling   exits   cost     whole-100%  cat cut  WINNERS CUT
        tau<=60     6     -$1.27   20%           5        1
        tau<=90    13    -$18.87   18%           8        5
        none       16    -$13.82   16%          11        5
    Five of its six fires are REAL LOSSES and it is the only cell that improves
    maxDD (-23.27 against -24.81 for holding); by tau<=90 the winner cuts have
    gone 1 -> 5 and the cost 15x.
    """

    def _arm(self, bot, r, tau, **over):
        """`tau` = seconds left in the round, set on the round itself."""
        r.end_ms = int(time.time() * 1000) + int(tau * 1000)
        bot.open_fills = {7: (r.slug, object())}
        bot._exit_watch = {r.slug: watch(**over)}
        bot.ledger = []
        return bot._exit_watch[r.slug]

    @pytest.fixture
    def late_only(self, monkeypatch, armed):
        """The shipped combination: grind armed, ceiling 60, tau floor off."""
        monkeypatch.setattr(settings, "model_exit_behind_max_tau_s", CEILING)
        monkeypatch.setattr(settings, "model_exit_min_tau_s", 0.0)
        return CEILING

    def test_OFF_by_default_is_the_previous_behaviour(self, store, armed,
                                                      monkeypatch):
        """0.0 is the shipped default AND the revert: no ceiling, no opinion."""
        monkeypatch.setattr(settings, "model_exit_behind_max_tau_s", 0.0)
        monkeypatch.setattr(settings, "model_exit_min_tau_s", 0.0)
        r = make_round()
        bot = make_bot(store, r)
        w = self._arm(bot, r, 200.0,
                      behind_since=int(time.time() * 1000) - 20_000)
        behind_by(bot, r, 60.0)
        seen = {}

        async def fake(r_, w_, *, tau, sig_name, confirmed=None, shares=None):
            seen["sig"] = sig_name

        bot._cross_exit_execute = fake
        assert asyncio.run(bot._model_exit_check(r)) is True
        assert seen["sig"] == "grind", seen

    def test_a_deficit_EARLY_in_the_round_does_not_sell(self, store, late_only):
        """The case the ceiling exists for: 30bps behind with 200s to go is a
        wobble, and selling it is how the old grind cut its winners."""
        r = make_round()
        bot = make_bot(store, r)
        self._arm(bot, r, 200.0, behind_since=int(time.time() * 1000) - 20_000)
        behind_by(bot, r, 30.0)
        called = []

        async def fake(*a, **k):
            called.append(k)

        bot._cross_exit_execute = fake
        assert asyncio.run(bot._model_exit_check(r)) is False
        assert not called, "an early deficit is not a decided loss"

    def test_the_clock_is_NOT_STARTED_while_the_ceiling_suppresses_it(
            self, store, late_only):
        """⛔ THE REASON THE GATE IS BEFORE THE CALL, NOT AFTER IT. Gating the
        RESULT would let `_grind_against` stamp `behind_since` at tau 200, so
        the position would sell the INSTANT the ceiling lifted -- with none of
        the 5s confirmation the state exists to demand."""
        r = make_round()
        bot = make_bot(store, r)
        w = self._arm(bot, r, 200.0)          # NO clock running yet
        behind_by(bot, r, 30.0)
        called = []

        async def fake(*a, **k):
            called.append(k)

        bot._cross_exit_execute = fake
        assert asyncio.run(bot._model_exit_check(r)) is False
        assert not called
        assert not w.get("behind_since"), (
            "a suppressed tick must not accumulate behind-time")

    def test_a_deficit_INSIDE_the_ceiling_still_sells(self, store, late_only):
        """The knob must not disarm the half it belongs to."""
        r = make_round()
        bot = make_bot(store, r)
        self._arm(bot, r, 40.0, behind_since=int(time.time() * 1000) - 20_000)
        behind_by(bot, r, 30.0)
        seen = {}

        async def fake(r_, w_, *, tau, sig_name, confirmed=None, shares=None):
            seen["sig"] = sig_name

        bot._cross_exit_execute = fake
        assert asyncio.run(bot._model_exit_check(r)) is True
        assert seen["sig"] == "grind", seen

    def test_the_ceiling_does_not_touch_the_reaction_half(self, store,
                                                          late_only):
        """⛔ THE HALVES ARE SEPARATE. The ceiling is a GRIND knob: a snap-away
        at tau 200 is still the reaction rule's, whatever this is set to."""
        r = make_round()
        bot = make_bot(store, r)
        self._arm(bot, r, 200.0)
        series(bot, r, [(30, STRIKE * 1.0040), (20, STRIKE * 1.0040),
                        (10, STRIKE * 1.0020), (2, STRIKE * 1.0020)])
        seen = {}

        async def fake(r_, w_, *, tau, sig_name, confirmed=None, shares=None):
            seen["sig"] = sig_name

        bot._cross_exit_execute = fake
        assert asyncio.run(bot._model_exit_check(r)) is True
        assert seen["sig"] == "react", seen
