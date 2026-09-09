import math
import pytest

from btcpredict.pricing.digital import (fair_value, implied_sigma,
                                        moneyness_sd, norm_cdf)
from btcpredict.pricing.vol import VolEstimator

SIG = 0.0004


def test_at_the_money_is_even():
    assert fair_value(100, 100, 150, SIG) == pytest.approx(0.5, abs=1e-9)


def test_expiry_is_deterministic():
    assert fair_value(101, 100, 0, SIG) == 1.0
    assert fair_value(99, 100, 0, SIG) == 0.0


def test_tie_settles_down():
    # The venue's rule is `end > strike -> Up`, so an exact tie is a Down.
    assert fair_value(100, 100, 0, SIG) == 0.0
    assert fair_value(100, 100, -1, SIG) == 0.0


def test_monotonic_in_price():
    vals = [fair_value(s, 78400, 120, SIG) for s in (78300, 78380, 78400, 78420, 78500)]
    assert vals == sorted(vals)


def test_time_decay_sharpens_a_winning_position():
    # Held above the strike, less time remaining means more certainty.
    vals = [fair_value(78450, 78400, t, SIG) for t in (240, 120, 60, 30, 10)]
    assert vals == sorted(vals)


def test_never_returns_exactly_zero_or_one_while_live():
    # A 0.0/1.0 would imply infinite edge and size a bet on a single bad tick.
    assert 0.0 < fair_value(90000, 78400, 1, SIG) < 1.0
    assert 0.0 < fair_value(60000, 78400, 1, SIG) < 1.0


def test_rejects_bad_inputs():
    for bad in ((0, 100), (100, 0), (-1, 100)):
        with pytest.raises(ValueError):
            fair_value(bad[0], bad[1], 60, SIG)
    with pytest.raises(ValueError):
        fair_value(100, 100, 60, 0.0)


def test_norm_cdf_reference_values():
    assert norm_cdf(0.0) == pytest.approx(0.5)
    assert norm_cdf(1.96) == pytest.approx(0.975, abs=1e-3)
    assert norm_cdf(-1.96) == pytest.approx(0.025, abs=1e-3)


def test_moneyness_sign():
    assert moneyness_sd(78450, 78400, 60, SIG) > 0
    assert moneyness_sd(78350, 78400, 60, SIG) < 0


def test_implied_sigma_round_trips():
    iv = implied_sigma(0.71, 78400, 78370, 120)
    assert iv is not None
    assert fair_value(78400, 78370, 120, iv) == pytest.approx(0.71, abs=1e-3)


def test_implied_sigma_undefined_at_the_money():
    # At S == K every sigma gives 0.5, so the quote carries no vol information.
    assert implied_sigma(0.5, 100, 100, 60) is None


def test_vol_floor_and_scaling():
    v = VolEstimator(floor=0.0002)
    assert v.sigma_1m() == 0.0002              # empty estimator falls back to the floor
    v.add(0.0); v.add(0.0); v.add(0.0)
    assert v.sigma_1m() >= 0.0002              # dead-quiet input cannot produce sigma 0
    v2 = VolEstimator()
    for r in (0.001, -0.001) * 30:
        v2.add(r)
    assert v2.sigma_for_tau(240) == pytest.approx(v2.sigma_1m() * 2.0, rel=1e-9)
