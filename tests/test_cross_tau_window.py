"""The arm's entry CLOCK: 5-180 s, shipped 2026-09-18, and why not 5-60.

THE QUESTION THIS CLOSES. `pm_param_sweep.py` measured tau [60,120] at +$11.18
against +$0.39 for the shipped [5,60] on the same 2,246 rounds. A 28x difference
on one knob is not a number to leave lying around, but it was NOT shipped,
because (a) its day-clustered t was negative and (b) past tau=60 the projection
`((60-tau)*mean + tau*spot)/60` carries a NEGATIVE weight on the mean -- at
tau=120 it is `2*spot - mean`, an extrapolation of a quantity the venue computes
as a plain average. So the +$11.18 looked like momentum wearing the venue's
rule's clothes.

WHAT KILLED THE SUSPICION. `tools/pm_tau_window.py` re-ran the sweep with both
signals -- `fc` (the shipped projection) and `fc60` (`fc` while tau<=60, the SPOT
past that) -- on 2,282 rounds, one entry per round, floor 0.25 bps, taker at the
recorded ask. They differ ONLY in the tau>60 value, and they agree to 0.0012 per
share on EVERY window, i.e. the formula is worth nothing either way: the wide
window's edge is in the ENTRIES, not in the extrapolation.

WHAT DECIDED IT (PASS = pooled t>=2 AND day-clustered t>=2 AND both halves
positive AND drop-best-day positive):

    window     n   win   per-share  total   t     day-t  halves      drop   verdict
    [ 5, 60]  69 56.5%   +0.0612   +4.22  +1.01  +1.51  -0.017/+0.137 +0.056 FAIL
    [ 5,180] 574 61.3%   +0.0710  +40.76  +3.42  +2.82  +0.047/+0.095 +0.065 PASS
    [ 5,300] 1194 57.6%  +0.0376  +44.92  +2.62  +3.10  +0.024/+0.051 +0.035 PASS

Every upper bound >= 180 passes and the SHIPPED window fails its own repo's
battery -- its first half is NEGATIVE. That is the direction the evidence
repeats, which is what ships; the exact optimum is not quotable.
"""
from __future__ import annotations

import pathlib
import time

import pytest

from btcpredict.config import MARKETS, Settings, cross_knob
from btcpredict.main import TWAP_REF_LOOKBACK_S, twap_forecast

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _tape(prices, step_ms=1000, newest_ms=None):
    """Chronological (ts_ms, price); `newest_ms` defaults to the wall clock."""
    newest_ms = int(time.time() * 1000) if newest_ms is None else int(newest_ms)
    n = len(prices)
    return [(newest_ms - (n - 1 - i) * step_ms, float(p))
            for i, p in enumerate(prices)]


# ---------------------------------------------------------------------------
# the shipped number
# ---------------------------------------------------------------------------

def test_the_window_ships_the_direction_that_survives_both_halves():
    """⭐ 180 -> 240 on 2026-09-19, on the first sweep of this window that was
    both config-independent and scored OUT OF SAMPLE. Inside [0.45,0.65),
    floor 0.50, ceiling off, charged the live-measured ladder: tau<=240 makes
    +24.41 (t+1.68) against 180's +19.00 (t+1.68) on the full tape, and
    +14.34 against +10.00 on days the choice never saw -- better at look_s 1.0
    AND at the 2.0 the arm does not trade. 300 is worse than 240 at both, so
    it is an interior optimum.  See config.cross_max_tau_s."""
    assert Settings().cross_max_tau_s == 240.0


def test_only_the_upper_bound_moved():
    """⭐ SUPERSEDED FOR THE FLOOR (2026-09-18): it is 30.0, not 5.0.

    The original reasoning -- "the floor is an EXECUTION bound (median
    decision->fill 3.7 s), not a signal one, and there is no measured reason to
    touch it" -- was right about the TAPE and wrong about the arm. The replay
    takes the FIRST qualifying crossing per round, so only 38 of 806 of its
    entries are at tau<60 and a tau floor cannot bind there. The LIVE arm puts
    18% of its fills under tau 60, and under tau 30 they run 1 win in 7 at
    -0.1130/share. Below 30 s more than HALF the settling window is already
    printed and unchangeable, which is arithmetic, not a fitted cell.
    See config.cross_min_tau_s. The UPPER bound moved to 240 on 09-19."""
    assert Settings().cross_min_tau_s == 30.0
    assert Settings().cross_max_tau_s == 240.0


