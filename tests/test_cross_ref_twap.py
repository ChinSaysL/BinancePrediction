"""The crossing must be measured against the LEVEL the venue pays on.

The arm asks "which side did the spot just cross INTO" and used to answer with
`r.strike` -- the spot at the open INSTANT. Polymarket resolves these markets on
the `chainlink-<sym>-usd-twap-60s` stream (config
`{id: btc-5m-twap-60, twapLookbackSeconds: 60}`), comparing its value at the bell
against the same stream's value at the open, so BOTH ends of the payout are a
60 s AVERAGE and the level is the trailing 60 s mean at the boundary.

MEASURED on the live DB, 1,509 settled Polymarket-era rounds with a recorded
strike, family keyed on the slug prefix, one comparison each against the venue's
own winner:

    fin_TWAP >= TWAP60(open)    1420/1509 = 94.1%
    fin_TWAP >= `r.strike`      1344/1509 = 89.1%
    paired: 92 rounds where only the TWAP rule is right, 16 the other way
            McNemar z = +7.31 on 108 discordant pairs

and the offset it corrects is unbiased but +-3-4 bps wide -- more than 2 bps on
40% of rounds, against a depth floor of ONE TICK (0.0013 bps on BTC, 1.2 on
DOGE). Three live losses on 2026-09-17 sat in that cohort; the BTC one (fill
874, 19:04:31) crossed the STRIKE by +0.53 bps while the reference sat 1.02 bps
on the OTHER side of the spot, i.e. it bought a side that had not cleared the
level the round is paid on. It is not taken under this rule.

This does NOT re-open "the strike should be a 60 s TWAP": `_pm_strike_backfill`
rejected a window average AS THE STRIKE on 0.042-0.114 bps of evidence, and it
was right -- `r.strike` is the round's start price and stays that everywhere it
means that. The question here is only which level a CROSSING is read against.
"""
from __future__ import annotations

import asyncio
import pathlib
import tempfile
import time
from collections import deque
from types import SimpleNamespace

import pytest

from btcpredict import config as C
from btcpredict.config import Settings
from btcpredict.feeds.market_feed import Outcome, Round
from btcpredict.main import Bot, twap_reference
from btcpredict.risk.guards import RiskGuard
from btcpredict.store.db import Store

STRIKE = 77040.0


# --------------------------------------------------------------------------
# the pure helper
# --------------------------------------------------------------------------

def _window(start_ms, px, n=60):
    """1 Hz samples across the whole 60 s BEFORE the open."""
    return [(start_ms - (n - i) * 1000, px) for i in range(n)]


class TestTheReference:
    def test_it_is_the_MEAN_of_the_window_not_an_endpoint(self):
        start = int(time.time() * 1000)
        t = _window(start, 100.0, n=60)
        t[-1] = (t[-1][0], 200.0)          # only the last second moved
        ref = twap_reference(t, start)
        assert ref is not None
        assert 100.0 < ref < 102.0, (
            "a single print must not drag the reference -- that is the whole "
            "difference between a 60 s average and the spot at the open")

    def test_the_window_ends_AT_the_open(self):
        """A sample at or after the open belongs to the NEXT comparison."""
        start = int(time.time() * 1000)
        t = _window(start, 100.0) + [(start, 1_000.0), (start + 1000, 1_000.0)]
        assert twap_reference(t, start) == pytest.approx(100.0)

    def test_an_uncovered_window_has_no_reference(self):
        """The tape starts 10 s into the window: this is not a 60 s average."""
        start = int(time.time() * 1000)
        t = [(ts, 100.0) for ts, _ in _window(start, 100.0)][10:]
        assert twap_reference(t, start) is None

    def test_a_tape_that_stops_before_the_open_has_no_reference(self):
        start = int(time.time() * 1000)
        t = [(ts, 100.0) for ts, _ in _window(start, 100.0)][:40]
        assert twap_reference(t, start) is None

    def test_a_round_that_has_not_opened_has_no_reference(self):
        """The level is not a forecast: before the open there is nothing to
        average, and a number now would be a price from the wrong minute."""
        future = int(time.time() * 1000) + 120_000
        assert twap_reference(_window(future, 100.0), future) is None

    def test_too_few_samples_is_no_reference(self):
        start = int(time.time() * 1000)
        t = [(start - 60_000, 100.0), (start - 30_000, 100.0),
             (start - 1000, 100.0)]
        assert twap_reference(t, start) is None

    def test_a_non_positive_price_is_skipped_not_averaged(self):
        start = int(time.time() * 1000)
        t = _window(start, 100.0)
        t[10] = (t[10][0], 0.0)
        t[20] = (t[20][0], -5.0)
        assert twap_reference(t, start) == pytest.approx(100.0)

    def test_no_tape_is_no_reference(self):
        assert twap_reference([], int(time.time() * 1000)) is None
        assert twap_reference(None, int(time.time() * 1000)) is None


