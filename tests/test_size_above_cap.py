"""A $1.00 stake is not expressible on Polymarket above ~$0.20.

MEASURED, 2026-09-16. `stake_cap_usdt=1.0` is applied to `stake` in main.py and
every ledger row records $1.00. But the broker sizes with

    shares = max(r.min_order or 5, ceil(stake / limit))

and `r.min_order` is 5.0 on Polymarket, so at a 0.90 limit the MAX picks 5
shares: **$4.50 of notional for a $1.00 stake**, with nothing in the record
showing it. `MIN_NOTIONAL` cannot catch it -- `shares * limit >= stake` by
construction at any price -- and `plan()`/`buy_limit` had no check above.

These tests pin the size arithmetic and the refusal at both order paths, so a
future change that lets the venue minimum win again fails here instead of in
the account.
"""
from __future__ import annotations

import math

import pytest

from btcpredict.venues.polymarket import broker as B

CAP = 1.0


# -- the predicate ---------------------------------------------------------

@pytest.mark.parametrize("limit, expect_cost", [(0.90, 4.50), (0.99, 4.95),
                                                (0.50, 2.50), (0.30, 1.50)])
def test_venue_minimum_beats_the_stake_cap(limit, expect_cost):
    """5 shares (the venue minimum) vs `ceil(stake/limit)` (2 at 0.90)."""
    shares = max(B.DEFAULT_MIN_SHARES, math.ceil(CAP / limit))
    assert shares == B.DEFAULT_MIN_SHARES
    assert shares * limit == pytest.approx(expect_cost)
    assert B._size_above_cap(shares, limit, CAP)


@pytest.mark.parametrize("limit, expect", [(0.20, False), (0.15, True),
                                           (0.10, False), (0.05, False)])
def test_below_a_fifth_it_mostly_fits(limit, expect):
    """NOT monotone in the price, because `ceil(stake/limit)` is not: at 0.20
    the size is 5 shares and $1.00 fits exactly, at 0.15 it is 7 shares and
    $1.05 does not, at 0.10 it is 10 and $1.00 fits again. So the refusal is
    about the PAIR (shares, price) and cannot be predicted from a price."""
    shares = max(B.DEFAULT_MIN_SHARES, math.ceil(CAP / limit))
    assert B._size_above_cap(shares, limit, CAP) is expect
    assert (shares * limit > CAP) is expect      # the check, restated


def test_cap_zero_is_off():
    """0 = no cap, the repo's convention for every other limit."""
    assert not B._size_above_cap(B.DEFAULT_MIN_SHARES, 0.99, 0.0)
    assert not B._size_above_cap(500, 0.01, 0.0)


def test_the_check_is_about_the_minimum_not_about_large_stakes():
    """It refuses when the SMALLEST order the venue accepts exceeds the cap.
    A stake of $100 buying $100.80 at 0.90 is the stake's own size, and main.py
    already `min(stake, cap)`s that -- the broker is not the place to re-clamp
    it, or every normal-sized trade would be refused."""
    assert not B._size_above_cap(112, 0.90, 0.0)     # cap off
    assert B._size_above_cap(112, 0.90, CAP)         # $100.80 > $1.00, true
    assert B._size_above_cap(5, 0.90, CAP)           # ...and so is the minimum


def test_the_500_share_order_is_still_caught_by_the_price_floor():
    """The 17:29 incident: `ceil(5/0.01)` = 500 shares at 0.01 = $5.00, which
    alone is over the cap -- but the entry floor catches it FIRST, on the
    PRICE, and that is the guard that names it. This test only records that
    both guards see it, so neither can be dropped on the assumption that the
    other one covers it."""
    assert B._below_entry_floor(0.01, B.DEFAULT_MIN_ENTRY_PX)
    assert B._size_above_cap(500, 0.01, 1.0)         # 500 x 0.01 = $5.00 > $1
    # The 500 comes from the SIZE, and the size comes from the `limit`, which is
    # why the price floor is the real fix and this cap is only the backstop.


def test_the_cap_is_on_both_order_paths():
    """A floor enforced in one of two order paths is not a floor -- the same
    reason `_below_entry_floor` is one predicate used twice."""
    import inspect
    src = inspect.getsource(B)
    assert src.count("_size_above_cap(shares, limit, self.cfg.stake_cap_usdt)") == 2
    assert src.count('self.last_failure = "size_above_cap"') == 2
    # The AMOUNT path needs the third clamp, and it is a different one: it has
    # no share count to test, so the cap binds on the dollars directly. Without
    # it a FAK would spend the whole stake above the cap on every order.
    assert src.count("min(stake, self.cfg.stake_cap_usdt)") == 1


