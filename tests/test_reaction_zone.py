"""The model arm's REACTION ZONE: the strike as a level the price bounces off.

`model_max_strike_crossings` refuses an entry once the spot has already crossed
the round's strike that many times. MEASURED 2026-09-22 (see the config comment
for the full table): the refused cohort ran -0.156/$1 against +0.126 for the kept
one, and `off vs <=3` repeats in BOTH halves.

THE RESAMPLE IS THE WHOLE BALLGAME, and that is what most of this file pins.
`self._ticks` holds one sample per poll and the live poll runs at a median 58 ms,
so `spot > strike` flips on jitter many times per real crossing: the same round
that scores 3 crossings on the 1 Hz `quotes` tape the value was fitted on scores
dozens on the poll grid, and the gate would then refuse nearly every round.
Counting on a 1 Hz RESAMPLE is the only reason the fitted number transfers.

The two look-alike rules that are NOT this one are recorded in the config so they
do not get re-proposed: `min_moneyness_sd` (the DISTANCE form, which FLIPS
between halves) and an unresampled count (a property of the poll cadence).
"""
from __future__ import annotations

import pathlib
import tempfile
import time

import pytest

from btcpredict.config import settings
from btcpredict.feeds.market_feed import Outcome, Round
from btcpredict.main import Bot
from btcpredict.store.db import Store

STRIKE = 77000.0


def make_round(start_ms=None, strike=STRIKE):
    now = (start_ms if start_ms is not None
           else int(time.time() * 1000) - 180_000)
    return Round(
        slug="btc-updown-5m-1789000000", market_topic_id=1, market_id=2,
        condition_id="c", symbol="BTCUSDT", start_ms=now,
        end_ms=now + 300_000, strike=strike, end_price=None,
        price_feed_id=None, feed_provider=None, fee_rate_bps=200,
        liquidity=5000.0, trade_volume=1000.0, status="OPEN",
        trading_status="OPEN", live_status="OPEN",
        outcomes=[Outcome(name="Up", price=0.30, chance=0.30, token_id="1",
                          market_outcome_id=11, winner=None),
                  Outcome(name="Down", price=0.70, chance=0.70, token_id="2",
                          market_outcome_id=12, winner=None)])


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = Store(pathlib.Path(d) / "t.db")
        yield s
        s.close()


def make_bot(store, r=None):
    bot = Bot.__new__(Bot)
    bot.mode = "paper"
    bot.store = store
    bot.book = None
    bot._ticks = {}
    bot._round = r or make_round()
    return bot


