"""The Polymarket terminal distribution must match the venue's REAL rule.

Like `test_twap_pricing.py`, this does not check that the formula "looks
plausible". It simulates Brownian rounds, settles them with the actual rule
(the TIME-AVERAGE of the price over the settlement window must reach the price
at the round's start) and compares the closed form against the realised
frequency, decile by decile. A formula that is over- or under-confident fails
that even when it is off by no constant at all.

The candidates in `tools/pm_rule_ident.py` are 5x apart in variance
(`sigma^2*L/3`), so the tests also pin the pieces that separate them: the two
regimes of `W`, the `sqrt(3)` against the point rule at the window's own
horizon, and the `sqrt(T/L)` against the whole-round window.
"""
from __future__ import annotations

import math
import pathlib
import random

import pytest

from btcpredict.models import MODEL_NAMES, build_model
from btcpredict.models.base import ModelContext
from btcpredict.models.twap import TwapModel
from btcpredict.pricing.twap import twap_fair_value
from btcpredict.venues.polymarket.pricing import (
    FEE_RATE,
    FULL,
    TAIL,
    TWAP_LOOKBACK_S,
    breakeven_ask,
    decisiveness_vs_full,
    edge_per_share,
    fair_value,
    fee_as_stake_fraction,
    fee_per_share,
    integral_variance,
    settles_up,
    terminal_rule,
    window_log_avg,
    window_log_integral,
    window_seconds,
    z_score,
)

SIG = 0.0008          # 8 bps per minute -> ~18 bps over a 5-minute round
S0 = 100.0
T = 300.0
L = TWAP_LOOKBACK_S

#: For the subprocess guard below: the package must be importable from the
#: repo root, exactly as it is when the bot runs.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _p(pair) -> float:
    return pair[0]


def _assumed(pair) -> bool:
    return pair[1]


# ---------------------------------------------------------------------------
# the two regimes
# ---------------------------------------------------------------------------

def test_at_the_open_nothing_is_known_so_it_is_a_coin_flip():
    assert _p(fair_value(S0, S0, T, SIG)) == 0.5


def test_the_window_is_sqrt_three_less_dispersed_than_a_point_at_its_own_horizon():
    """At the moment the window opens, the TWAP z is exactly sqrt(3) x the
    point rule's z at a 60-second horizon -- the same factor the whole-round
    derivation produces, because a window of L seconds is an average of L.

    Checked through `z_score` rather than through a probability so a broken
    dispersion cannot hide behind a saturating Phi.
    """
    for x in (2e-4, -2e-4, 5e-4):
        S = S0 * math.exp(x)
        z_tw = z_score(S, S0, L, SIG)                       # tau == L
        z_pt = x / (SIG * math.sqrt(L / 60.0))              # point rule, 60 s
        assert z_tw == pytest.approx(math.sqrt(3.0) * z_pt, rel=1e-9)


def test_the_two_regimes_agree_at_the_window_open():
    """`tau >= L` and `tau < L` are different formulas for the same quantity.

    They must meet at `tau == L` with `J = 0`, or the model steps discontinuously
    as the window opens -- which would be a threshold artefact in the arm.
    """
    for x in (1e-4, -3e-4):
        S = S0 * math.exp(x)
        above = z_score(S, S0, L + 1e-6, SIG)
        below = z_score(S, S0, L - 1e-6, SIG, window_log_int=0.0)
        assert above == pytest.approx(below, rel=1e-4)


def test_the_integral_variance_matches_the_definition():
    """`Var[integral_a^b W du]` from `Var[integral_0^s W] = s^3/3`."""
    assert integral_variance(0.0, 60.0) == pytest.approx(60.0 ** 3 / 3.0)
    # a later window is MORE uncertain, not less: the path wanders further
    assert integral_variance(90.0, 150.0) > integral_variance(0.0, 60.0)
    # and a degenerate window has no variance at all
    assert integral_variance(10.0, 10.0) == 0.0


