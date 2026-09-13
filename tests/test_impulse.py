"""The impulse trigger: trade a real underlying move, not the model's view."""
import math

from btcpredict.edge.impulse import impulse_bps, impulse_side


def pts(*pairs):
    return [(int(ts * 1000), px) for ts, px in pairs]


def test_flat_series_has_no_impulse():
    assert impulse_bps(pts((0, 100.0), (1, 100.0), (2, 100.0)), 1.0) == 0.0


def test_move_is_measured_over_the_window():
    # +10 bps over the last second
    bps = impulse_bps(pts((0, 100.0), (1, 100.0), (2, 100.0 * math.exp(0.001))), 1.0)
    assert bps is not None
    assert abs(bps - 10.0) < 0.5


def test_down_move_is_negative():
    bps = impulse_bps(pts((0, 100.0), (1, 100.0), (2, 100.0 * math.exp(-0.0008))), 1.0)
    assert bps is not None and bps < 0


def test_window_uses_the_newest_reference_at_least_window_old():
    # a 20 bps jump 2s ago, then flat: measured over 1s it must be ~0
    series = pts((0, 100.0), (1, 100.0), (2, 100.0 * math.exp(0.002)), (3, 100.0 * math.exp(0.002)))
    assert abs(impulse_bps(series, 1.0)) < 0.5
    # ... but over 2s it is the jump itself
    assert abs(impulse_bps(series, 2.0) - 20.0) < 1.0


def test_stale_reference_is_refused():
    """An old move is not an impulse: if the only point at or before the window
    is staler than window + slack, there is nothing to trade."""
    series = pts((0, 100.0), (10, 100.0 * math.exp(0.003)))
    assert impulse_bps(series, 1.0, slack_s=1.5) is None
    # with enough slack it becomes measurable again
    assert impulse_bps(series, 1.0, slack_s=20.0) is not None


def test_short_history_is_refused():
    assert impulse_bps([], 1.0) is None
    assert impulse_bps(pts((0, 100.0)), 1.0) is None


def test_side_requires_the_threshold_and_is_symmetric():
    assert impulse_side(5.0, 4.0) == "Up"
    assert impulse_side(-5.0, 4.0) == "Down"
    assert impulse_side(3.9, 4.0) is None
    assert impulse_side(-3.9, 4.0) is None
    assert impulse_side(None, 4.0) is None
