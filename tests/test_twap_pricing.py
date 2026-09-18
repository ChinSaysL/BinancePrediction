"""The TWAP model must match a simulated TWAP round, not just look plausible.

The algebra in `pricing/twap.py` has three moving parts that can each be wrong
in a way that still returns a number between 0 and 1:

  1. the `exp(x) = 1 + x` linearisation that turns "average price >= start" into
     "average log-return >= 0";
  2. `Var[integral_0^tau W_u du] = tau^3/3`, which sets the dispersion;
  3. the split of the integral into a known part `J_t` and a random remainder.

So the central test simulates actual Brownian paths, settles them with the REAL
rule (the time-average of the price must reach the start price) and checks the
closed form's probability against the realised frequency, bucketed decile by
decile. That is a calibration test: it fails if the formula is over- or
under-confident, not merely if it is off by a constant.
"""
from __future__ import annotations

import math
import random

import pytest

from btcpredict.pricing.digital import norm_cdf  # noqa: F401  (kept for parity of imports)
from btcpredict.pricing.twap import (
    point_rule_z,
    twap_dispersion_ratio,
    twap_fair_value,
    twap_settles_up,
    twap_z,
)

SIG = 0.0008          # 8 bps per minute -> ~18 bps over a 5-minute round
S0 = 100.0


def test_at_the_open_nothing_is_known_so_it_is_a_coin_flip():
    assert twap_fair_value(S0, S0, 300.0, SIG, elapsed_s=0.0,
                           realized_log_avg=0.0) == 0.5


def test_the_twap_rule_is_sqrt_three_less_dispersed_than_the_point_rule():
    """The factor that was measured independently from the 48-round comparison.

    With no realised history (`J_t = 0`) the two z-scores must differ by exactly
    sqrt(3) -- which is where the "1.73x less dispersed" number comes from.
    """
    for tau in (30.0, 60.0, 150.0, 300.0):
        for x in (1e-4, -1e-4, 5e-4):
            S = S0 * math.exp(x)
            z_tw = twap_z(S, S0, tau, SIG, elapsed_s=0.0, realized_log_avg=0.0)
            z_pt = point_rule_z(S, S0, tau, SIG)
            assert z_tw == pytest.approx(twap_dispersion_ratio() * z_pt, rel=1e-9)


def test_it_is_monotone_in_the_spot():
    vals = [twap_fair_value(S0 * (1 + d), S0, 150.0, SIG, elapsed_s=150.0,
                            realized_log_avg=0.0)
            for d in (-0.002, -0.001, -0.0002, 0.0, 0.0002, 0.001, 0.002)]
    assert vals == sorted(vals)


def test_it_degenerates_to_the_realised_sign_at_the_bell():
    """With no time left, the outcome is the sign of the realised average."""
    assert twap_fair_value(S0, S0, 0.0, SIG, realized_log_avg=1e-4) > 0.99
    assert twap_fair_value(S0, S0, 0.0, SIG, realized_log_avg=-1e-4) < 0.01
    # and near the bell, still decided by the realised part, not the level
    up = twap_fair_value(S0 * 1.00005, S0, 1.0, SIG, elapsed_s=299.0,
                         realized_log_avg=-3e-4)
    assert up < 0.5


def test_the_two_rules_disagree_in_both_directions():
    """The economics: the average so far is an anchor the point rule lacks.

    This is the whole reason the model cannot be reused: a round can be above
    its strike right now and still LOSE, because the average is what settles.
    """
    elapsed, tau = 150.0, 150.0
    # above the strike now, but the round has averaged negative -> Down
    S_up_level = S0 * math.exp(1e-4)
    p_twap = twap_fair_value(S_up_level, S0, tau, SIG, elapsed_s=elapsed,
                             realized_log_avg=-2e-4)
    assert point_rule_z(S_up_level, S0, tau, SIG) > 0
    assert p_twap < 0.5

    # below the strike now, but the round has averaged positive -> Up
    S_down_level = S0 * math.exp(-1e-4)
    p_twap2 = twap_fair_value(S_down_level, S0, tau, SIG, elapsed_s=elapsed,
                              realized_log_avg=2e-4)
    assert point_rule_z(S_down_level, S0, tau, SIG) < 0
    assert p_twap2 > 0.5


def test_a_path_that_ends_high_can_still_settle_down():
    """Ground truth, and the trap in backtesting on the wrapper's label."""
    base = 100.0
    # flat, dives, then rips back above the start at the bell
    prices = [(0, base)] + [(t, base * 0.995) for t in range(5, 200, 5)] + \
             [(t, base * 1.004) for t in range(200, 301, 5)]
    assert prices[-1][1] > base                      # the point rule says Up
    assert twap_settles_up(prices, base) is False    # the TWAP rule says Down

    # the mirror: ends below the start, but averaged above it
    mirror = [(0, base)] + [(t, base * 1.005) for t in range(5, 200, 5)] + \
             [(t, base * 0.997) for t in range(200, 301, 5)]
    assert mirror[-1][1] < base
    assert twap_settles_up(mirror, base) is True


def test_the_average_is_time_weighted_not_sample_weighted():
    """Irregular ticks: averaging the samples is a different quantity."""
    base = 100.0
    # one long stretch below the start, a brief spike above it just before the
    # bell. Sample-weighted this is ambiguous; time-weighted it is Down.
    samples = [(0, base), (1, base * 0.99), (299, base * 0.99), (300, base * 1.05)]
    assert twap_settles_up(samples, base) is False


def test_the_closed_form_matches_a_simulated_twap_round():
    """Calibration against simulated rounds, bucketed by the model's own P."""
    rng = random.Random(20260916)
    dt, steps, T = 5.0, 60, 300.0
    t_ck = 150.0
    sd_step = SIG * math.sqrt(dt / 60.0)
    n_paths = 4000

    model_p: list[float] = []
    outcome: list[bool] = []
    for _ in range(n_paths):
        x = 0.0
        int_x = 0.0     # integral of the log-return, for the analytic input
        int_s = 0.0     # integral of the PRICE, for the true settlement
        snap = None
        for i in range(steps):
            x_new = x + rng.gauss(0.0, sd_step)
            int_x += 0.5 * (x + x_new) * dt
            int_s += 0.5 * (math.exp(x) + math.exp(x_new)) * dt
            x = x_new
            if snap is None and (i + 1) * dt == t_ck:
                snap = (x, int_x / t_ck)
        # the real rule: the round's time-average price must reach S0
        up = (int_s / T) >= 1.0
        if snap is not None:
            x_ck, avg_ck = snap
            model_p.append(twap_fair_value(S0 * math.exp(x_ck), S0, T - t_ck, SIG,
                                           elapsed_s=t_ck, realized_log_avg=avg_ck))
            outcome.append(up)

    assert len(model_p) == n_paths
    # overall: the mean forecast must match the realised base rate
    base_rate = sum(outcome) / len(outcome)
    assert sum(model_p) / len(model_p) == pytest.approx(base_rate, abs=0.03)

    # calibration, decile by decile: the formula must know how sure it is
    order = sorted(range(len(model_p)), key=lambda i: model_p[i])
    bins = 10
    per = len(order) // bins
    worst = 0.0
    for b in range(bins):
        idx = order[b * per:(b + 1) * per]
        mean_p = sum(model_p[i] for i in idx) / len(idx)
        freq = sum(outcome[i] for i in idx) / len(idx)
        worst = max(worst, abs(mean_p - freq))
    assert worst < 0.08, f"worst decile miscalibration {worst:.3f}"