def test_the_fix_ships_ON_and_is_revertible():
    """ON by default: the two rules are not symmetric -- `r.strike` is the level
    the arm happened to use, this one is the level the venue pays. Revert with
    CROSS_REF_TWAP=false in `.env` + restart."""
    assert Settings.model_fields["cross_ref_twap"].default is True


# --------------------------------------------------------------------------
# the live path
# --------------------------------------------------------------------------

class FakeFeed:
    def __init__(self, t):
        self.ticks = deque(t)
        self.price = t[-1][1]
        self.ts_ms = t[-1][0]

    def is_fresh(self, max_age_s):
        return True


def make_round(tau_s=20.0):
    now = int(time.time() * 1000)
    return Round(
        slug="btc-updown-5m-1789000000", market_topic_id=1, market_id=2,
        condition_id="c", symbol="BTCUSDT", start_ms=now - 180_000,
        end_ms=now + int(tau_s * 1000), strike=STRIKE, end_price=None,
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


def make_bot(store, feed_ticks, ref_window):
    """`feed_ticks` drives the CROSSING; `ref_window` is `self._ticks`, the
    buffer the 60 s reference is averaged out of -- one per symbol, appended
    once per poll, exactly as `_round_range_bps` reads it."""
    bot = Bot.__new__(Bot)
    bot.mode = "paper"
    bot.store = store
    bot.book = None
    bot.broker = SimpleNamespace(mode="paper")
    bot.bankroll = 50.0
    bot.open_fills = {}
    feed = FakeFeed(feed_ticks)
    bot._primary_spot = feed
    bot.spot = feed
    bot.spots = {"BTCUSDT": feed}
    bot._round = make_round()
    bot._asks = (0.30, 0.70)
    bot._sigma = 0.0002
    bot._cross_fired = {}
    bot._cross_day = ""
    bot._cross_count = 0
    bot._order_cooldown_until = 0.0
    bot._ticks = {"BTCUSDT": ref_window}
    bot.guard = RiskGuard(daily_loss_cap=0.0, max_consecutive_losses=5,
                          max_open_rounds=1, daily_loss_cap_pct=0.20,
                          bankroll=lambda: 50.0)
    return bot


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr("btcpredict.config.settings.cross_enabled", True)
    monkeypatch.setattr("btcpredict.config.settings.cross_look_s", 2.0)
    monkeypatch.setattr("btcpredict.config.settings.cross_min_tau_s", 3.0)
    monkeypatch.setattr("btcpredict.config.settings.cross_max_tau_s", 30.0)
    monkeypatch.setattr("btcpredict.config.settings.cross_max_price", 0.50)
    monkeypatch.setattr("btcpredict.config.settings.cross_min_price", 0.02)


def _run(bot):
    calls = []

    async def place(*a, **k):
        calls.append(k)
        return None

    bot._place_order = place
    asyncio.run(bot._cross_tick())
    return calls


class TestTheArmFollowsTheReference:
    def test_a_cross_that_never_cleared_the_reference_is_NOT_taken(
            self, store, armed):
        """THE LIVE BUG. The 60 s mean before the open sat at 77050; the spot
        opened at 77040 and later ticked 77030 -> 77045. That clears the STRIKE
        by 5 ticks and is a 5-tick DEPTH on the old rule -- the fill-874 shape,
        where the round is paid against a level 1.02 bps the other way.

        Against the reference the spot is BELOW it on both sides of the move,
        so there is no crossing to take and the arm must do nothing."""
        now = int(time.time() * 1000)
        start = now - 180_000
        buf = _window(start, 77050.0) + [
            (start + 1000, 77030.0), (now - 2000, 77030.0), (now, 77045.0)]
        bot = make_bot(store, [(now - 3000, 77030.0), (now - 2000, 77030.0),
                               (now, 77045.0)], buf)
        assert bot._cross_level(bot._round) == (77050.0, "twap60")
        calls = _run(bot)
        assert not calls, (
            "fired on a crossing of the strike that never cleared the level "
            "the round is paid on")
        assert bot.store.counts()["decisions"] == 0

    def test_a_cross_the_STRIKE_alone_would_refuse_IS_taken(self, store, armed):
        """The mirror image, and the reason this is not a tightening. The mean
        before the open sat at 77030 -- below the strike -- and the spot ticked
        77020 -> 77035. That clears the reference but not the strike, so the
        old rule saw no crossing at all and the fix adds an entry the arm was
        blind to."""
        now = int(time.time() * 1000)
        start = now - 180_000
        buf = _window(start, 77030.0) + [
            (start + 1000, 77020.0), (now - 2000, 77020.0), (now, 77035.0)]
        bot = make_bot(store, [(now - 3000, 77020.0), (now - 2000, 77020.0),
                               (now, 77035.0)], buf)
        assert bot._cross_level(bot._round) == (77030.0, "twap60")
        calls = _run(bot)
        assert calls, "the reference crossing must reach the order path"
        row = list(bot.store.query("select side from decisions"))[0]
        assert row[0] == "Up"

    def test_the_old_rule_would_have_taken_the_first_one(self, store, armed,
                                                         monkeypatch):
        """A guard on the two tests above: with the knob OFF the same tape
        fires. Without this the pair would pass if the trigger had simply
        stopped working."""
        now = int(time.time() * 1000)
        start = now - 180_000
        buf = _window(start, 77050.0) + [
            (start + 1000, 77030.0), (now - 2000, 77030.0), (now, 77045.0)]
        bot = make_bot(store, [(now - 3000, 77030.0), (now - 2000, 77030.0),
                               (now, 77045.0)], buf)
        monkeypatch.setattr("btcpredict.config.settings.cross_ref_twap", False)
        calls = _run(bot)
        assert calls, "with the knob off the strike crossing must still fire"
        row = list(bot.store.query("select side from decisions"))[0]
        assert row[0] == "Up"

    def test_the_trigger_records_WHICH_level_it_used(self, store, armed):
        """A cohort split needs the source in the row. A silent fallback to the
        strike is the old behaviour wearing the new name."""
        now = int(time.time() * 1000)
        start = now - 180_000
        buf = _window(start, 77030.0) + [
            (start + 1000, 77020.0), (now - 2000, 77020.0), (now, 77035.0)]
        bot = make_bot(store, [(now - 3000, 77020.0), (now - 2000, 77020.0),
                               (now, 77035.0)], buf)
        _run(bot)
        row = list(bot.store.query("select reason from decisions"))[0]
        assert "ref=twap60" in row[0]
        # ...and the STRIKE is still recorded as the strike, so every existing
        # tool that reads `decisions.strike` keeps its meaning.
        row = list(bot.store.query("select strike from decisions"))[0]
        assert row[0] == pytest.approx(STRIKE)


class TestTheFallback:
    def test_no_reference_on_the_tape_falls_back_to_the_strike(self, store,
                                                               armed):
        """A process that booted after the open has no tape for the window. The
        arm must then behave EXACTLY as it did before this existed rather than
        refusing every round -- an unavailable measurement is not a refusal.

        NARROWED 2026-09-18, and this fixture is why it still reads this way:
        `make_bot` here has NO `session`, so the 1 s KLINE read cannot even be
        asked and this is the last resort. When the read CAN be asked the arm
        now waits for it (a 60 s mean off Binance's 1 s bars) and only reaches
        this fallback after 3 failed attempts, with a WARNING -- see
        `tests/test_cross_level_kline.py` and the `bnb-updown-5m-1789710600`
        measurement under `cross_ref_twap`."""
        now = int(time.time() * 1000)
        bot = make_bot(store, [(now - 3000, 77030.0), (now - 2000, 77030.0),
                               (now, 77045.0)], [])
        level, src = bot._cross_level(bot._round)
        assert (level, src) == (STRIKE, "strike")
        calls = _run(bot)
        assert calls, "the strike crossing must still fire on the fallback"
        row = list(bot.store.query("select side from decisions"))[0]
        assert row[0] == "Up"

    def test_a_pre_open_round_is_not_cached_as_the_strike(self, store, armed):
        """`_setup_polymarket` builds the CURRENT and the NEXT round. The next
        one has no reference yet, and caching `r.strike` for it would freeze
        today's behaviour onto the round the fix exists for."""
        bot = make_bot(store, [(0, 1.0)], [])
        future = int(time.time() * 1000) + 60_000
        r = make_round()
        r.start_ms = future
        assert bot._cross_level(r) == (STRIKE, "strike")
        assert not (getattr(bot, "_cross_ref", {}) or {}).get(r.slug), (
            "a pre-open round must not be memoised")

    def test_the_reference_is_memoised_per_round(self, store, armed):
        """It is read on the websocket clock; re-averaging a 40,000-tick buffer
        per family per tick is the cost this avoids."""
        now = int(time.time() * 1000)
        start = now - 180_000
        buf = _window(start, 77030.0) + [(start + 1000, 77020.0),
                                         (now, 77035.0)]
        bot = make_bot(store, [(now - 2000, 77020.0), (now, 77035.0)], buf)
        first = bot._cross_level(bot._round)
        bot._ticks = {}                      # the tape can vanish; the answer stands
        assert bot._cross_level(bot._round) == first
