"""The crossing can be read on the statistic the venue actually pays on.

THE HOLE THIS CLOSES. `cross_ref_twap` fixed WHICH LEVEL the crossing is
compared against (the 60 s mean at the open, not the spot at the open instant).
It did not touch the SIGNAL: `crossed_side` still fires on the point spot, and
inside the arm's own window (tau 5-60 s) the settlement window [bell-60, bell]
is 67-92% already printed -- so the spot that just crossed is the part that is
about to be AVERAGED AWAY.

    forecast(ts) = ((60 - tau) * mean(spot over [ts-60s, ts]) + tau * spot) / 60

MEASURED LIVE, `bnb-updown-5m-1789680600` 2026-09-17 21:34:41: the spot crossed
the reference by +1.63 bps at tau 19 s with the venue's own book at 0.01-0.04
for the preceding 44 s, we paid 0.26, and the MEAN never crossed -- the book was
right. That is the whole family: spot crosses, mean does not.

WHAT `tools/pm_twap_trigger_check.py` SAYS IT COSTS (PM era, first crossing, one
entry per round, tau 5-60):

    SPOT cross (shipped)   n=154  ask=0.468  win 51.3%  per-share +0.0382
    FC   cross (this)      n=105  ask=0.548  win 60.0%  per-share +0.0453

per-share better on both halves, days 2/2, and 32% fewer entries -- so the TOTAL
at a fixed stake is lower. It is shipped as a RISK change that removes a family
by construction, not as an edge claim. See config.cross_fc_trigger.
"""
from __future__ import annotations

import pathlib
import time
from types import SimpleNamespace

import pytest

from btcpredict.main import (Bot, TWAP_REF_LOOKBACK_S, TWAP_REF_MIN_N,
                             crossed_side, symbol_for_round, twap_forecast)

ROOT = pathlib.Path(__file__).resolve().parents[1]
LEVEL = 100.5
#: The level the "big enough move" test crosses: a 2.0 move carries the fc by
#: 2 x (tau/lookback) = 0.667 at tau 20, clear of the 0.505 gap; a 1.0 move
#: carries it by 0.344 and is left below.
LEVEL_FC = 100.505


def _tape(samples, newest_ms=None, step_ms=1000):
    """`samples` is oldest -> newest, one per `step_ms`, ending at now."""
    newest = int(time.time() * 1000) if newest_ms is None else newest_ms
    n = len(samples)
    return [(newest - (n - 1 - i) * step_ms, px)
            for i, px in enumerate(samples)]


def _bot(**knobs):
    b = Bot.__new__(Bot)          # __init__ is skipped by the fixtures too
    b._ticks = {}
    b._cross_fc_dbg = set()
    b._ck = lambda name, r=None: knobs.get(name, False)
    return b


# ---------------------------------------------------------------------------
# the pure helper
# ---------------------------------------------------------------------------

def test_no_tape_is_none():
    assert twap_forecast([], 20.0) is None
    assert twap_forecast(None, 20.0) is None
    assert twap_forecast([(1, 100.0), (2, 100.0)], 20.0) is None


def test_a_stale_tape_is_none():
    """No fresh sample -> no forecast, and the caller keeps the spot."""
    stale = _tape([100.0] * 120, newest_ms=int(time.time() * 1000) - 10_000)
    assert twap_forecast(stale, 20.0) is None


def test_before_the_settlement_window_has_started_the_value_is_the_spot():
    """tau > lookback: nothing is printed yet, so there is nothing to project."""
    tape = _tape([100.0] * 30 + [101.0] * 30)
    fc = twap_forecast(tape, TWAP_REF_LOOKBACK_S + 30.0)
    assert fc is not None
    by_ts = dict(tape)
    assert all(p == by_ts[ts] for ts, p in fc)
    # the first few ticks have no 60 s window behind them and are skipped
    assert len(fc) >= len(tape) - TWAP_REF_MIN_N