def test_far_from_the_window_the_tail_rule_converges_to_the_point_rule():
    """A window 5 half-lives away carries no information about the level: the
    mean is `L*x`, the variance grows like `L^2*tau`, and `z` degenerates to the
    point rule's. This is the check that the regime-1 variance is not merely
    plausible but asymptotically right.
    """
    x = 1e-3
    S = S0 * math.exp(x)
    z_far = z_score(S, S0, 50_000.0, SIG)
    z_pt = x / (SIG * math.sqrt(50_000.0 / 60.0))
    assert z_far == pytest.approx(z_pt, rel=1e-3)


def test_the_realised_window_anchor_flips_the_sign_both_ways():
    """The economics of the venue's rule: the window's average so far is an
    anchor the point rule has no concept of, and it points both directions.
    """
    # spot ABOVE the strike, window average BELOW it -> Down
    S_hi = S0 * math.exp(1e-4)
    p = _p(fair_value(S_hi, S0, 30.0, SIG, window_log_int=-2e-4 * 30.0))
    assert S_hi > S0
    assert p < 0.5

    # spot BELOW the strike, window average ABOVE it -> Up
    S_lo = S0 * math.exp(-1e-4)
    p2 = _p(fair_value(S_lo, S0, 30.0, SIG, window_log_int=2e-4 * 30.0))
    assert S_lo < S0
    assert p2 > 0.5


def test_at_the_bell_it_is_the_sign_of_the_realised_window():
    assert _p(fair_value(S0, S0, 0.0, SIG, window_log_int=1e-4)) > 0.99
    assert _p(fair_value(S0, S0, 0.0, SIG, window_log_int=-1e-4)) < 0.01
    # and NEAR the bell the realised part still decides, not the level
    up = _p(fair_value(S0 * 1.00005, S0, 1.0, SIG,
                       window_log_int=-2e-3 * 59.0))
    assert up < 0.5


def test_a_flat_assumed_window_is_reported_and_is_not_the_same_as_a_measured_one():
    """`None` is an assumption, not a neutral value. The model says which it
    used, so a price off a guess is distinguishable in the ledger."""
    measured = fair_value(S0, S0, 30.0, SIG, window_log_int=-1e-3 * 30.0)
    assumed = fair_value(S0, S0, 30.0, SIG)
    assert _assumed(measured) is False
    assert _assumed(assumed) is True
    # the flat assumption uses the CURRENT level, so at S == K it says 0.5
    assert _p(assumed) == pytest.approx(0.5, abs=1e-9)
    # regime 1 needs no J at all, so nothing was assumed
    assert _assumed(fair_value(S0 * 1.0002, S0, 150.0, SIG)) is False


def test_the_flat_assumption_is_the_optimistic_side_near_the_bell():
    """Documented direction of the error, so nobody 'fixes' it by accident."""
    below = _p(fair_value(S0, S0, 30.0, SIG, window_log_int=-3e-4 * 30.0))
    flat = _p(fair_value(S0, S0, 30.0, SIG))
    assert flat > below


# ---------------------------------------------------------------------------
# the window question
# ---------------------------------------------------------------------------

def test_the_measured_rule_is_the_tail_window():
    assert terminal_rule() == TAIL
    assert window_seconds(TAIL, 300.0) == L
    # the tail window is a venue constant: the round's length cannot move it
    assert window_seconds(TAIL, 900.0) == L


def test_a_full_window_without_a_round_length_raises_rather_than_guessing():
    """A silent 300 s default would price a 15m round as a 5m one."""
    with pytest.raises(ValueError):
        window_seconds(FULL, 150.0)
    with pytest.raises(ValueError):
        fair_value(S0, S0, 150.0, SIG, window=FULL)