def test_the_config_carries_the_cap_and_defaults_to_off():
    cfg = B.PmBrokerConfig()
    assert cfg.stake_cap_usdt == 0.0
    assert B.PmBrokerConfig(stake_cap_usdt=1.0).stake_cap_usdt == 1.0


def test_registry_passes_the_cap_through():
    """The guard is inert unless the setting reaches the broker."""
    from types import SimpleNamespace
    from btcpredict.venues.registry import pm_config
    assert pm_config(SimpleNamespace(stake_cap_usdt=1.0)).stake_cap_usdt == 1.0
    assert pm_config(SimpleNamespace()).stake_cap_usdt == 0.0


def test_the_refusal_cannot_pause_every_family():
    """A bound refusing is not an order failing. Without the name in
    `no_cooldown` this would pause every family for 30 s -- the coupling that
    once made a tighter ceiling throttle the arm."""
    import inspect
    from btcpredict import main
    src = inspect.getsource(main)
    assert '"size_above_cap"' in src.split("no_cooldown = {")[1].split("}")[0]


# -- shrinking to fit, instead of refusing (2026-09-17) --------------------
#
# MEASURED: 25 of 25 order attempts over 5 hours were refused by the predicate
# above, every one `NO FILL in 0.00s` -- the order never reached the venue -- so
# the cap made the arm inert while looking like a venue problem. The predicate
# was right that the size it was HANDED exceeded the cap; it was wrong to give
# up, because a smaller legal size existed.

def test_a_legal_size_is_chosen_instead_of_refusing():
    """`ceil(5/0.24) = 21` costs $5.04, but 20 shares cost $4.80 and fit."""
    assert B._shares_within_cap(21, 0.24, 5.0, B.DEFAULT_MIN_SHARES) == 20.0


def test_it_still_refuses_when_no_size_both_trades_and_fits():
    """At 0.90 the venue's 5 shares cost $4.50 and 2 shares cost $1.80 but are
    below its minimum, so at a $2.50 cap there is genuinely nothing to send.
    `None` is the honest answer."""
    assert B._shares_within_cap(3, 0.90, 2.5, B.DEFAULT_MIN_SHARES) is None


@pytest.mark.parametrize("limit", [0.24, 0.30, 0.50, 0.67, 0.90])
def test_at_a_cap_equal_to_the_notional_floor_nothing_can_be_fitted(limit):
    """⛔ THE LIVE CONFIGURATION, 2026-09-17. `stake_cap_usdt=1.0` on a $20
    account, with `MIN_NOTIONAL` also 1.0: the notional must be >= $1.00 and
    <= $1.00, so only prices that divide $1.00 exactly can be expressed. Every
    other price is unsendable no matter how the size is chosen, which is why 25
    of 25 attempts were refused and none reached the venue."""
    assert B._shares_within_cap(max(B.DEFAULT_MIN_SHARES, math.ceil(1 / limit)),
                                limit, CAP, B.DEFAULT_MIN_SHARES) is None


def test_the_fit_respects_the_venue_notional_floor():
    """5 shares at 0.19 is $0.95 -- within the cap but under `MIN_NOTIONAL`, so
    it is still refused. This is why raising the cap is the only lever that
    unblocks the arm's 0.50-0.90 population: below ~$1 of notional nothing is
    sendable at all."""
    assert B._shares_within_cap(6, 0.19, CAP, B.DEFAULT_MIN_SHARES) is None


@pytest.mark.parametrize("limit", [0.05, 0.08, 0.10, 0.125, 0.20])
def test_a_fitted_size_never_costs_more_than_the_cap(limit):
    for cap in (1.0, 2.5, 4.5, 5.0):
        got = B._shares_within_cap(max(B.DEFAULT_MIN_SHARES, math.ceil(1 / limit)),
                                  limit, cap, B.DEFAULT_MIN_SHARES)
        if got is not None:
            assert got * limit <= cap + 1e-9
            assert got >= B.DEFAULT_MIN_SHARES
            assert got * limit >= B.MIN_NOTIONAL


def test_cap_zero_is_no_cap_so_there_is_nothing_to_fit():
    assert B._shares_within_cap(500, 0.01, 0.0, B.DEFAULT_MIN_SHARES) == 500


def test_a_cap_that_can_express_the_bands_top_trades_five_shares():
    """The number the operator needs: at 0.90 the venue's minimum 5 shares cost
    $4.50, so `stake_cap_usdt >= 4.50` is the smallest cap that can express the
    cross arm's whole measured band."""
    assert B._shares_within_cap(5, 0.90, 4.5, B.DEFAULT_MIN_SHARES) == 5.0
    assert B._shares_within_cap(5, 0.90, 4.49, B.DEFAULT_MIN_SHARES) is None