def test_the_window_is_the_PRINTED_PART_of_the_paid_window():
    """⛔ THE WINDOW BUG, PINNED. `TWAP60(bell)` averages `[bell-60, bell]`, so
    at `ts` the printed part of it is `[bell-60, ts]` -- a FIXED left edge. This
    function used to average `[ts-60s, ts]`: the whole trailing minute at every
    tau, so at tau=20 it averaged 20 seconds the paid window will never contain.

    The tape is built so the two disagree: the last 40 samples are 110, the 20
    before them are 90. Inclusive of both ends (the repo's convention at 1 Hz,
    the same one `twap_reference` uses) the paid window holds 41 samples:
        printed part of the paid window  (40x110 + 90)/41 = 109.51 -> fc = 109.67
        the whole trailing minute    (40x110 + 21x90)/61 = 103.11 -> fc = 105.41
    """
    tape = _tape([90.0] * 80 + [110.0] * 40)
    fc = twap_forecast(tape, 20.0)
    assert fc is not None
    m_paid = (40 * 110.0 + 90.0) / 41
    assert fc[-1][1] == pytest.approx((40 * m_paid + 20 * 110.0) / 60), (
        "the forecast averaged the trailing minute instead of the paid window")
    m_minute = (40 * 110.0 + 21 * 90.0) / 61
    wrong = (40 * m_minute + 20 * 110.0) / 60
    assert abs(fc[-1][1] - wrong) > 1.0, (
        "the two windows are 4 bps apart on this tape; a forecast that lands on "
        "the trailing-minute value is the bug this pins")


def test_a_tape_that_does_not_cover_the_printed_window_is_the_spot():
    """The coverage guard, the same one `twap_reference` applies. A tape that
    starts 38 s into the printed window would otherwise average 2 samples and
    call it a 40 s mean -- so the spot is the answer instead (at tau just under
    the boundary the weight on the mean is under 5%, which is also why this is
    the continuous choice)."""
    now = int(time.time() * 1000)
    tape = [(now - 2000, 100.0), (now, 101.0)]
    fc = twap_forecast(tape, 20.0, now_ms=now)
    assert fc is None or all(p == 101.0 or p == 100.0 for _, p in fc), (
        "a two-sample 'mean' was reported as the paid window's printed part")


def test_the_window_is_a_full_sixty_seconds():
    """At tau=0 the printed part IS the whole paid window, so this is the same
    number the old window gave -- and the reason the two agree at the bell and
    nowhere else."""
    tape = _tape([99.0] * 60 + [100.0] * 70)
    fc = twap_forecast(tape, 0.0)
    assert fc is not None
    assert fc[-1][1] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# the mechanism: this is what the loss looked like
# ---------------------------------------------------------------------------

def test_a_spot_spike_that_does_not_move_the_mean_is_not_a_crossing():
    """THE WHOLE POINT. bnb-updown-5m-1789680600, in one assertion.

    A ONE-SAMPLE spike on a 60 s mean at tau 20 s: the spot is through the level
    and the crossing is real (the tape 2 s ago was below it), the projected mean
    is not -- 40 of the 60 s are still to be printed, and they get printed at
    100.0. The spot trigger buys this; the forecast does not.
    """
    tape = _tape([100.0] * 299 + [101.0])
    assert crossed_side(list(tape), LEVEL, 1.0) == "Up"
    fc = twap_forecast(tape, 20.0)
    assert fc is not None
    assert fc[-1][1] < LEVEL, "the projected mean must not be through the level"
    assert crossed_side(list(fc), LEVEL, 1.0) is None


def test_a_move_big_enough_to_drag_the_projection_does_cross_the_mean():
    """Not a blanket refusal -- and the threshold is arithmetic, not taste.

    The projection weights the newest sample by tau/lookback (1/3 at tau 20),
    so a move of `d` carries the forecast `d/3` and must clear the whole gap to
    the level. One unit below (the test above) leaves it short of a 0.5 gap;
    two units carry it clear. That is why the family this removes is a small,
    fresh move near the level -- the same shape as the live BNB loss.
    """
    tape = _tape([100.0] * 299 + [102.0])
    fc = twap_forecast(tape, 20.0)
    assert fc is not None
    assert fc[-1][1] > LEVEL_FC
    assert fc[-2][1] < LEVEL_FC, "the tick it crossed against was below"
    assert crossed_side(list(fc), LEVEL_FC, 1.0) == "Up"


