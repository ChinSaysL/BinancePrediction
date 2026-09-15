"""The FOK bound must never be authorised above the caller's own ceiling.

`slippageBps` is not a price. It is the venue's tolerance for the book moving
between the quote and the execution, and the venue anchors the bound to the
quote: `bound = quote * (1 + slip/10000)`, capped at 10000 (2x the quote) for
every client. That means the ceiling a caller passes is only actually enforced
if `slip` is derived from it.

Writing it as `slip = max(slippage_bps, room)` looks harmless -- the floor only
ADDS tolerance, and only for callers who supplied a ceiling. It is not harmless:
`room` falls below `slippage_bps` exactly when the quote has drifted up within
`slippage_bps/10000` of the ceiling, which is precisely the moment the ceiling is
about to bind. So the floor raises the bound above the ceiling exactly when the
ceiling matters.

Measured live 2026-09-15 (one attempt in the log):
    quote 0.5800  slip 300  ->  bound 0.5974,  ceiling 0.58
i.e. the venue was authorised to fill 3.74c above a trigger ask of 0.56, on an
arm whose ceiling had just been tightened to 2c. That single line re-opened part
of the leak the 2c cap was shipped to close.

These tests are written against the live numbers, so they fail on the old
expression and pass on the clamp.
"""
from __future__ import annotations

import pytest

from btcpredict.execution.live import derived_slip_bps

# (trigger ask, ceiling) pairs seen on the live tape.
CASES = [(0.50, 0.52), (0.56, 0.58), (0.58, 0.60), (0.62, 0.64), (0.70, 0.72)]


@pytest.mark.parametrize("ask,ceil", CASES)
def test_bound_never_exceeds_the_ceiling(ask, ceil):
    """For any quote between the ask and the ceiling, bound <= ceiling."""
    for quote in (ask, ask + 0.005, ceil - 0.01, ceil - 0.001, ceil):
        if quote <= 0:
            continue
        slip = derived_slip_bps(300, ceil, quote)
        bound = quote * (1 + slip / 10_000)
        assert bound <= ceil + 1e-9, (
            f"ask={ask} ceil={ceil} quote={quote} slip={slip} bound={bound}")


def test_the_live_regression_is_fixed():
    """The exact live line: quote 0.58 against ceiling 0.58.

    Old expression gave slip=300 -> bound 0.5974, which is ABOVE the ceiling.
    """
    slip = derived_slip_bps(300, 0.58, 0.58)
    assert slip == 0
    assert 0.58 * (1 + slip / 10_000) <= 0.58 + 1e-9


def test_a_quote_close_to_the_ceiling_gets_only_the_room_left():
    # 0.56 ask, 0.58 ceiling, quote drifted to 0.579 -> only ~17bps of room
    slip = derived_slip_bps(300, 0.58, 0.579)
    assert slip == 17
    assert 0.579 * (1 + slip / 10_000) <= 0.58 + 1e-9


def test_no_ceiling_keeps_the_configured_default():
    """The floor's original purpose: callers without a ceiling are untouched."""
    assert derived_slip_bps(300, None, 0.55) == 300
    assert derived_slip_bps(200, None, 0.0) == 200
    assert derived_slip_bps(300, 0.58, 0.0) == 300      # no usable quote


def test_quote_at_or_above_the_ceiling_authorises_nothing():
    """The caller refuses this case first; the helper must not invent room."""
    assert derived_slip_bps(300, 0.58, 0.58) == 0
    assert derived_slip_bps(300, 0.58, 0.60) == 0


def test_the_venue_maximum_is_respected():
    """A cheap quote would otherwise ask for more than any client may have."""
    assert derived_slip_bps(300, 0.90, 0.10) == 10_000
    assert derived_slip_bps(300, 0.90, 0.45) == 10_000


def test_room_is_positive_over_the_normal_range():
    assert derived_slip_bps(200, 0.60, 0.58) == 344
    assert derived_slip_bps(200, 0.52, 0.50) == 400
