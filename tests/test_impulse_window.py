"""The impulse trigger's WINDOW: what span does it actually measure?

`impulse_window_s` was 1.0, which on a 1 Hz recorded tape is a single tick --
it only catches jumps, and the sweep never asked whether the venue also lags a
slower drift. `tools/backtest_impulse_orders --sweep-window` (1,128 rounds,
priced at the ask, restricted to the live path's `ask <= fair - margin`) says:

    window   triggers  fills  WR      px      EV/$1   tot     pooled-t  drop1
    0.5s       85       43    65.1%  0.4753  +0.6329  +27.22   +2.15   +0.61
    1.0s       85       43    65.1%  0.4733  +0.6379  +27.43   +2.17   +0.64
    2.0s      115       48    66.7%  0.5027  +0.6384  +30.64   +2.30   +0.43
    3.0s      173       55    69.1%  0.5275  +0.5728  +31.51   +2.34   +0.45
    5.0s      229       61    60.7%  0.5403  +0.3748  +22.86   +1.63   +0.15

2.0s is the live value: the same EV per dollar as 1.0s, 12% more fills. The
price paid rises 0.473 -> 0.540 across the sweep, which is the mechanism -- past
~3s the venue has repriced and the move is old news.

That only holds if the window really measures the SPAN. `impulse_bps` picks the
newest tick at least `window_s` old, and refuses when the only candidate is
staler than window + 1.5s slack. Both properties are pinned here, because a
window that silently degrades to "the previous tick" would reproduce 1.0s at
every setting and the sweep would be a lie.
"""
from __future__ import annotations

from btcpredict.edge.impulse import impulse_bps

NOW = 1_700_000_000_000


def ramp(window_ms: int = 3000, step_ms: int = 200, total_bps: float = 30.0,
         rise_from_ms: int = 2000):
    """A dense tick stream (5/s) rising linearly over the last `rise_from_ms`."""
    ticks = []
    for i in range(window_ms // step_ms + 1):
        ts = NOW - window_ms + i * step_ms
        frac = max(0.0, min(1.0, (ts - (NOW - rise_from_ms)) / rise_from_ms))
        ticks.append((ts, 100.0 * (1 + total_bps / 1e4 * frac)))
    return ticks


def test_a_longer_window_reaches_further_back():
    """The whole basis for moving 1.0 -> 2.0: at 1s the reference is half-way up
    the ramp, at 2s it is the bottom of it, so the two windows report different
    moves. If this ever fails, the window has collapsed to the last tick."""
    ticks = ramp()
    one = impulse_bps(ticks, 1.0, slack_s=1.5)
    two = impulse_bps(ticks, 2.0, slack_s=1.5)
    assert one is not None and two is not None
    assert 14.0 < one < 16.0, f"a 1s window should reach the ramp's midpoint: {one}"
    assert 28.0 < two < 32.0, f"a 2s window should reach the ramp's base: {two}"
    assert two > one * 1.8


def test_a_stale_reference_is_refused_not_used():
    """An old move is not an impulse. With a single tick 10s back, every window
    must return None rather than trading a 10-second-old print as if it were
    fresh -- the slack exists so a *gap* in the tape cannot be mistaken for a
    move."""
    ticks = [(NOW - 10_000, 100.0), (NOW, 100.5)]
    for w in (0.5, 1.0, 2.0, 3.0):
        assert impulse_bps(ticks, w, slack_s=1.5) is None, w


def test_the_shipped_window_is_the_measured_one():
    """2.0s came from the sweep; 1.0 was the unswept default and 5.0 measurably
    loses money (EV/$1 +0.375 against +0.638).

    Asserted against the Settings DEFAULT, not the running value: a deploy
    profile (`.env`) may legitimately pin its own window, and asserting the live
    value here made the test fail on the VPS against the profile it was about to
    be updated from.
    """
    from btcpredict.config import settings
    assert type(settings).model_fields["impulse_window_s"].default == 2.0
    # and whatever this process loaded must be a window the sweep supports
    assert settings.impulse_window_s in (0.5, 1.0, 2.0, 3.0)