class TestTheCount:
    def test_a_one_way_run_never_crosses(self, store):
        r = make_round()
        bot = make_bot(store, r)
        bot._ticks["BTCUSDT"] = [(r.start_ms + s * 1000, STRIKE + 5.0 + s)
                                 for s in range(6)]
        assert bot._strike_crossings(r) == 0

    def test_each_flip_of_side_is_one_crossing(self, store):
        r = make_round()
        bot = make_bot(store, r)
        side = [+1, -1, +1, -1, +1]          # 4 flips
        bot._ticks["BTCUSDT"] = [(r.start_ms + s * 1000, STRIKE + 5.0 * d)
                                 for s, d in enumerate(side)]
        assert bot._strike_crossings(r) == 4

    def test_the_count_ignores_which_side_it_started_on(self, store):
        """Only the transitions matter -- sign of the level is not the signal."""
        r = make_round()
        bot = make_bot(store, r)
        bot._ticks["BTCUSDT"] = [(r.start_ms + s * 1000, STRIKE - 5.0 * d)
                                 for s, d in enumerate([+1, -1, +1, -1, +1])]
        assert bot._strike_crossings(r) == 4

    def test_the_count_is_taken_on_a_one_hz_resample_not_the_poll_grid(
            self, store):
        """⛔ THE POINT OF THE WHOLE GATE.

        ############################################################
        # 20 polls per second, flipping 10 times a second, for 5 seconds.
        # The RAW poll-grid count is ~90 crossings. The last poll of each
        # second alternates Up, Down, Up, Down, Up -- so a 1 Hz read of the
        # SAME buffer sees 4. Shipping the raw count would refuse every round
        # the arm has ever traded; the fitted value is 3, i.e. `>= 3` refuses.
        ############################################################
        """
        r = make_round()
        bot = make_bot(store, r)
        seq = []
        for sec in range(5):
            last_up = (sec % 2 == 0)
            for k in range(20):
                if k == 19:
                    px = STRIKE + (1.0 if last_up else -1.0)
                else:
                    px = STRIKE + (1.0 if k % 2 == 0 else -1.0)
                seq.append((r.start_ms + sec * 1000 + k * 50, px))
        bot._ticks["BTCUSDT"] = seq

        raw, prev = 0, None
        for _ms, px in seq:
            now_up = px > STRIKE
            if prev is not None and now_up != prev:
                raw += 1
            prev = now_up
        assert raw >= 80, raw

        assert bot._strike_crossings(r) == 4

    def test_the_resample_is_last_sample_per_second(self, store):
        """A 1 Hz read must see where the second ENDED, not where it started."""
        r = make_round()
        bot = make_bot(store, r)
        # second 0 ends Up, second 1 ends Down, second 2 ends Up -> 2 crossings
        bot._ticks["BTCUSDT"] = [
            (r.start_ms + 0, STRIKE - 9.0), (r.start_ms + 990, STRIKE + 9.0),
            (r.start_ms + 1000, STRIKE + 9.0), (r.start_ms + 1990, STRIKE - 9.0),
            (r.start_ms + 2000, STRIKE - 9.0), (r.start_ms + 2990, STRIKE + 9.0)]
        assert bot._strike_crossings(r) == 2

    def test_ticks_before_the_round_are_ignored(self, store):
        r = make_round()
        bot = make_bot(store, r)
        bot._ticks["BTCUSDT"] = [
            (r.start_ms - 60_000, STRIKE - 9.0),     # previous round
            (r.start_ms - 59_000, STRIKE + 9.0),
            (r.start_ms + 1000, STRIKE + 9.0),
            (r.start_ms + 2000, STRIKE + 9.0),
            (r.start_ms + 3000, STRIKE + 9.0)]
        assert bot._strike_crossings(r) == 0

    def test_a_bad_price_is_skipped(self, store):
        r = make_round()
        bot = make_bot(store, r)
        bot._ticks["BTCUSDT"] = [
            (r.start_ms + 1000, 0.0), (r.start_ms + 2000, None),
            (r.start_ms + 3000, STRIKE + 9.0), (r.start_ms + 4000, STRIKE + 9.0),
            (r.start_ms + 5000, STRIKE + 9.0)]
        assert bot._strike_crossings(r) == 0

    def test_too_little_history_is_no_opinion_not_a_refusal(self, store):
        """None must mean 'cannot judge' -- a cold start must not disable an arm."""
        r = make_round()
        bot = make_bot(store, r)
        bot._ticks = {}
        assert bot._strike_crossings(r) is None
        bot._ticks["BTCUSDT"] = [(r.start_ms + 1000, STRIKE),
                                 (r.start_ms + 2000, STRIKE)]
        assert bot._strike_crossings(r) is None

    @pytest.mark.parametrize("strike", [None, 0.0, -1.0])
    def test_an_impossible_strike_is_no_opinion(self, store, strike):
        r = make_round(strike=strike)
        bot = make_bot(store, r)
        bot._ticks["BTCUSDT"] = [(r.start_ms + s * 1000, STRIKE)
                                 for s in range(6)]
        assert bot._strike_crossings(r) is None


class TestTheShippedValue:
    def test_the_default_is_the_measured_crossing_count(self):
        assert settings.model_max_strike_crossings == 3

    def test_the_gate_is_an_elif_in_the_model_chain(self):
        """A bare `if` would let the risk guard be bypassed on every refused
        tick -- the bug that once ran the daily loss cap to -$5 instead of -$3."""
        src = pathlib.Path("btcpredict/main.py").read_text()
        assert "elif (settings.model_max_strike_crossings > 0" in src
        assert "self._strike_crossings(r)" in src

    def test_one_hz_is_the_grid_the_value_was_fitted_on(self):
        """Raising the resample would silently make the gate refuse far more."""
        import inspect
        sig = inspect.signature(Bot._strike_crossings)
        assert sig.parameters["resample_ms"].default == 1000

    def test_the_measured_table_sits_next_to_the_value(self):
        src = pathlib.Path("btcpredict/config.py").read_text()
        i = src.index("model_max_strike_crossings: int =")
        block = src[max(0, i - 5000):i]
        for token in ("+0.1015", "+0.0732", "0-2", "3-4", "REPEATS",
                      "+0.1264", "-0.1556"):
            assert token in block, token
        # and the two refuted look-alikes must stay recorded
        assert "min_moneyness_sd" in block
