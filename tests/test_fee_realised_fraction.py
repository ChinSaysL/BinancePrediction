"""The venue charges the taker fee only on PARTIAL fills.

Measured on 762 live fills: fillPct>=0.999 pays 4.1 bps (91% pay zero),
fillPct<0.999 pays 157.4 bps. The expected charge is therefore 0.445x the
curve. `evaluate` builds the HURDLE from this rate, so overstating it makes the
bot refuse trades whose true edge clears the true bar.
"""
import pytest

import btcpredict.edge.signal as S
from btcpredict.edge.signal import (FeeModel, evaluate, expected_fee_bps_for_price,
                                    fee_bps_for_price, fee_rate_for_price)


@pytest.fixture(autouse=True)
def _restore():
    old = S.FEE_REALISED_FRACTION
    yield
    S.FEE_REALISED_FRACTION = old


def test_curve_itself_is_unchanged():
    """fee_bps_for_price still means "what is charged WHEN charged"."""
    assert fee_bps_for_price(0.65) == pytest.approx(169.4, abs=0.5)
    assert fee_bps_for_price(0.80) == pytest.approx(66.9, abs=0.5)


def test_expected_fee_applies_the_fraction():
    S.FEE_REALISED_FRACTION = 0.445
    for px in (0.55, 0.65, 0.80):
        assert expected_fee_bps_for_price(px) == pytest.approx(
            fee_bps_for_price(px) * 0.445)


def test_rate_is_the_expected_fee_not_the_curve():
    S.FEE_REALISED_FRACTION = 0.445
    assert fee_rate_for_price(0.70) == pytest.approx(
        fee_bps_for_price(0.70) * 0.445 / 1e4)


def test_fraction_of_one_restores_the_old_behaviour():
    S.FEE_REALISED_FRACTION = 1.0
    assert fee_rate_for_price(0.70) == pytest.approx(
        fee_bps_for_price(0.70) / 1e4)


def test_a_lower_fee_lowers_the_hurdle():
    """The whole point: a correct fee must let marginal trades through."""
    kw = dict(fair_up=0.78, quote_up=0.70, quote_down=0.32, fee=None,
              half_spread=0.0, safety_margin=0.01,
              fee_model=FeeModel.NOTIONAL, max_quote=0.95, min_quote=0.05,
              min_edge=0.0, edge_haircut=1.0)
    S.FEE_REALISED_FRACTION = 1.0
    high = evaluate(**kw)
    S.FEE_REALISED_FRACTION = 0.445
    low = evaluate(**kw)
    assert low.hurdle < high.hurdle, "correcting the fee must lower the hurdle"
    assert low.ev_per_dollar > high.ev_per_dollar, "and must raise EV"


def test_a_trade_refused_at_the_old_fee_can_pass_at_the_measured_one():
    """A signal sitting between the two hurdles is the trade being unlocked."""
    S.FEE_REALISED_FRACTION = 1.0
    hi = evaluate(fair_up=0.706, quote_up=0.70, quote_down=0.32, fee=None,
                  half_spread=0.0, safety_margin=0.0,
                  fee_model=FeeModel.NOTIONAL, max_quote=0.95, min_quote=0.05,
                  min_edge=0.0, edge_haircut=1.0)
    S.FEE_REALISED_FRACTION = 0.445
    lo = evaluate(fair_up=0.706, quote_up=0.70, quote_down=0.32, fee=None,
                  half_spread=0.0, safety_margin=0.0,
                  fee_model=FeeModel.NOTIONAL, max_quote=0.95, min_quote=0.05,
                  min_edge=0.0, edge_haircut=1.0)
    assert not hi.act and lo.act, (
        f"expected refuse->act; got {hi.act}/{lo.act} "
        f"(hurdles {hi.hurdle:.4f} vs {lo.hurdle:.4f})")
