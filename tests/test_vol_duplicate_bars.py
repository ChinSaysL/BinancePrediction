"""The vol refresh re-fetches the same bars every minute -- it must not re-add them.

MEASURED 2026-09-19, live. `refresh_vol_if_stale()` polls `fetch_klines("1m", 5)`
every 60 s and ingests `klines[:-1]` = the last FOUR completed bars. Three of them
arrive again on the next call, so each bar was fed ~4 times. Every duplicate is
another EWMA update carrying the newest bar's return, which shortens the filter's
effective time constant (`lam^n` -> `lam^(4n)`) and over-weights the newest bars
in the simple mean.

The consequence was not cosmetic: sigma_1m ran 1.65-1.9x both the out-of-sample
estimator and the vol implied by the venue's own quote (6.6-7.6 bps/min against
4.0-4.3), which pushes fair = Phi(ln(S/K)/(sigma*sqrt(tau))) toward 0.50. Every
quote inside the arm's [0.45,0.80] band then looks overpriced, the only "edge"
appears on the cheap side the band refuses, and the model arm sent NOTHING for
five days while its gates were measured and correct.
"""
from __future__ import annotations

import math

import pytest

from btcpredict.pricing.vol import VolEstimator


MIN = 60_000
#: A realistic epoch-ms bar open. NOT 0: a falsy timestamp means "unknown" to
#: the guard (it cannot be ordered), so a zero-based fixture would silently skip
#: the dedupe and test nothing.
BASE = 1_800_000_000_000


def _bar(i: int, o: float, c: float) -> list:
    """A Binance 1m kline row: [open_time, open, high, low, close, ...]."""
    open_ms = BASE + i * MIN
    return [open_ms, str(o), str(max(o, c)), str(min(o, c)), str(c),
            "1", open_ms + 59_999, "1", 1, "0.5", "1", "0"]


class TestTheSameBarIsNeverCountedTwice:

    def test_a_repeated_bar_does_not_move_sigma(self):
        v = VolEstimator(lookback=240)
        bars = [_bar(i, 100.0 + i, 100.0 + i + 0.5) for i in range(4)]
        for b in bars:
            assert v.add_bar(int(b[0]), math.log(float(b[4]) / float(b[1])))
        once = v.sigma_1m()
        n_once = v.n
        # The next poll returns the SAME four bars (three of them new to nobody).
        for _ in range(10):
            for b in bars:
                assert not v.add_bar(int(b[0]), math.log(float(b[4]) / float(b[1])))
        assert v.sigma_1m() == pytest.approx(once)
        assert v.n == n_once, "a duplicate bar must not grow the sample"

    def test_a_newer_bar_is_ingested(self):
        v = VolEstimator(lookback=240)
        v.add_bar(BASE, 0.0)
        assert v.add_bar(BASE + MIN, 0.001) is True
        assert v.n == 2
        assert v.add_bar(BASE + MIN, 0.001) is False
        assert v.add_bar(BASE + 2 * MIN, -0.002) is True
        assert v.n == 3

    def test_a_bar_older_than_the_newest_is_refused(self):
        v = VolEstimator(lookback=240)
        v.add_bar(BASE + 10 * MIN, 0.001)
        assert v.add_bar(BASE + 9 * MIN, 0.001) is False, "out-of-order re-feed"
        assert v.n == 1

    def test_the_ewma_keeps_the_intended_time_constant(self):
        """Feeding one volatile bar once must NOT be worth four times its weight.

        With lam=0.97 a single shock moves the variance by 3%, not by 11%.
        """
        v = VolEstimator(lookback=240, seasonality=False)
        for i in range(100):
            v.add_bar(BASE + i * MIN, 0.0001)
        quiet = v.sigma_1m()
        shock = 0.01                      # a violent minute
        v.add_bar(BASE + 100 * MIN, shock)
        one = v.sigma_1m()
        for _ in range(3):                # the same bar arriving again
            v._last_ms = 0                # force past the guard, as the old path did
            v.add_bar(BASE + 100 * MIN, shock)
        four = v.sigma_1m()
        assert four > one, "the duplicate feed is what inflated sigma"
        # The correct feed must sit far below the old four-times behaviour.
        assert one < 0.5 * (four + quiet)

    def test_seeding_dedupes_within_the_file(self):
        v = VolEstimator(lookback=240)
        bars = [_bar(i, 100.0, 100.1) for i in range(5)]
        v.seed_from_klines(bars + bars)   # a paged/overlapping fetch
        assert v.n == len(bars)
