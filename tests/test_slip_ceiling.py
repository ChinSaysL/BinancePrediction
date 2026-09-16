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
def test_bound_never_exceeds_the_ceiling_beyond_the_schema_minimum(ask, ceil):
    """The bound may exceed the ceiling only by the 1bp the schema forces.

    `slippageBps` is typed "Range 1-10000", so a quote sitting exactly on the
    ceiling cannot be authorised at 0 -- the venue rejects that outright. The
    permitted excess is therefore quote * 1bp (0.006c on a 0.60 quote).
    """
    for quote in (ask, ask + 0.005, ceil - 0.01, ceil - 0.001, ceil):
        if quote <= 0:
            continue
        slip = derived_slip_bps(300, ceil, quote)
        bound = quote * (1 + slip / 10_000)
        allowed = max(ceil, quote * 1.0001) + 1e-9
        assert bound <= allowed, (
            f"ask={ask} ceil={ceil} quote={quote} slip={slip} bound={bound}")


@pytest.mark.parametrize("quote", [0.02, 0.10, 0.50, 0.58, 0.599, 0.60, 0.90, 1.20])
def test_always_within_the_venues_documented_range(quote):
    """The venue schema (slippagebps/schema(1).yaml:1405) says "Range 1-10000".

    A 0 here is not a tighter cap, it is an INVALID ORDER: measured live
    2026-09-15 19:14, slip 0 returned in 19ms/18ms as "rejected" while every
    in-range attempt took 200-250ms. This is the test that would have caught it.
    """
    for ceil in (0.10, 0.52, 0.58, 0.60, 0.90):
        for default in (0, 1, 200, 300, 10_000):
            v = derived_slip_bps(default, ceil, quote)
            assert 1 <= v <= 10_000, (default, ceil, quote, v)


def test_the_live_regression_is_fixed():
    """The exact live line: quote 0.58 against ceiling 0.58.

    The old expression gave slip=300 -> bound 0.5974, which is ABOVE the ceiling.
    The fix gives 1 -- the schema floor, because 0 would be rejected outright.
    """
    slip = derived_slip_bps(300, 0.58, 0.58)
    assert slip == 1
    assert 0.58 * (1 + slip / 10_000) <= 0.58 * 1.0001 + 1e-9


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


def test_a_quote_at_or_above_the_ceiling_still_sends_the_schema_minimum():
    """The caller refuses this case first, but if it ever reaches here the value
    must still be in range -- 0 is REJECTED by the venue."""
    assert derived_slip_bps(300, 0.58, 0.58) == 1
    assert derived_slip_bps(300, 0.58, 0.60) == 1


def test_the_venue_maximum_is_respected():
    """A cheap quote would otherwise ask for more than any client may have."""
    assert derived_slip_bps(300, 0.90, 0.10) == 10_000
    assert derived_slip_bps(300, 0.90, 0.45) == 10_000


def test_room_is_positive_over_the_normal_range():
    assert derived_slip_bps(200, 0.60, 0.58) == 344
    assert derived_slip_bps(200, 0.52, 0.50) == 400


class TestQuoteViability:
    """A quote that cannot reach the ceiling must never become an order.

    Measured live 2026-09-15/16 over 112 attempts: 7 (6.25%) were PROVABLY
    unfillable -- `quote * 2 < ceiling`, because the venue anchors the FOK bound
    to the quote and caps slippage at 10000bps, i.e. 2x the quote, for every
    client. Every one of the 7 carried `slip 10000`, which is the clamp showing
    its work:
        quote 0.0696 -> x2 = 0.1392 against a ceiling of 0.650
        quote 0.0400 -> x2 = 0.0800 against a ceiling of 0.190
    Those are not market rejections. They are dead on arrival, signed by us, and
    recorded against the canary as if the market had refused them.

    A further 42 of 112 (38%) were merely STALE -- far enough below the ceiling
    that the slippage has to run at or near maximum, anchoring the order to a
    price the venue no longer has.
    """

    from btcpredict.execution.live import quote_is_unusable as _q
    _bad = staticmethod(_q)      # staticmethod: `self._bad` must not bind self

    def test_the_measured_unfillable_case_is_caught(self):
        why = self._bad(0.0696, 0.65)
        assert why and why.startswith("quote_unfillable"), why

    def test_the_measured_stale_case_is_caught(self):
        # Their example: 0.30 x2 = 0.60 >= 0.58 so the 2x math passes, but the
        # quote is 28c below the tape and would force maximum slippage.
        why = self._bad(0.30, 0.58)
        assert why and why.startswith("quote_stale"), why

    def test_a_healthy_quote_passes(self):
        assert self._bad(0.64, 0.65) is None

    def test_the_gate_uses_the_VENUE_max_not_ours(self):
        # 2q == ceiling exactly is still reachable; one tick under is not.
        assert self._bad(0.05, 0.10) is None          # 2q = 0.10, drop = 0.05
        assert self._bad(0.049, 0.10) is not None

    def test_a_quote_ABOVE_the_ceiling_is_left_to_the_existing_gate(self):
        # `quote_above_ceiling` already refuses it; this gate must not shadow it
        assert self._bad(0.70, 0.65) is None

    def test_no_ceiling_disables_the_gate(self):
        assert self._bad(0.01, None) is None