def test_decisiveness_reflects_when_the_window_actually_opens():
    """The two windows are the SAME object when the tail window opens, and the
    tail is the more CONSERVATIVE one at the round's open -- its window is 240 s
    in the future while the full-round window starts now.

    The slogan "the tail rule is 5x less dispersed in variance" describes the
    terminal distribution of a window that starts now; it is not the
    conditional z mid-round, and sizing a mid-round entry off it over-states
    the tail rule's confidence. Both halves are checked here, and the last
    assertion measures the ratio through `z_score` rather than through the
    helper that computes it.
    """
    assert decisiveness_vs_full(L) == pytest.approx(1.0, rel=1e-12)
    at_open = decisiveness_vs_full(T)
    assert at_open == pytest.approx(0.6202, abs=1e-3)
    assert 0.0 < at_open < 1.0                 # LESS decisive, not more
    # ... and the two rules converge again far from the bell
    assert decisiveness_vs_full(50_000.0) == pytest.approx(1.0, rel=1e-2)

    x = 1e-4
    S = S0 * math.exp(x)
    z_tail = z_score(S, S0, T, SIG, window=TAIL)
    z_full = z_score(S, S0, T, SIG, window=FULL, round_seconds=T)
    assert z_tail / z_full == pytest.approx(decisiveness_vs_full(T), rel=1e-9)
    assert z_tail < z_full

    # inside the tail window there is no like-for-like comparison to make
    with pytest.raises(ValueError):
        decisiveness_vs_full(L - 1.0)


def test_the_full_window_is_the_shared_whole_round_derivation():
    """`window='full'` must reproduce `pricing.twap.twap_fair_value` exactly.

    Two implementations of one derivation is a drift risk, so they are pinned
    against each other rather than trusted. `realized_log_avg` there is the mean
    of `ln(S/K)` over `[0, elapsed]`; `window_log_int` here is the same integral.
    """
    for tau, elapsed, avg in ((150.0, 150.0, 2e-4), (30.0, 270.0, -1e-4),
                              (240.0, 60.0, 0.0)):
        S = S0 * math.exp(3e-4)
        mine = _p(fair_value(S, S0, tau, SIG, window=FULL,
                             elapsed_s=elapsed,
                             window_log_int=avg * elapsed))
        theirs = twap_fair_value(S, S0, tau, SIG, elapsed_s=elapsed,
                                 realized_log_avg=avg)
        assert mine == pytest.approx(theirs, abs=1e-9)


# ---------------------------------------------------------------------------
# the realised window (`J`)
# ---------------------------------------------------------------------------

def test_the_window_integral_is_time_weighted_not_sample_weighted():
    """Irregular samples: averaging them is a different quantity.

    60 samples at `K` and one excursion at the very end. The excursion is 1 of
    61 SAMPLES but 0.5 of 60 SECONDS, and the integral follows the clock.
    """
    samples = [(float(t), S0) for t in range(0, 59_000, 1_000)] + \
              [(59_500.0, S0 * math.exp(1e-2))]
    got = window_log_integral(samples, S0, open_ms=0.0, close_ms=60_000.0)
    assert got == pytest.approx(1e-2 * 0.5, abs=1e-9)

    # the last observed value is held FORWARD to the window's close ...
    assert window_log_integral(samples, S0, open_ms=59_000.0,
                              close_ms=60_000.0) == pytest.approx(
        1e-2 * 0.5, abs=1e-9)
    # ... which means a sample sitting exactly ON the close describes nothing
    # inside the window. A caller working from per-second CLOSES must pass the
    # last one AT the close and expect it to be ignored, or count one extra.
    assert window_log_integral([(0.0, S0), (60_000.0, S0 * math.exp(1e-2))],
                               S0, open_ms=0.0, close_ms=60_000.0) == 0.0


def test_the_window_integral_returns_none_when_nothing_is_known():
    """A missing window is not a window that averaged zero."""
    assert window_log_integral([], S0, open_ms=0.0, close_ms=1_000.0) is None
    # every sample lies AFTER the window's close: no evidence about it
    assert window_log_integral([(5_000.0, S0)], S0, open_ms=0.0,
                               close_ms=1_000.0) is None
    assert window_log_integral([(0.0, S0)], 0.0, open_ms=0.0,
                               close_ms=1_000.0) is None


