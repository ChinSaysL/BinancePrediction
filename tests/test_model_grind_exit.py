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