def test_one_sample_moves_the_forecast_less_than_it_moves_the_spot():
    """The smoothing, as a number: tau/lookback spot + the rest through the mean.

    The window is INCLUSIVE of both ends, so it holds lookback + 1 samples at
    1 Hz -- one sample carries 1/61 through the mean's weight and 1 whole
    through the spot's: 20/60 + (40/60)(1/61) = 0.3443 for a 1.0 move.
    """
    base = _tape([100.0] * 300)
    spiked = list(base)
    spiked[-1] = (spiked[-1][0], 101.0)
    fc_base = twap_forecast(base, 20.0)
    fc_spike = twap_forecast(spiked, 20.0)
    assert fc_base is not None and fc_spike is not None
    moved = fc_spike[-1][1] - fc_base[-1][1]
    # The float form is exact to the clock: `twap_forecast` reads `time.time()`
    # itself, so the tolerance covers the microseconds between the two calls
    # (0.0164 per second of drift), not any slack in the arithmetic.
    assert moved == pytest.approx(20 / 60 + (40 / 60) * (1 / 61), abs=1e-2)
    assert moved < 1.0, "a one-sample spike must not carry the trigger"


# ---------------------------------------------------------------------------
# the switch
# ---------------------------------------------------------------------------

def test_the_signal_falls_back_to_the_spot_on_a_partial_tape():
    """A tape that cannot describe the window -> the SPOT, logged, `sig=spot`.

    That is the SHIPPED judgement, and it is the same one `_cross_level` makes
    for the LEVEL: a boot inside the last 60 s leaves the arm in one coherent
    legacy mode -- old level, old signal -- rather than half of each. It is not
    silent: the trigger carries `sig=spot`, so the fallback cohort stays
    separable from `sig=fc` in `decisions` and the fire line.

    (A first version FAILED CLOSED here -- no forecast, no trade. It was backed
    out: refusing is unmeasured in money terms, its only live effect is the
    post-restart window, and it broke 10 fixtures that seed exactly this tape.)
    """
    r = SimpleNamespace(slug="btc-updown-5m-1789680600")
    tape = _tape([100.0] * 298 + [101.0, 101.0])

    off = _bot(fc_trigger=False)._cross_signal(r, tape, 20.0)
    assert off == (tape, "spot"), "knob off = the raw tape, unchanged"

    b = _bot(fc_trigger=True)
    b._ticks[symbol_for_round(r)] = tape
    got, name = b._cross_signal(r, tape, 20.0)
    assert name == "fc" and got is not tape

    boot = _bot(fc_trigger=True)         # a boot: nothing on the tape yet
    assert boot._cross_signal(r, tape, 20.0) == (tape, "spot")
    assert r.slug in boot._cross_fc_dbg, "and it must say so, once per round"

    stale = _bot(fc_trigger=True)        # a tape that stopped 10 s ago
    stale._ticks[symbol_for_round(r)] = _tape(
        [100.0] * 120, newest_ms=int(time.time() * 1000) - 10_000)
    assert stale._cross_signal(r, tape, 20.0) == (tape, "spot")


def test_the_fire_path_reads_the_signal_through_the_switch():
    """`crossed_side` must be handed the SWITCHED tape, not the feed's own.

    A run that computes the forecast and then crosses the raw spot tape passes
    every unit test above and changes nothing live -- which is the shape of
    four of this repo's five "fixes" that did not take.
    """
    src = (ROOT / "btcpredict" / "main.py").read_text()
    assert "sig_ticks, sig_name = self._cross_signal(r, ticks, tau)" in src
    assert "side = crossed_side(list(sig_ticks), level" in src


def test_the_knob_defaults_on_and_is_reversible_by_env():
    """ON by DEFAULT, not only in the VPS `.env`.

    Every profile under configs/ swaps the `.env`, and they do not all set this
    key: as an env-only default the protection was one `make config PROFILE=`
    away from silently disappearing.
    """
    from btcpredict.config import Settings
    assert Settings().cross_fc_trigger is True
    settings = (ROOT / "btcpredict" / "config.py").read_text()
    assert "cross_fc_trigger: bool = True" in settings
    assert "CROSS_FC_TRIGGER=false" in settings, "the revert must be stated"


def test_the_trigger_reads_the_signal_and_logs_it():
    """A switch nobody can audit from the record is not a switch."""
    src = (ROOT / "btcpredict" / "main.py").read_text()
    assert "sig_ticks, sig_name = self._cross_signal(r, ticks, tau)" in src
    assert "crossed_side(list(sig_ticks), level" in src
    assert "self._ck(\"fc_trigger\", r)" in src
    # the cohort split, in the reason and in the fire line
    assert 'f" sig={sig_name}"' in src
    assert '(%s %.2f vs %s %.2f, tau %.0fs)' in src