def test_the_log_average_is_the_integral_over_the_span():
    samples = [(0.0, S0 * math.exp(2e-4)), (30_000.0, S0 * math.exp(2e-4))]
    integ = window_log_integral(samples, S0, open_ms=0.0, close_ms=30_000.0)
    avg = window_log_avg(samples, S0, open_ms=0.0, close_ms=30_000.0)
    assert integ == pytest.approx(2e-4 * 30.0)
    assert avg == pytest.approx(2e-4)


# ---------------------------------------------------------------------------
# the ground-truth label
# ---------------------------------------------------------------------------

def test_settles_up_uses_the_venue_rule_not_the_point_rule():
    """A path that ends HIGH can still settle Down, and the mirror too.

    This is why the wrapper's settlement column cannot score this model: on
    those rounds the two venues are answering about different bets.
    """
    base = S0
    # dives through the window, then rips back above the start at the bell
    prices = [(t * 1_000.0, base * 0.995) for t in range(240, 300, 5)] + \
             [(300_000.0, base * 1.004)]
    assert prices[-1][1] > base                          # point rule: Up
    assert settles_up(prices, base, start_ms=0.0, end_ms=300_000.0) is False

    mirror = [(t * 1_000.0, base * 1.005) for t in range(240, 300, 5)] + \
             [(300_000.0, base * 0.997)]
    assert mirror[-1][1] < base
    assert settles_up(mirror, base, start_ms=0.0, end_ms=300_000.0) is True


def test_settles_up_covers_only_the_last_sixty_seconds():
    """What happened 4 minutes ago must NOT enter the tail window's average."""
    base = S0
    samples = [(t * 1_000.0, base * 0.99) for t in range(0, 240, 5)] + \
              [(t * 1_000.0, base * 1.01) for t in range(240, 301, 5)]
    # the round's own average is below the start (the first 4 minutes dominate)
    whole = settles_up(samples, base, start_ms=0.0, end_ms=300_000.0,
                       window=FULL)
    # the last 60 s are above it
    tail = settles_up(samples, base, start_ms=0.0, end_ms=300_000.0,
                      window=TAIL)
    assert whole is False
    assert tail is True
    # and the full window needs a start, or it has nothing to average
    assert settles_up(samples, base, end_ms=300_000.0, window=FULL) is None


# ---------------------------------------------------------------------------
# the fee and the edge
# ---------------------------------------------------------------------------

def test_the_fee_matches_the_venue_to_the_cent():
    """The one live fill that pinned the formula (2026-09-16 17:29:42Z):
    500 shares at 0.01 -> `entry_fees_usdc 0.3465`.
    """
    assert fee_per_share(0.01) * 500 == pytest.approx(0.3465, abs=1e-9)
    assert FEE_RATE == 0.07


def test_the_fee_shape_is_the_opposite_of_the_wrapper_s_flat_rate():
    """350 bps of stake at 0.50, 70 bps at 0.90, 693 bps at 0.01.

    The wrapper charges a FLAT measured 96 bps of stake. Pricing a Polymarket
    entry on that number would over-charge favourites and, much worse,
    under-charge the cheap side by an order of magnitude.
    """
    assert fee_as_stake_fraction(0.50) * 1e4 == pytest.approx(350.0)
    assert fee_as_stake_fraction(0.90) * 1e4 == pytest.approx(70.0)
    assert fee_as_stake_fraction(0.01) * 1e4 == pytest.approx(693.0)
    # monotone: the cheaper the contract, the more expensive the trade
    assert fee_as_stake_fraction(0.05) > fee_as_stake_fraction(0.50)


def test_edge_per_share_is_p_minus_price_minus_fee_and_makers_pay_nothing():
    assert edge_per_share(0.92, 0.88) == pytest.approx(
        0.92 - 0.88 - fee_per_share(0.88))
    assert edge_per_share(0.92, 0.88, maker=True) == pytest.approx(0.92 - 0.88)
    # a win rate is meaningless without the price: 93% winners still lose
    assert edge_per_share(0.93, 0.99) < 0


