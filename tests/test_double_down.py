"""The double-down's two load-bearing rules, tested without a venue.

Measured before shipping (tools/arm_overlap --double-at, 1,177 rounds): the
added unit is +0.0686/$1 at t 2.05 and lifts total profit ~36%, but it is bought
at a WORSE price than the first unit (~0.69 against ~0.61) so its payoff is
1.44:1 where the first unit's is 1.64:1, and it FAILS the half-sample check
(second half +0.003 against the base arm's +0.112).

Two consequences, and these tests pin both:

  1. it ships CAPPED and booked as its own mode, so one experiment cannot become
     a position size, and it can be killed on its own ledger;
  2. it is the only caller allowed to bypass the one-position-per-round
     invariant -- but ONLY that. The halts must still apply, because adding to a
     position on a day the account was already stopped is exactly the failure a
     risk guard exists to prevent.
"""
from __future__ import annotations

import pytest

from btcpredict.main import Bot
from btcpredict.risk.guards import RiskGuard


def trig(**kw):
    """Default: a winning position up 0.12 in a permissive window."""
    base = dict(entry_px=0.500, ask=0.620, tau=150.0, min_tau=60.0, max_tau=210.0,
                min_quote=0.45, max_quote=0.80, need=0.10, count=0, cap=10,
                already=False)
    return Bot.double_trigger(**{**base, **kw})


def test_adds_once_the_position_is_up_enough():
    assert trig() is not None


@pytest.mark.parametrize("ask", [0.55, 0.59, 0.599])
def test_does_not_add_before_the_price_has_actually_moved(ask):
    assert trig(ask=ask) is None


def test_one_add_per_round_ever():
    """Otherwise a rising price re-triggers on every poll, which is a martingale."""
    assert trig(already=True) is None


@pytest.mark.parametrize("count,cap", [(10, 10), (11, 10)])
def test_the_daily_cap_holds(count, cap):
    assert trig(count=count, cap=cap) is None


@pytest.mark.parametrize("tau", [59.0, 211.0, 290.0, 30.0])
def test_the_tau_window_is_respected(tau):
    """Widening the entry window turned the book negative; an add is an entry."""
    assert trig(tau=tau) is None


@pytest.mark.parametrize("ask", [0.44, 0.81, 0.99])
def test_the_quote_band_is_respected(ask):
    assert trig(ask=ask) is None


@pytest.mark.parametrize("ask,entry", [(None, 0.5), (0.7, None), (0.0, 0.5),
                                       (1.0, 0.5)])
def test_refuses_missing_or_impossible_prices(ask, entry):
    assert trig(ask=ask, entry_px=entry) is None


# --- the guard: halts apply, the round-dedup does NOT ----------------------

def guard(**kw):
    base = dict(daily_loss_cap=8.0, max_consecutive_losses=12,
                max_open_rounds=1, bankroll=lambda: 40.0)
    return RiskGuard(**{**base, **kw})


def test_check_add_permits_a_round_we_already_hold():
    """This is the whole point: `check` refuses it, `check_add` must not."""
    g = guard()
    g.on_open("btc-updown-5m-1")
    assert g.check("btc-updown-5m-1") == (False, "already positioned in this round")
    assert g.check_add("btc-updown-5m-1") == (True, "ok")


def test_check_add_still_respects_the_daily_loss_cap():
    g = guard()
    g.on_settle("btc-updown-5m-1", -8.0)          # breaches the $8 cap
    ok, why = g.check_add("btc-updown-5m-2")
    assert not ok and "daily loss cap" in why, "an add must not happen on a stopped day"


def test_check_add_still_respects_the_loss_streak():
    g = guard(max_consecutive_losses=3)
    for i in range(3):
        g.on_settle(f"btc-updown-5m-{i}", -0.5)
    ok, why = g.check_add("btc-updown-5m-9")
    assert not ok and "consecutive losses" in why


def test_check_add_respects_an_existing_halt():
    g = guard()
    g.on_settle("btc-updown-5m-1", -8.0)
    assert g.state.halted
    ok, why = g.check_add("btc-updown-5m-2")
    assert not ok and why.startswith("halted:")


def test_a_cap_of_zero_means_uncapped_not_never():
    """The repo's convention: a limit of 0 is OFF, not 'block everything'.

    Reading `count >= cap` with cap=0 blocked EVERY add, which is the opposite
    of what a zero limit means everywhere else in this repo -- and the two were
    silently interchangeable until the cap was changed.
    """
    assert trig(count=0, cap=0) is not None
    assert trig(count=50, cap=0) is not None
    assert trig(count=86, cap=0) is not None


def test_a_positive_cap_still_bites():
    assert trig(count=9, cap=10) is not None
    assert trig(count=10, cap=10) is None
