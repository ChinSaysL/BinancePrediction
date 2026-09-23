"""`floor_max_bankroll_fraction` -- the knob that could stop the arm dead.

WHAT THE RULE DID. `stake_for_fraction(..., floor_to_minimum=True)` stakes the
venue's $1 minimum when the requested fraction cannot reach it -- but it refused
once the floor was more than `FLOOR_MAX_BANKROLL_FRACTION` (0.25) of the bankroll.
A $1 floor therefore needed a **$4.00** bankroll, and the live record carries the
consequence (main.py, 2026-09-18): on a $2.19 bankroll every cross trigger was
refused with "stake under the venue minimum", ~20/second, for an afternoon.

DISABLED 2026-09-23 on the operator's instruction, and turned into a config knob
so it is visible and revertible in one line (`floor_max_bankroll_fraction = 0.0`,
the repo's 0 = OFF convention).

WHAT IS PINNED HERE:
  * 0.0 means OFF -- the floor is staked on any bankroll, however small;
  * 0.25 still refuses exactly as it always did, so the revert path is real;
  * the disabling knob applies ONLY to that limit -- `floor_to_minimum=False`
    must still SKIP, or the change would quietly re-enable floor-to-minimum for
    everyone;
  * the arm passes the setting, so the knob is not inert dead weight.
"""
from __future__ import annotations

import pathlib

import pytest

from btcpredict.config import settings
from btcpredict.edge.signal import FLOOR_MAX_BANKROLL_FRACTION as LEGACY
from btcpredict.edge.signal import stake_for_fraction

KW = dict(fraction=0.04, min_stake=1.0, cap=0.0, floor_to_minimum=True)


def test_it_ships_disabled():
    assert settings.floor_max_bankroll_fraction == 0.0, \
        "0.0 is the shipped value AND the revert"
    # the module constant survives as the DEFAULT of the parameter, so any
    # caller that has not been updated keeps its old behaviour
    assert LEGACY == 0.25


def test_disabled_stakes_the_floor_on_a_tiny_account():
    """$2 bankroll, $1 floor: 50% of the account, and now allowed through."""
    assert stake_for_fraction(2.0, **KW,
                              floor_max_bankroll_fraction=0.0) == 1.0


def test_the_legacy_limit_still_refuses():
    """Revert path: 0.25 must reproduce the old refusal, or the revert lies."""
    assert stake_for_fraction(2.0, **KW,
                              floor_max_bankroll_fraction=0.25) == 0.0
    # ...and still stakes the floor where the rule is satisfied ($1 is <= 25%
    # of $4, so $4 is the first bankroll that passes)
    assert stake_for_fraction(3.9, **KW,
                              floor_max_bankroll_fraction=0.25) == 0.0
    assert stake_for_fraction(4.0, **KW,
                              floor_max_bankroll_fraction=0.25) == 1.0


def test_the_knob_does_not_enable_floor_to_minimum_by_itself():
    """`floor_to_minimum=False` is a DIFFERENT decision and must still skip."""
    assert stake_for_fraction(2.0, fraction=0.04, min_stake=1.0, cap=0.0,
                              floor_to_minimum=False,
                              floor_max_bankroll_fraction=0.0) == 0.0


def test_the_parameter_default_is_the_legacy_constant():
    """A caller that forgets the argument keeps 0.25, not "off"."""
    assert stake_for_fraction(2.0, **KW) == 0.0


def test_the_arm_actually_passes_the_setting():
    src = pathlib.Path("btcpredict/main.py").read_text()
    assert "floor_max_bankroll_fraction=float(" in src
    assert '"floor_max_bankroll_fraction"' in src, \
        "read via getattr: the tests' STUB configs do not carry the field, and " \
        "a bare attribute read raises AttributeError on every one of them"


def test_nothing_divides_by_the_knob():
    """The refusal message computed `min_order_usdt / FLOOR_MAX_BANKROLL_FRACTION`
    unconditionally: with the limit disableable that is a ZeroDivisionError on
    the path that reports it. Guarded now."""
    src = pathlib.Path("btcpredict/main.py").read_text()
    assert "min_order_usdt / FLOOR_MAX_BANKROLL_FRACTION" not in src
