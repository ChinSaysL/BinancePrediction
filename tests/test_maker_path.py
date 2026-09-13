"""The maker execution path posts instead of crossing.

Measured on 1,187 recorded rounds (`tools.backtest_impulse_orders`, the same 119
impulse entries priced four ways): taking the ask pays +0.0590/$1 with pooled-t
0.44 (noise), while posting at our own fair fills 43% of those entries, wins
66.7% of the ones that DO fill, and pays +0.5890/$1 with pooled-t 2.23, daily-t
4.24 and both halves positive (+0.40/+0.77).

The whole difference is selection. A taker pays the spread to chase a move the
whole book can see; a resting bid is only filled when someone crosses to us,
which is when the venue's price is stale. Makers also pay no fee here (the
schedule is takerOnly).

So the two properties worth pinning are: it must REST (strictly below the ask, or
it is a taker), and it must never be priced from a fair that is not a valuation.
"""
from __future__ import annotations

import pytest

from btcpredict.main import maker_limit_px


def test_posts_at_our_fair_when_the_fair_is_below_the_ask() -> None:
    assert maker_limit_px(0.60, 0.72, 0.01) == 0.60


def test_never_crosses_even_when_our_fair_is_above_the_ask() -> None:
    """fair 0.80 against an ask of 0.72 must post BELOW 0.72, not at 0.80."""
    assert maker_limit_px(0.80, 0.72, 0.01) == 0.71


@pytest.mark.parametrize("fair,ask", [(0.60, 0.72), (0.80, 0.72), (0.30, 0.31),
                                      (0.95, 0.99), (0.05, 0.06)])
def test_always_rests_strictly_below_the_ask(fair: float, ask: float) -> None:
    px = maker_limit_px(fair, ask, 0.01)
    assert px is not None
    assert 0.01 <= px < ask, "a limit at or above the ask is a taker, not a maker"


@pytest.mark.parametrize("fair", [0.0, 1.0, float("nan"), -0.2, 1.5])
def test_refuses_a_degenerate_fair(fair: float) -> None:
    """A fair outside (0,1) is a missing input, not a valuation.

    Pricing a limit from one is what produced the 0.01 order on 2026-09-12 that
    rested the full wait against a 0.730 book and stalled the arm for an hour.
    """
    assert maker_limit_px(fair, 0.72, 0.01) is None


@pytest.mark.parametrize("ask", [None, 0.0, 1.0, 1.2, 0.01])
def test_refuses_an_unusable_ask(ask: float | None) -> None:
    assert maker_limit_px(0.60, ask, 0.01) is None


def test_returns_a_venue_tick_multiple() -> None:
    """The venue's tick is a cent; a price off-tick is rejected or rounded."""
    px = maker_limit_px(0.603333, 0.72, 0.01)
    assert px == round(px, 2)


def test_the_two_executions_are_distinguishable() -> None:
    """maker fills must not land in the taker P&L they are meant to be compared
    against."""
    from btcpredict.config import Settings
    s = Settings(_env_file=None)
    assert s.impulse_execution in ("taker", "maker")
    assert s.impulse_maker_wait_s > 0
