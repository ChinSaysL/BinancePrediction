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


def test_age_is_measured_from_the_start_of_the_current_run():
    bot = _bot(_ticks((4000, 100.00), (900, 100.00), (800, 100.20), (0, 100.25)))
    age = bot._train_age_ms(_round(), THR)
    assert age is not None
    assert 780 <= age <= 900, age


def test_a_run_that_reaches_the_buffer_start_is_unknown_not_zero():
    """Fail closed, like the gate. A truncated buffer cannot date the crossing."""
    bot = _bot(_ticks((3000, 100.20), (2000, 100.25), (1000, 100.30), (0, 100.35)))
    assert bot._train_age_ms(_round(), THR) is None


def test_a_wick_below_restarts_the_run():
    """The age is the CURRENT excursion, not the oldest time we were far away."""
    bot = _bot(_ticks((5000, 100.20), (3000, 100.00),
                      (1000, 100.20), (0, 100.25)))
    age = bot._train_age_ms(_round(), THR)
    assert age is not None and 950 <= age <= 1100, age


def test_inside_the_threshold_is_not_a_run():
    bot = _bot(_ticks((2000, 100.05), (1000, 100.08), (0, 100.10)))
    assert bot._train_age_ms(_round(), THR) is None


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

    `_train_is_fresh` is the stricter, window-edge test: it requires the OLDEST
    sample in the window to be below the threshold, which forces the crossing to
    sit inside the window. So whenever the gate passes, the age must be <= the
    window -- otherwise the logged D would appear to contradict a gate the
    trigger had already cleared, and the number would be worse than no number.
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
