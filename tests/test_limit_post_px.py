"""The cross/train LIMIT must never be posted ABOVE its own ceiling.

`limit_ceiling` is `min(ask + cross_limit_buffer, cross_max_fill_px)` -- a RISK
bound, not a valuation. The first version of the post-at-the-ceiling fix used
`max(limit_px, ceiling)`, which lifts a passive price UP to the ceiling but does
nothing when `take_limit_px` already returned something higher, so every ask
above `cross_max_fill_px` was posted straight through the cap that the MARKET
branch enforces through `max_px`.

These numbers are the live config (cap 0.65, buffer 0.02, margin 0.01, tick
0.01) and the asks were read off the recorded log. They FAIL on the max()
version and pass on `limit_post_px`.
"""

import pytest

from btcpredict.edge.signal import limit_post_px, take_limit_px

CAP = 0.65
BUFFER = 0.02
MARGIN = 0.01
TICK = 0.01


def ceiling_for(ask: float, cap: float = CAP) -> float:
    """What main.py computes before calling limit_post_px (3318-3324)."""
    return min(round(ask + BUFFER, 4), cap)


def post_for(ask: float, cap: float = CAP) -> float:
    """The whole live path: take_limit_px, then the ceiling clamp."""
    ceil_ = ceiling_for(ask, cap)
    take = take_limit_px(ask, MARGIN, ask, TICK, ask)
    return limit_post_px(take, ceil_)


@pytest.mark.parametrize("ask", [0.66, 0.70, 0.80, 0.88])
def test_an_ask_above_the_cap_is_never_posted_through_it(ask):
    """The bug: take_limit_px returns the ask, which is ABOVE the capped
    ceiling, and max() posted it. 0.66/0.70/0.80/0.88 all went out whole."""
    assert post_for(ask) <= CAP
    assert post_for(ask) == pytest.approx(CAP)


def test_the_max_version_would_have_failed_this():
    """Pin the difference, so a future 'simplification' back to max() is caught
    here rather than in the log."""
    ask = 0.70
    ceil_ = ceiling_for(ask)
    take = take_limit_px(ask, MARGIN, ask, TICK, ask)
    assert max(take, min(round(ceil_, 2), 0.99)) == pytest.approx(0.70)  # the bug
    assert limit_post_px(take, ceil_) == pytest.approx(0.65)             # the fix


def test_a_passive_price_is_still_lifted_to_the_ceiling():
    """The fix must keep the property the previous change was shipped for:
    quote-1-tick becomes the ceiling and crosses. Measured live 2026-09-16 --
    0.13/0.55/0.16 posted against ceilings of 0.16/0.58/0.19, none filled."""
    for ask, want in ((0.14, 0.16), (0.56, 0.58), (0.17, 0.19)):
        assert post_for(ask) == pytest.approx(want)


@pytest.mark.parametrize("ask", [0.30, 0.51, 0.60])
def test_below_the_cap_the_post_is_the_ceiling(ask):
    assert post_for(ask) == pytest.approx(round(ask + BUFFER, 2))


def test_the_cap_bites_at_0_64_but_the_post_still_clears_the_ask():
    """ask + buffer = 0.66 > 0.65, so the cap binds 1c early. The post is
    clamped to 0.65, which is still ABOVE the 0.64 ask -- so the order is
    marketable and this is the cap working, not the cap blocking."""
    assert ceiling_for(0.64) == pytest.approx(0.65)
    assert post_for(0.64) == pytest.approx(0.65)
    assert post_for(0.64) > 0.64


def test_the_model_arm_is_untouched():
    """limit_ceiling=None is the model arm (and every non-cross caller); its
    take_limit_px value must pass through exactly."""
    for px in (0.01, 0.37, 0.99):
        assert limit_post_px(px, None) == pytest.approx(px)


def test_post_is_always_a_valid_tick_price():
    for ask in (0.01, 0.05, 0.50, 0.95, 0.99):
        p = post_for(ask)
        assert 0.01 <= p <= 0.99
        assert p == pytest.approx(round(p, 2))


def test_with_the_cap_off_only_the_buffer_bounds_it():
    """cross_max_fill_px = 0 (off) means the ceiling is ask + buffer alone, and
    the post may then exceed 0.65 -- that is the configured behaviour, not the
    bug, so it is pinned deliberately."""
    assert post_for(0.70, cap=9.0) == pytest.approx(0.72)
