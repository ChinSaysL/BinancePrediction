"""The projection tool must model the rule that is LIVE, not the one it
remembers.

This tool has already reported a strategy nobody was trading twice: a `break`
where live uses `continue`, and a fraction rule after live moved to a flat edge
band. A projection of the wrong rule is worse than no projection, because its
numbers get quoted in decisions -- so the mode is read from settings, and a
mode whose inputs the pool does not carry raises instead of substituting.
"""
import pytest

from btcpredict.config import settings
from tools.project_balance import _base_stake


@pytest.fixture
def mode(monkeypatch):
    def _set(**kw):
        for k, v in kw.items():
            monkeypatch.setattr(settings, k, v)
    return _set


def test_fraction_mode_reports_zero_below_the_venue_minimum(mode):
    mode(sizing_mode="fraction", stake_fraction=0.02,
         stake_floor_to_minimum=False)
    assert _base_stake(100.0, 0.02, 1.0, 25.0) == pytest.approx(2.0)
    # 2% of $40 is $0.80: a skip, never a $1.00 bet -- rounding up would trade
    # a larger fraction than the one under test.
    assert _base_stake(40.0, 0.02, 1.0, 25.0) == 0.0
    # ...but with the floor on, the tool must follow the live rule and bet the
    # minimum, or it projects an idle bot while a floored one trades
    mode(stake_floor_to_minimum=True)
    assert _base_stake(40.0, 0.02, 1.0, 25.0) == pytest.approx(1.0)
    assert _base_stake(100.0, 0.02, 1.0, 25.0) == pytest.approx(2.0)


def test_a_flat_edge_band_is_the_same_stake_at_every_bankroll(mode):
    mode(sizing_mode="edge", min_stake_usdt=10.0, max_stake_usdt=10.0)
    assert _base_stake(1000.0, 0.12, 1.0, 10.0) == 10.0
    assert _base_stake(50.0, 0.12, 1.0, 10.0) == 10.0
    # and at an equity where the stake is larger than the whole account -- the
    # row that ruins an account, so it must not be smoothed over
    assert _base_stake(5.0, 0.12, 1.0, 10.0) == 10.0


def test_a_widening_edge_band_raises_instead_of_guessing(mode):
    mode(sizing_mode="edge", min_stake_usdt=5.0, max_stake_usdt=10.0)
    with pytest.raises(NotImplementedError, match="per-trade edge"):
        _base_stake(100.0, 0.12, 1.0, 10.0)


def test_kelly_raises_instead_of_falling_back_to_fraction(mode):
    mode(sizing_mode="kelly")
    with pytest.raises(NotImplementedError, match="fair/quote"):
        _base_stake(100.0, 0.12, 1.0, 10.0)


def test_the_pool_is_net_of_the_measured_entry_slip(monkeypatch):
    """The pool's payoffs come from the quote the gate saw; live pays the fill.
    A projection built on the gross pool is ~30% optimistic about the edge it
    is sizing, so the subtraction is the default -- and switchable, so the two
    numbers can be compared instead of argued about."""
    import tools.project_balance as pb

    monkeypatch.setattr(pb, "load_trades",
                        lambda market: ([{"pnl": 0.4, "mz": 0.1, "ts_ms": 0},
                                         {"pnl": -1.0, "mz": 0.1, "ts_ms": 0}], None))
    gross, _t, _r = pb.build_pools("m", slip=0.0)
    net, _t2, _r2 = pb.build_pools("m")                      # default = measured
    assert gross == pytest.approx([0.4, -1.0])
    assert net == pytest.approx([0.4 - pb.MEASURED_SLIP_PER_DOLLAR,
                                 -1.0 - pb.MEASURED_SLIP_PER_DOLLAR])
    # the cost is paid on entry, so it lands on winners and losers alike;
    # the win rate is unchanged and only the mean moves
    assert sum(1 for x in net if x <= 0) == sum(1 for x in gross if x <= 0)
    assert pb.MEASURED_SLIP_PER_DOLLAR > 0.0