def test_the_breakeven_ask_solves_the_fee_identity():
    for p in (0.50, 0.80, 0.914, 0.99):
        a = breakeven_ask(p)
        assert edge_per_share(p, a) == pytest.approx(0.0, abs=1e-12)
    # it is BELOW the win rate, and by the fee -- the trap in turning a
    # measured win rate into a ceiling
    assert breakeven_ask(0.914) < 0.914


# ---------------------------------------------------------------------------
# the model adapter
# ---------------------------------------------------------------------------

def _ctx(**kw) -> ModelContext:
    base = dict(closes=[S0], price=S0, strike=S0, tau_s=30.0, sigma_1m=SIG)
    base.update(kw)
    return ModelContext(**base)


def test_the_model_is_registered_and_selectable_by_name():
    assert "twap" in MODEL_NAMES
    m = build_model("twap")
    assert isinstance(m, TwapModel)
    assert m.params["window"] == TAIL


def test_the_model_prices_the_anchored_window_and_says_so():
    below = TwapModel().predict(_ctx(window_log_int=-3e-4 * 30.0))
    assert below.emitted
    assert below.p_up < 0.5
    assert "J" in below.reason and "flat" not in below.reason

    flat = TwapModel().predict(_ctx())
    assert flat.emitted
    assert "flat" in flat.reason

    # with no window integral at all it still emits a number -- it is a
    # pricing model, not a gate -- but the assumption is on the record
    assert TwapModel().predict(_ctx(tau_s=150.0)).p_up == pytest.approx(0.5)


def test_the_model_refuses_rather_than_invents_when_inputs_are_missing():
    # `price` self-fills from the closes, so that case has to empty them too.
    cases = {"price": {"price": None, "closes": []},
             "strike": {"strike": None},
             "tau_s": {"tau_s": None},
             "sigma_1m": {"sigma_1m": None}}
    for name, kw in cases.items():
        pred = TwapModel().predict(_ctx(**kw))
        assert pred.emitted is False, name
        assert 0.0 < pred.p_up < 1.0, name


def test_the_model_can_be_switched_to_the_whole_round_window():
    m = build_model("twap", {"window": FULL, "round_seconds": T})
    ctx = _ctx(tau_s=150.0, window_log_int=0.0)
    assert m.predict(ctx).emitted
    assert "full" in m.predict(ctx).reason
    # With the window still 90 s in the future, the TAIL rule is the more
    # conservative of the two: it says nothing about the next 90 s, while the
    # full-round window is already running and averages the current level over
    # all 150 s remaining.
    spot = _ctx(tau_s=150.0, price=S0 * math.exp(1e-4))
    tail = build_model("twap").predict(spot)
    whole = m.predict(spot)
    assert tail.p_up < whole.p_up
    # at tau=150 the tail window has not opened, so nothing is assumed ...
    assert tail.reason == "twap:tail J"
    # ... while inside it an absent integral IS an assumption, and is labelled
    inside = build_model("twap").predict(_ctx(tau_s=30.0))
    assert inside.reason == "twap:tail flat"


def test_the_full_window_fails_closed_without_a_round_length():
    """A silent 300 s default would price a 15m round as a 5m one."""
    m = build_model("twap", {"window": FULL})
    pred = m.predict(_ctx(tau_s=150.0))
    assert pred.emitted is False
    assert "round_seconds" in pred.reason


# ---------------------------------------------------------------------------
# calibration against the real rule
# ---------------------------------------------------------------------------