def test_the_width_is_global_no_family_overrides_it():
    """A per-family `max_tau_s` in `cross_overrides` would leave that family on
    the old clock while the record said 180, and nothing would log it."""
    for prefix, _sym, _iv in MARKETS:
        ov = Settings().cross_overrides.get(prefix) or {}
        assert "max_tau_s" not in ov, prefix
        assert cross_knob(prefix, "max_tau_s") == 240.0, prefix


# ---------------------------------------------------------------------------
# the signal the wide window is measured on is the signal the arm computes
# ---------------------------------------------------------------------------

def test_past_the_lookback_the_forecast_is_the_spot_not_an_extrapolation():
    """THE property that makes widening safe. `tools/pm_tau_window.py` measured
    [5,180] on `fc60` -- `fc` while tau<=60, the spot past that. The arm must
    compute that same object, or the shipped window is not the measured one.

    A RISING tape is the load-bearing part: the extrapolation `2*spot - mean`
    differs from the spot there, so this test fails if that branch ever reverts
    to the formula."""
    tape = _tape([100.0] * 70 + [101.0] * 5)
    by_ts = dict(tape)

    for tau_now in (TWAP_REF_LOOKBACK_S, 120.0, 180.0):
        fc = twap_forecast(tape, tau_now)
        assert fc is not None
        assert all(p == by_ts[ts] for ts, p in fc), tau_now

    # ...and the check has teeth: inside the window the two differ.
    inside = twap_forecast(tape, 20.0)
    assert inside is not None
    assert any(p != by_ts[ts] for ts, p in inside)


def test_the_arm_reads_the_bound_from_the_cross_knob():
    """So the window is per-family overridable and the refusal is real -- a
    round outside it returns before any venue call."""
    src = (ROOT / "btcpredict" / "main.py").read_text()
    i = src.index('if not train and not (self._ck("min_tau_s", r) <= tau')
    assert '<= self._ck("max_tau_s", r)):' in src[i:i + 200]
    assert "return" in src[i:i + 300]


# ---------------------------------------------------------------------------
# the record in config.py -- the numbers, not a summary of them
# ---------------------------------------------------------------------------

def test_config_quotes_the_measurement_that_decided_the_window():
    src = (ROOT / "btcpredict" / "config.py").read_text()
    # the refuted hypothesis, in the words that refute it
    assert "REFUTED BY DIRECT MEASUREMENT" in src
    assert "READ IT AS A DIRECTION, NOT AN OPTIMUM" in src
    # both the failing row and the passing row
    assert "+4.22" in src and "+0.0612" in src
    assert "+0.0710" in src and "+40.76" in src
    assert "+0.017" in src          # the shipped window's NEGATIVE first half
    # ...and the SAME sweep re-run at the look the arm actually runs, because the
    # table above was produced at the tools' stale 2.0 default
    assert "RE-RUN AT THE SHIPPED LOOK" in src
    assert "+0.0744" in src and "+41.38" in src
    assert "THE VERDICTS REPLICATE EXACTLY" in src
    # the cost is stated with the change, not discovered later
    assert "8x" in src
    assert "TURNOVER" in src
    # and the way back
    assert "CROSS_MAX_TAU_S=60.0" in src


# ---------------------------------------------------------------------------
# the sweep must measure the ARM'S trigger, not a look of its own choosing
# ---------------------------------------------------------------------------

def test_the_sweeps_default_to_the_look_the_arm_runs():
    """A `--look-s 2.0` literal sat in four PM tools while the arm shipped
    `cross_look_s = 1.0`, so the floor AND the tau window were both ranked on a
    crossing rule the bot does not run -- `crossed_side` compares a different
    pair of samples at 1 s than at 2 s. The default is now resolved from config,
    and this test is what keeps it that way."""
    assert Settings().cross_look_s == 1.0

    engine = (ROOT / "tools" / "pm_cross_replay.py").read_text()
    assert "from btcpredict.config import settings" in engine
    assert "return float(settings.cross_look_s)" in engine

    for name in ("pm_cross_replay", "pm_tau_window", "pm_param_sweep",
                 "pm_family_signal_check"):
        src = (ROOT / "tools" / f"{name}.py").read_text()
        assert '"--look-s", type=float, default=2.0' not in src, name
        assert '"--look-s", type=float, default=None' in src, name
        assert "args.look_s = shipped_look_s()" in src, name
        if name != "pm_cross_replay":
            assert "shipped_look_s" in src.split("def main(")[0], name

