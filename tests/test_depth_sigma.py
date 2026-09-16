"""The crossing-depth floor, expressed in sigma of the time remaining.

A crossing of z ~ 0 is a coin flip at ANY horizon, because what decides the
round is the diffusion still to come -- and the cross arm pays ~0.6 for the
contract, so it loses by construction. The tick floor (`cross_min_depth_ticks`)
cannot express this: it is a per-symbol correction that is a no-op on BTC
(median trigger 193 ticks).

The boundary z ~ 0.4 comes from two measurements that share no data:

  * 51 recorded cross fills against the venue's TRUE strikes (bars_1s,
    09-13..09-15).  |gap| 0-0.5 bps: n=29, win 51.7%, P&L -$10.70 of the
    -$11.37 total on 57% of the stake.
  * 1,645 settled btc-5m rounds priced at a fixed tau=19s with true strikes:
    gap within +-0.40 sigma -> P(close Up) 53.4%; 0.40-0.80 sigma -> 86.8%;
    0.80-1.59 sigma -> 95.0%.

`cross_min_depth_sigma` ships at 0.0 (OFF) -- z >= 0.4 keeps ~2-5 of 31 fills,
so enabling it is a strategy change, not a bug fix.
"""

import pytest

from btcpredict.config import settings
from btcpredict.edge.signal import depth_sigma

# measured: 2.51 bps std of 19s log returns over 13 days / 1.1M samples
S19_BPS = 2.51
SIGMA_1M = 1e-4 * S19_BPS * (60.0 / 19.0) ** 0.5


def test_the_measured_sigma_reproduces_the_boundary():
    """The two anchor measurements must agree through this function: a 1 bp gap
    at tau=19s is z ~ 0.40, and that is where P(up) jumps 53.4% -> 86.8%."""
    # 1 bp gap at tau=19s -> sigma_19s = 2.51 bps, so z = 1/2.51 = 0.398
    z = depth_sigma(100.0 * (1 + 1e-4), 100.0, SIGMA_1M, 19.0)
    assert z == pytest.approx(1.0 / S19_BPS, rel=0.02)
    assert z == pytest.approx(0.40, abs=0.01)


def test_depth_scales_as_sqrt_tau():
    """The same dollar gap is DECISIVE at tau=19s and NOISE at tau=60s. This is
    the property a fixed bps floor cannot express, and the reason the tick floor
    produced a non-monotone counterfactual."""
    a = depth_sigma(100.0 * (1 + 2e-4), 100.0, SIGMA_1M, 19.0)
    b = depth_sigma(100.0 * (1 + 2e-4), 100.0, SIGMA_1M, 76.0)
    assert a == pytest.approx(2.0 * b, rel=1e-9)     # 4x tau -> 2x sigma left


def test_it_is_unitless_across_symbols():
    """A 2.3 bp gap is the same z at $76k and at $719 -- the property the tick
    floor needed a per-symbol table for (BNB's median trigger is ONE cent)."""
    z_btc = depth_sigma(76000.0 * (1 + 2.3e-4), 76000.0, SIGMA_1M, 19.0)
    z_bnb = depth_sigma(719.0 * (1 + 2.3e-4), 719.0, SIGMA_1M, 19.0)
    assert z_btc == pytest.approx(z_bnb, rel=1e-9)


def test_the_two_gap_directions_are_symmetric():
    """`abs()` is deliberate here and is NOT the bug it was in the earlier
    cohort tables: the arm trades whichever side crossed, so depth is unsigned.
    What made those tables wrong was bucketing by abs() across MOVED trades."""
    up = depth_sigma(76010.0, 76000.0, SIGMA_1M, 45.0)
    dn = depth_sigma(75990.0, 76000.0, SIGMA_1M, 45.0)
    assert up == pytest.approx(dn)


def test_degenerate_inputs_return_zero_not_nan():
    """0.0 reads as 'not deep', so a floor refuses it. NaN would poison the
    comparison and silently pass."""
    for args in ((76000.0, 0.0, SIGMA_1M, 19.0),
                 (76000.0, 76000.0, 0.0, 19.0),
                 (76000.0, 76000.0, SIGMA_1M, 0.0)):
        assert depth_sigma(*args) == 0.0


def test_the_live_config_keeps_it_off():
    """Pinned so nobody enables a strategy change by editing a default."""
    assert settings.cross_min_depth_sigma == 0.0


def test_the_floor_would_refuse_the_band_that_lost():
    """Every fill in the losing band must fall under a 0.4 floor, and the
    profitable 2 bps at tau=19s must not."""
    # the 0-0.5 bps band at the arm's typical tau 45s
    for gap_bps in (0.05, 0.25, 0.5):
        z = depth_sigma(76000.0 * (1 + gap_bps * 1e-4), 76000.0, SIGMA_1M, 45.0)
        assert z < 0.4, gap_bps
    # the 2-4 bps band, which won 100% on n=1 but is the same direction as the
    # 1,645-round anchor
    z = depth_sigma(76000.0 * (1 + 3e-4), 76000.0, SIGMA_1M, 19.0)
    assert z > 1.0