def test_the_closed_form_matches_a_simulated_tail_twap_round():
    """Simulate real rounds, settle them with the ACTUAL rule, bucket the
    model's own P and require the realised frequency to track it.

    Two checkpoints on the SAME paths, one in each regime: `t=150` has the
    window still 90 s in the future (regime 1, no `J` exists), `t=270` is 30 s
    inside it (regime 2, `J` is the realised half). A formula that got either
    regime wrong fails its checkpoint while passing the other.
    """
    rng = random.Random(20260917)
    dt, steps = 5.0, 60
    ck = {"regime1": 150.0, "regime2": 270.0}
    window_open = 240.0
    sd_step = SIG * math.sqrt(dt / 60.0)

    models: dict[str, list[float]] = {k: [] for k in ck}
    full_window: list[float] = []
    outcomes: list[bool] = []
    for _ in range(4000):
        x = 0.0
        int_s = 0.0          # integral of the PRICE over the window
        int_x_win = 0.0      # integral of the log-return over the window
        t = 0.0
        for i in range(steps):
            x_new = x + rng.gauss(0.0, sd_step)
            s0, s1 = math.exp(x), math.exp(x_new)
            if t >= window_open:                 # only the tail window counts
                int_s += 0.5 * (s0 + s1) * dt
                int_x_win += 0.5 * (x + x_new) * dt
            t += dt
            x = x_new
            if t in ck.values():
                name = [k for k, v in ck.items() if v == t][0]
                got = fair_value(S0 * math.exp(x), S0, T - t, SIG,
                                 window_log_int=(int_x_win
                                                 if t > window_open else None))
                models[name].append(got[0])
                if t == 150.0:
                    # the SAME rounds, priced as if the whole round settled
                    wrong = fair_value(S0 * math.exp(x), S0, T - t, SIG,
                                       window=FULL, round_seconds=T)
                    full_window.append(wrong[0])
        # the real rule: the window's time-average PRICE must reach S0
        outcomes.append((int_s / L) >= 1.0)

    assert len(outcomes) == 4000
    base_rate = sum(outcomes) / len(outcomes)
    worst_by_window = {}
    for name in ck:
        assert len(models[name]) == 4000
        mean_p = sum(models[name]) / len(models[name])
        assert mean_p == pytest.approx(base_rate, abs=0.03), name

        # calibration, decile by decile: the formula must know HOW SURE it is
        worst_by_window[name] = _worst_decile_gap(models[name], outcomes)
        assert worst_by_window[name] < 0.08, \
            f"{name}: worst decile miscalibration {worst_by_window[name]:.3f}"

    # THE TEST HAS POWER: the same rounds priced with the WRONG window miss by
    # far more, so 'it passed' is a statement about the window and not about
    # the tolerance. The two candidates are 5x apart in variance.
    worst_wrong = _worst_decile_gap(full_window, outcomes)
    assert worst_wrong > worst_by_window["regime1"] + 0.05, (
        f"the full-window price is not discriminated: {worst_wrong:.3f} vs "
        f"{worst_by_window['regime1']:.3f}")


def test_the_venue_module_imports_without_the_model_layer():
    """An import cycle that fires in only ONE order is worse than none.

    Importing `venues.polymarket.pricing` FIRST used to pull in
    `btcpredict.models`, whose package `__init__` imports the TWAP adapter back
    out of the venue module -- before the class existed. It failed only in that
    order, so every test starting from `btcpredict.models` passed and hid it.
    A fresh interpreter is the only honest way to pin the order; in-process
    everything is already in `sys.modules`.
    """
    import subprocess
    import sys
    r = subprocess.run(
        [sys.executable, "-c",
         "import btcpredict.venues.polymarket.pricing as p;"
         "assert p.terminal_rule() == 'tail';"
         "assert p.fair_value(100.0, 100.0, 300.0, 0.0008)[0] == 0.5"],
        capture_output=True, text=True, cwd=str(_REPO_ROOT))
    assert r.returncode == 0, r.stderr


def _worst_decile_gap(model_p: list[float], outcomes: list[bool]) -> float:
    order = sorted(range(len(model_p)), key=lambda i: model_p[i])
    per = len(order) // 10
    worst = 0.0
    for b in range(10):
        idx = order[b * per:(b + 1) * per]
        mp = sum(model_p[i] for i in idx) / len(idx)
        fr = sum(outcomes[i] for i in idx) / len(idx)
        worst = max(worst, abs(mp - fr))
    return worst

