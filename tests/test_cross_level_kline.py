"""After a RESTART the level comes from the 1 s KLINES, never from `r.strike`.

THE TRADE THIS IS FOR. `bnb-updown-5m-1789710600`, 2026-09-18:

    05:49:35  deploy restarts the process
    05:50:00  a new round opens -- 25 s later, so `self._ticks` (appended once
              per poll) has NO sample from the round's reference minute
              [05:49:00, 05:50:00) and can never get one
    05:50:03  `cross ...: no 60s reference on the tape, falling back to the
              strike 752.095000`
    05:52:08  `CROSS ... Up sweep_px=0.230 (fc 752.13 vs strike 752.10, tau
              171s)` -- +0.47 bps, which CLEARS the 0.25 bps floor
    05:52:09  pm FAK FILL 4.54545 Up @ 0.22

    the LEVEL that round (60 s mean at the open, from the tape) = 752.5447
        -> +5.98 bps ABOVE the strike, because BNB dropped ~6 bps INTO the
           open: the point sat under the mean of the minute before it
        -> the same forecast read against it is -5.51 bps: THE FLOOR REFUSES
    the market agreed from the first tick: 0.26/0.76 at the open, 0.22/0.80 at
    the entry, 0.01/1.00 at tau 43. Settled Down. -$1.05.

So the fallback the old code documented as "never a new risk, exactly what
shipped before" swaps a 60 s MEAN for a POINT, and the two differ by more than
the arm's whole floor on ~40% of rounds. Binance's 1 s klines answer the same
question retrospectively, which is the fix; `r.strike` survives only where no
kline answer can arrive at all, and that case is a WARNING.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
import tempfile
import time
from collections import deque
from types import SimpleNamespace

import pytest

from btcpredict.feeds.market_feed import Outcome, Round
from btcpredict.main import (Bot, CROSS_LEVEL_KLINE_TRIES, TWAP_REF_LOOKBACK_S)
from btcpredict.risk.guards import RiskGuard
from btcpredict.store.db import Store

STRIKE = 77040.0
SLUG = "btc-updown-5m-1789000000"


# --------------------------------------------------------------------------
# the window
# --------------------------------------------------------------------------

def _bars(start_ms, px=77000.0, n=60, after=2):
    """1 s bars at `openTime` for the window [start - n, start), plus `after`
    bars at and past the open -- which must NOT be averaged in."""
    out = [[start_ms - (n - i) * 1000, 0.0, 0.0, 0.0, px] for i in range(n)]
    out += [[start_ms + i * 1000, 0.0, 0.0, 0.0, px * 9] for i in range(after)]
    return out


@pytest.fixture
def bars(monkeypatch):
    """Captures the kline call and answers with whatever the test installs."""
    seen = {}

    def install(payload, raises=None):
        async def fake(session, interval, limit, symbol="BTCUSDT", end_ms=None):
            seen.update(interval=interval, limit=limit, symbol=symbol,
                        end_ms=end_ms)
            if raises is not None:
                raise raises
            return payload
        monkeypatch.setattr("btcpredict.main.fetch_klines", fake)
        return seen
    return install


def _bot():
    bot = Bot.__new__(Bot)
    bot.session = object()
    return bot


def _slug(start_ms):
    """A real family slug: `_fetch_cross_level_kline` refuses a symbol it
    cannot map to Binance before it fetches anything, so a made-up slug would
    make every test below pass for the wrong reason."""
    return f"btc-updown-5m-{start_ms // 1000}"


def _warned(caplog, needle):
    return any(needle in r.getMessage() for r in caplog.records)


class TestTheWindow:
    def test_it_is_the_mean_of_the_sixty_seconds_BEFORE_the_open(self, bars):
        start = int(time.time() * 1000) - 200_000
        seen = bars(_bars(start, 77000.0))
        level = asyncio.run(_bot()._fetch_cross_level_kline(_slug(start), start))
        assert level == pytest.approx(77000.0)
        assert seen["interval"] == "1s"
        assert seen["end_ms"] == start, "the window ends AT the open"
        assert seen["limit"] >= int(TWAP_REF_LOOKBACK_S) + 1, (
            "60 bars are needed and the bar AT the open comes back too")

    def test_the_bar_at_the_open_is_NOT_in_the_average(self, bars):
        """`openTime == start` covers the second AFTER the open: it belongs to
        the bell-side comparison, not this one. `_bars` prices those bars 9x."""
        start = int(time.time() * 1000) - 200_000
        bars(_bars(start, 77000.0, after=3))
        level = asyncio.run(_bot()._fetch_cross_level_kline(_slug(start), start))
        assert level == pytest.approx(77000.0), (
            "a post-open bar leaked into the level -- that is look-ahead")

    def test_a_bar_before_the_window_is_not_in_the_average(self, bars):
        start = int(time.time() * 1000) - 200_000
        payload = [[start - 70_000, 0, 0, 0, 1.0]] + _bars(start, 77000.0)
        bars(payload)
        assert asyncio.run(_bot()._fetch_cross_level_kline(_slug(start), start)) \
            == pytest.approx(77000.0)

    def test_an_INCOMPLETE_window_has_no_level(self, bars):
        """59 of 60 bars is not a 60 s mean, and this number is compared
        against a 0.25 bps floor -- so it fails closed rather than scaling up."""
        start = int(time.time() * 1000) - 200_000
        bars(_bars(start, 77000.0, n=59))
        assert asyncio.run(
            _bot()._fetch_cross_level_kline(_slug(start), start)) is None

    def test_a_duplicated_bar_counts_ONCE(self, bars):
        """A mean that counts one second twice is a weighted mean, and the
        venue does not weight. The duplicated second here carries an outlier
        close, so counting it twice moves the answer by 17 points."""
        start = int(time.time() * 1000) - 200_000
        payload = _bars(start, 77000.0)
        payload[0][4] = 76000.0          # ONE second away from the other 59
        payload.append(list(payload[0]))  # ...returned twice
        bars(payload)
        level = asyncio.run(_bot()._fetch_cross_level_kline(_slug(start), start))
        assert level == pytest.approx((59 * 77000.0 + 76000.0) / 60.0), (
            "the duplicate was weighted: 60 seconds, one sample each")

    def test_a_duplicated_bar_cannot_fake_a_COMPLETE_window(self, bars):
        """The window is counted in seconds, not in bars returned: 59 distinct
        seconds with one of them repeated is a 59 s window, and a mean missing
        a second is the thing this method refuses."""
        start = int(time.time() * 1000) - 200_000
        payload = _bars(start, 77000.0, n=59)
        payload.append(list(payload[0]))
        bars(payload)
        assert asyncio.run(
            _bot()._fetch_cross_level_kline(_slug(start), start)) is None

    def test_a_pre_open_round_has_no_level(self, bars):
        """The level is not a forecast: before the open there is nothing to
        average, and a number now would be a price from the wrong minute."""
        future = int(time.time() * 1000) + 120_000
        bars(_bars(future, 77000.0))
        assert asyncio.run(
            _bot()._fetch_cross_level_kline(_slug(future), future)) is None

    def test_a_non_positive_close_is_not_averaged(self, bars):
        start = int(time.time() * 1000) - 200_000
        payload = _bars(start, 77000.0, n=60)
        payload[7][4] = 0.0
        bars(payload)
        assert asyncio.run(
            _bot()._fetch_cross_level_kline(_slug(start), start)) is None, (
                "a zero close must not enter the average (or the window is short)")


# --------------------------------------------------------------------------
# the arm
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
        slug=SLUG, market_topic_id=1, market_id=2, condition_id="c",
        symbol="BTCUSDT", start_ms=now - 180_000, end_ms=now + int(tau_s * 1000),
        strike=STRIKE, end_price=None, price_feed_id=None, feed_provider=None,
        fee_rate_bps=200, liquidity=5000.0, trade_volume=1000.0, status="OPEN",
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


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr("btcpredict.config.settings.cross_enabled", True)
    monkeypatch.setattr("btcpredict.config.settings.cross_look_s", 2.0)
    monkeypatch.setattr("btcpredict.config.settings.cross_min_tau_s", 3.0)
    monkeypatch.setattr("btcpredict.config.settings.cross_max_tau_s", 30.0)
    monkeypatch.setattr("btcpredict.config.settings.cross_max_price", 0.50)
    monkeypatch.setattr("btcpredict.config.settings.cross_min_price", 0.02)
    # This file tests where the LEVEL comes from, not the margin gates. The
    # fixture's crossing is 77030 -> 77045, i.e. 1.95 bps, which the 1.5 bps
    # ceiling would refuse -- so neutralise it here rather than re-tune the
    # tape and lose the level-source coverage. See config.cross_max_margin_bps.
    monkeypatch.setattr("btcpredict.config.settings.cross_max_margin_bps", 0.0)


CROSS_UP = [(int(time.time() * 1000) - 3000, 77030.0),
            (int(time.time() * 1000) - 2000, 77030.0),
            (int(time.time() * 1000), 77045.0)]


def make_bot(store, feed_ticks, ref_window, session=True):
    """`ref_window` is `self._ticks` -- EMPTY is the restart case, which is the
    one where the level has to come from the klines."""
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
    if session:
        bot.session = object()
    return bot


def _answer(monkeypatch, level):
    async def fake(self, slug, start_ms):
        return level
    monkeypatch.setattr(Bot, "_fetch_cross_level_kline", fake)


def _drain(bot):
    async def go():
        await asyncio.gather(*list(getattr(bot, "_cross_kline_tasks", {}).values()))
    asyncio.run(go())


class TestTheLevelOffTheKlines:
    def test_the_restart_round_is_read_from_the_klines_not_the_strike(
            self, store, armed, monkeypatch):
        """The headline: the tape cannot bracket the window, the kline can, and
        the arm waits a poll rather than pricing off the strike."""
        bot = make_bot(store, CROSS_UP, [])
        _answer(monkeypatch, 77050.0)

        async def go():
            first = bot._cross_level(bot._round)          # starts the read
            await asyncio.gather(*bot._cross_kline_tasks.values())
            return first, bot._cross_level(bot._round)    # now the answer

        first, landed = asyncio.run(go())
        assert first == (None, "kline-pending"), (
            "the tick must fail CLOSED while the level is unknown, not trade "
            "the strike")
        assert landed == (77050.0, "kline60")
        assert bot._cross_ref[SLUG] == (77050.0, "kline60")

    def test_the_live_trade_does_not_fire_once_the_level_is_known(
            self, store, armed, monkeypatch):
        """bnb-updown-5m-1789710600 in shape: the spot ticks up THROUGH THE
        STRIKE while sitting BELOW the level. On the strike that is an entry;
        on the level there is no crossing to take, and the arm must do
        nothing."""
        bot = make_bot(store, CROSS_UP, [])
        _answer(monkeypatch, 77050.0)          # the level, +1.3 bps over strike
        calls = []

        async def place(*a, **k):
            calls.append(k)
            return None
        bot._place_order = place

        async def go():
            await bot._cross_tick()            # starts the read, returns on None
            await asyncio.gather(*bot._cross_kline_tasks.values())
            await bot._cross_tick()            # now measured against the level

        asyncio.run(go())
        assert not calls, (
            "fired on a crossing of the strike that never cleared the level "
            "the round is paid on -- this is the -$1.05 entry")
        assert bot.store.counts()["decisions"] == 0

    def test_the_old_rule_would_have_taken_it(self, store, armed, monkeypatch):
        """A guard on the test above: with `cross_ref_twap` OFF the same tape
        fires, so the silence there is the level and not a dead trigger."""
        bot = make_bot(store, CROSS_UP, [])
        monkeypatch.setattr("btcpredict.config.settings.cross_ref_twap", False)
        calls = []

        async def place(*a, **k):
            calls.append(k)
            return None
        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert calls, "with the knob off the strike crossing must still fire"

    def test_the_trigger_row_says_which_source_the_level_came_from(
            self, store, armed, monkeypatch):
        """A silent fallback is the old behaviour wearing the new name; the
        cohort has to be splittable from the DB."""
        bot = make_bot(store, CROSS_UP, [])
        _answer(monkeypatch, 77030.0)          # at the tape's old level: fires
        calls = []

        async def place(*a, **k):
            calls.append(k)
            return None
        bot._place_order = place

        async def go():
            bot._cross_level(bot._round)
            await asyncio.gather(*bot._cross_kline_tasks.values())
            await bot._cross_tick()

        asyncio.run(go())
        assert calls
        row = list(bot.store.query("select reason from decisions"))[0]
        assert "ref=kline60" in row[0]

    def test_a_second_tick_does_not_pile_on_a_second_read(
            self, store, armed, monkeypatch):
        """`_cross_level` runs once per family per tick; one REST call per
        round is the budget, and a family that is already waiting is left
        alone."""
        bot = make_bot(store, CROSS_UP, [])
        calls = []

        async def slow(self, slug, start_ms):
            calls.append(slug)
            await asyncio.sleep(0.05)
            return 77050.0
        monkeypatch.setattr(Bot, "_fetch_cross_level_kline", slow)

        async def go():
            states = [bot._cross_level(bot._round) for _ in range(4)]
            await asyncio.gather(*bot._cross_kline_tasks.values())
            return states

        states = asyncio.run(go())
        assert all(s == (None, "kline-pending") for s in states)
        assert len(calls) == 1, "four ticks started more than one read"

    def test_it_never_caches_the_pending_answer(self, store, armed, monkeypatch):
        """If `(None, 'kline-pending')` were memoised the round would be dead
        for its whole life -- the failure this repo has already had once."""
        bot = make_bot(store, CROSS_UP, [])
        _answer(monkeypatch, 77050.0)

        async def go():
            bot._cross_level(bot._round)
            await asyncio.gather(*bot._cross_kline_tasks.values())

        asyncio.run(go())
        assert bot._cross_ref[SLUG] == (77050.0, "kline60")


class TestWhenNoKlineAnswerCanArrive:
    def test_no_session_keeps_the_pre_change_behaviour(self, store, armed,
                                                       caplog):
        """Every fixture-built Bot has no session, so this is also the reason
        the rest of the suite keeps its old expectations: a read that cannot be
        ASKED is not a read that failed."""
        bot = make_bot(store, CROSS_UP, [], session=False)
        calls = []

        async def place(*a, **k):
            calls.append(k)
            return None
        bot._place_order = place
        with caplog.at_level(logging.WARNING, logger="btcpredict"):
            assert bot._cross_level(bot._round) == (STRIKE, "strike")
            asyncio.run(bot._cross_tick())
        assert calls, "the strike fallback must still reach the order path"
        assert _warned(caplog, "NO LEVEL"), (
            "the fallback must never be silent")

    def test_a_sync_caller_cannot_start_a_read(self, store, armed):
        """`_cross_level` is sync and tests call it directly: without a running
        loop there is no task, so the strike is the answer."""
        bot = make_bot(store, CROSS_UP, [])
        assert bot._ensure_cross_level_kline(bot._round, 1) == "impossible"

    def test_an_unknown_symbol_cannot_be_asked(self, store, armed):
        bot = make_bot(store, CROSS_UP, [])
        bot._round.slug = "nope-updown-5m-1789000000"
        assert bot._ensure_cross_level_kline(bot._round, 1) == "impossible"

    def test_the_read_is_retried_then_gives_up_ON_the_strike(
            self, store, armed, monkeypatch, caplog):
        """A transient failure must not kill the round, and a permanent one
        must not stall it either -- three attempts, then the strike, logged."""
        bot = make_bot(store, CROSS_UP, [])
        seen = []

        async def fake(self, slug, start_ms):
            seen.append(slug)
            return None                     # e.g. an incomplete window
        monkeypatch.setattr(Bot, "_fetch_cross_level_kline", fake)

        async def go():
            out = []
            for _ in range(CROSS_LEVEL_KLINE_TRIES + 1):
                out.append(bot._cross_level(bot._round))
                await asyncio.gather(*list(bot._cross_kline_tasks.values()))
            return out

        with caplog.at_level(logging.WARNING, logger="btcpredict"):
            states = asyncio.run(go())
        assert states[:CROSS_LEVEL_KLINE_TRIES] == [(None, "kline-pending")] * 3
        assert states[-1] == (STRIKE, "strike"), (
            "after the attempts are spent the round must not sit dead")
        assert len(seen) == CROSS_LEVEL_KLINE_TRIES, "the attempt budget is 3"
        assert _warned(caplog, "NO LEVEL")

    def test_a_raising_read_is_a_failed_read_not_a_crash(self, store, armed,
                                                         monkeypatch, caplog):
        bot = make_bot(store, CROSS_UP, [])

        async def boom(self, slug, start_ms):
            raise RuntimeError("kline REST is down")
        monkeypatch.setattr(Bot, "_fetch_cross_level_kline", boom)

        async def go():
            bot._cross_level(bot._round)
            await asyncio.gather(*list(bot._cross_kline_tasks.values()))

        with caplog.at_level(logging.WARNING, logger="btcpredict"):
            asyncio.run(go())
        assert any("kline read failed" in r.getMessage() for r in
                   caplog.records)


class TestTheRecord:
    def test_the_constant_is_three(self):
        assert CROSS_LEVEL_KLINE_TRIES == 3

    def test_the_config_quotes_the_measurement_and_the_revert(self):
        import inspect
        from btcpredict import config as C
        src = inspect.getsource(C.Settings)
        assert "752.5447" in src and "5.98 bps" in src, (
            "the config must carry the round that was lost and the gap")
        assert "ref=kline60" in src, "the new source has to be named"
        assert "752.4772" in src and "-4.61 bps" in src, (
            "and the kline's own reading of that round, which refuses it")
        assert "median\n    # +0.007 bps" in src or "+0.007 bps" in src, (
            "the new source has to be pinned to the one it replaces")
        assert "CROSS_REF_TWAP=false" in src, "every knob needs its revert path"

    def test_the_prune_covers_the_retry_counter(self):
        """`_cross_ref` is pruned to the live slugs each tick; a counter left
        behind would suppress the first attempt of the NEXT round with the same
        slug -- and these slugs are unique, so it would leak instead."""
        import inspect
        src = inspect.getsource(Bot._cross_tick)
        assert "_cross_kline = {k: v for k, v in" in src
        assert "if k in live}" in src

    def test_the_source_name_matches_what_the_row_records(self):
        import inspect
        src = inspect.getsource(Bot._cross_level_kline)
        assert 'cache[slug] = (level, "kline60")' in src
