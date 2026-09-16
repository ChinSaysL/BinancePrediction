"""D -- the excursion age the train arm's whole edge turns on -- is now measured
at the trigger instead of reconstructed afterwards.

The sweep this arm was built from prices its cells by delay: X>=15 hits 94.9% at
D=0 and 83.7% by D=5s, and `cross_train_fresh_s` is nothing but the D gate. But D
was never logged, so it had to be recovered from the `quotes` table -- and that
reconstruction is provably wrong: walking the live train fills under a 5.0s bound
gave median 2.1s but p90 8.3s and max 25.8s, which the gate cannot have admitted.

The cause is that the gate reads the in-memory TICK buffer while `quotes` is a
different, coarser series. These tests pin the new measurement, and pin the
invariant that makes it safe to log: passing the freshness gate implies the age
is inside the window, so a logged D can never contradict the gate it passed.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from btcpredict.main import Bot

STRIKE = 100.0
THR = 15.0                      # bps; +/-0.15% => 100.15 / 99.85


def _bot(ticks, symbol="BTCUSDT"):
    bot = Bot.__new__(Bot)      # skip network setup
    bot._ticks = {symbol: ticks}
    return bot


def _round(symbol="BTCUSDT"):
    return SimpleNamespace(strike=STRIKE, symbol=symbol)


def _ticks(*specs):
    """specs are (ms_ago, price) -> [(ts_ms, px)] as the feed stores them."""
    now = int(time.time() * 1000)
    return [(now - ago, px) for ago, px in specs]


def test_age_is_the_time_since_the_last_IN_BAND_sample():
    bot = _bot(_ticks((4000, 100.00), (900, 100.00), (800, 100.20), (0, 100.25)))
    age = bot._train_age_ms(_round(), THR)
    assert age is not None
    assert 860 <= age <= 1000, age


def test_THE_PRODUCTION_FAILURE_newest_sample_in_band_but_price_already_out():
    """REGRESSION: this returned None on EVERY live fire.

    `self._ticks` is appended once per poll (~1 Hz), so at a fire the newest
    SAMPLE is usually still inside the threshold -- that sample is exactly what
    makes `_train_is_fresh` pass -- while `feed.price` has already moved out.
    Anchoring on a run of above-threshold samples finds no run and returns None,
    which is what shipped: every TRAIN reason was written with no D at all.
    """
    # NO sample in the buffer is out-of-band: the poll simply has not caught up
    # with a price that `feed.price` already reports as far outside.
    bot = _bot(_ticks((2000, 100.05), (900, 99.98)))
    age = bot._train_age_ms(_round(), THR)
    assert age is not None, "must date the excursion, not bail"
    assert 860 <= age <= 1000, age


def test_a_buffer_with_no_in_band_sample_is_unknown_not_zero():
    """Fail closed, like the gate: nothing below the threshold to anchor on."""
    bot = _bot(_ticks((3000, 100.20), (2000, 100.25), (1000, 100.30), (0, 100.35)))
    assert bot._train_age_ms(_round(), THR) is None


def test_a_wick_back_inside_resets_the_anchor():
    """The anchor is the MOST RECENT in-band sample, not the oldest one."""
    bot = _bot(_ticks((5000, 100.25), (3000, 100.00),
                      (1000, 100.25), (0, 100.30)))
    age = bot._train_age_ms(_round(), THR)
    assert age is not None and 2800 <= age <= 3200, age


def test_a_sample_inside_the_threshold_right_now_reads_as_zero():
    bot = _bot(_ticks((2000, 100.05), (1000, 100.08), (0, 100.10)))
    age = bot._train_age_ms(_round(), THR)
    assert age is not None and age <= 50, age


def test_degenerate_inputs_return_none():
    assert _bot(_ticks((100, 100.20)))._train_age_ms(
        SimpleNamespace(strike=None, symbol="BTCUSDT"), THR) is None
    assert _bot(_ticks((100, 100.20)))._train_age_ms(_round(), 0.0) is None
    assert _bot([])._train_age_ms(_round(), THR) is None
    # wrong symbol -> empty buffer, not a stale age from another family
    assert _bot(_ticks((100, 100.20)),
                symbol="BTCUSDT")._train_age_ms(_round("ETHUSDT"), THR) is None


def test_a_logged_age_can_never_exceed_the_gate_that_passed():
    """THE INVARIANT THAT MAKES THIS SAFE TO LOG.

    `_train_is_fresh` is the window-edge test: it requires the OLDEST sample in
    the window to be in-band, which forces the anchor to sit inside the window.
    So whenever the gate passes, the age must be <= the window -- otherwise the
    logged D would appear to contradict a gate the trigger had already cleared,
    and the number would be worse than no number.
    """
    fresh_s = 1.0
    for crossing_ms_ago in (100, 400, 800, 950):
        ticks = _ticks((4000, 100.00), (crossing_ms_ago + 100, 100.00),
                       (crossing_ms_ago, 100.20), (0, 100.25))
        bot = _bot(ticks)
        r = _round()
        bot._ck = lambda k, _r=None: fresh_s if k == "train_fresh_s" else 0.0

        age = bot._train_age_ms(r, THR)
        if bot._train_is_fresh(r, THR):
            assert age is not None, "gate passed but age unknown"
            assert age <= fresh_s * 1000, (crossing_ms_ago, age)


def test_the_gate_refuses_an_excursion_older_than_the_window():
    """The converse, so the test above is not vacuous."""
    ticks = _ticks((6000, 100.00), (1500, 100.20), (0, 100.25))
    bot = _bot(ticks)
    bot._ck = lambda k, _r=None: 1.0 if k == "train_fresh_s" else 0.0
    assert bot._train_is_fresh(_round(), THR) is False
    age = bot._train_age_ms(_round(), THR)
    assert age is not None and age > 1000, age
