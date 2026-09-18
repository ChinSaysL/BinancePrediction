"""The strike backfill: the families the Binance prediction feed does not carry.

WHY THIS FILE EXISTS. Polymarket lists sol/xrp/doge up/down markets and the bot
may order in them, but the STRIKE had no source -- the wrapper feed carries
btc/eth/bnb only and gamma carries no reference price at all. A round without a
strike never reaches `_mkt`, so those families were discovered, cached, and
never evaluated (`9/9 cached, 6 live`, with the three missing ones appearing
nowhere else because a refusal is not an error).

The backfill reads the round's START price off the live spot tape, and the
property under test is NOT the happy path -- it is the FAIL-CLOSED behaviour,
because a wrong strike is strictly worse than none: the arms gate on
`strike is None`, and `crossed_side` would look for a crossing of a level this
market was never settled against. A wrong strike does not raise; it trades.
"""
from __future__ import annotations

import asyncio
import ast
import pathlib
import time

import pytest

from btcpredict.main import Bot
from btcpredict.venues.polymarket import feed as F
from btcpredict.venues.polymarket import markets as M
from btcpredict.venues.polymarket.feed import PmMarketFeed

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = (ROOT / "btcpredict" / "main.py").read_text()

# A family whose strike the WRAPPER does not carry -- the case this exists for.
SOL = "sol-updown-5m"
SYMBOL = "SOLUSDT"


def _slug(prefix: str, start_ts: int) -> str:
    return f"{prefix}-{start_ts}"


class FakeSpot:
    """`SpotFeed`'s one relevant attribute: the append-ordered tick tape."""

    def __init__(self, ticks):
        self.ticks = ticks


def _bot(spots):
    b = Bot.__new__(Bot)          # skip network setup
    b.spots = spots
    return b


def _pm_round(slug: str, start_ts: int) -> M.PmRound:
    return M.PmRound(slug=slug, start_ts=start_ts, condition_id="0xcond",
                     up_token="1" * 40, down_token="2" * 40, tick=0.01,
                     min_order=5.0, end_ms=(start_ts + 300) * 1000,
                     neg_risk=False, liquidity=1.0)


class FakeWrapper:
    """The Binance prediction feed. Returns nothing for families it lacks."""

    def __init__(self, strike=75832.5, slug=None, listed=True):
        self.strike = strike
        self.slug = slug
        self.listed = listed
        self.asked: list[str] = []

    async def rounds_by_prefixes(self, prefixes):
        p = prefixes[0]
        self.asked.append(p)
        if not self.listed:
            return {}                     # the wrapper has no such market
        return {p: type("R", (), {"strike": self.strike, "slug": self.slug})()}


class CountingBackfill:
    def __init__(self, value):
        self.value = value
        self.asked: list[str] = []

    def __call__(self, slug):
        self.asked.append(slug)
        return self.value


# ---------------------------------------------------------------------------
# the Bot-side helper: the tape read, and when it must refuse
# ---------------------------------------------------------------------------

class TestTheBackfilledStrike:
    def test_it_returns_the_newest_tick_at_or_before_the_open(self):
        """The tick AFTER the open must never be used.

        A price from a second after the open carries a second of the round's
        own drift, which is precisely the quantity the strike is meant to be
        measured against -- using it makes every crossing look smaller.
        """
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        open_ms = start_ts * 1000
        bot = _bot({SYMBOL: FakeSpot([
            (open_ms - 5_000, 100.0),
            (open_ms - 30, 101.5),      # <- the answer
            (open_ms + 30, 102.0),      # after the open: must be ignored
            (open_ms + 5_000, 103.0),
        ])})
        assert bot._pm_strike_backfill(_slug(SOL, start_ts)) == 101.5

    def test_it_refuses_a_round_that_has_not_opened_yet(self):
        """Before the open the newest tick is a PRE-ROLL price.

        A strike is the level the round is settled against, so a price from
        before the round is not that level -- and the caller retries on
        `recheck_s`, so refusing here self-heals instead of caching a wrong
        answer.
        """
        start_ts = int(time.time()) + 30
        bot = _bot({SYMBOL: FakeSpot([(start_ts * 1000 - 10, 100.0)])})
        assert bot._pm_strike_backfill(_slug(SOL, start_ts)) is None

    def test_it_refuses_a_stale_tick_rather_than_using_the_wrong_minute(self):
        """The tape stalled: the nearest price is from before the round.

        This is a SAFETY bound. A websocket tick is 10-50 ms old in production
        (BTC bookTicker runs 33-114 updates/s), so the only way to get here is
        a stalled feed or a boot after the open -- and both must produce NO
        strike.
        """
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        open_ms = start_ts * 1000
        bot = _bot({SYMBOL: FakeSpot([(open_ms - 60_000, 100.0)])})
        assert bot._pm_strike_backfill(_slug(SOL, start_ts)) is None

    def test_a_tick_exactly_on_the_tolerance_is_still_accepted(self):
        from btcpredict.main import _PM_STRIKE_TICK_TOL_MS
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        open_ms = start_ts * 1000
        bot = _bot({SYMBOL: FakeSpot([
            (open_ms - _PM_STRIKE_TICK_TOL_MS, 100.0)])})
        assert bot._pm_strike_backfill(_slug(SOL, start_ts)) == 100.0

    def test_it_refuses_when_every_tick_is_after_the_open(self):
        """Booted mid-round: the buffer holds nothing that predates the open."""
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        open_ms = start_ts * 1000
        bot = _bot({SYMBOL: FakeSpot([(open_ms + 1_000, 100.0),
                                      (open_ms + 2_000, 101.0)])})
        assert bot._pm_strike_backfill(_slug(SOL, start_ts)) is None

    def test_it_refuses_an_empty_or_missing_tape(self):
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        assert _bot({SYMBOL: FakeSpot([])})._pm_strike_backfill(
            _slug(SOL, start_ts)) is None
        assert _bot({})._pm_strike_backfill(_slug(SOL, start_ts)) is None
        # a feed that exists but never published a tick
        assert _bot({SYMBOL: object()})._pm_strike_backfill(
            _slug(SOL, start_ts)) is None

    def test_it_refuses_a_family_it_cannot_map_to_a_symbol(self):
        """The slug is the ground truth for which tape to read."""
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        bot = _bot({SYMBOL: FakeSpot([(start_ts * 1000 - 10, 100.0)])})
        assert bot._pm_strike_backfill("hype-updown-5m-1") is None
        assert bot._pm_strike_backfill("not-a-slug") is None
        assert bot._pm_strike_backfill("") is None

    def test_the_symbol_comes_from_the_slug_not_from_config_defaults(self):
        """`r.symbol` is BTCUSDT for every family in this repo (see the
        SYMBOL_BY_PREFIX note), so a lookup keyed on it would read BTC's tape
        for a sol round."""
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        open_ms = start_ts * 1000
        bot = _bot({"BTCUSDT": FakeSpot([(open_ms - 10, 76_000.0)]),
                    SYMBOL: FakeSpot([(open_ms - 10, 97.5)])})
        assert bot._pm_strike_backfill(_slug(SOL, start_ts)) == 97.5


# ---------------------------------------------------------------------------
# the feed: wrapper first, backfill only where the wrapper has nothing
# ---------------------------------------------------------------------------

class TestTheRoundSourceUsesIt:
    def _source(self, monkeypatch, *, wrapper, backfill, slug, start_ts):
        monkeypatch.setattr(M, "current_and_next",
                            lambda now=None, series=None: (
                                _pm_round(slug, start_ts), None))
        monkeypatch.setattr(M, "fetch_round",
                            lambda ts, series=None: _pm_round(slug, start_ts))
        monkeypatch.setattr(M, "fetch_round_any",
                            lambda ts, series=None: _pm_round(slug, start_ts))
        return PmMarketFeed(strike_feed=wrapper, strike_backfill=backfill)

    def test_the_wrapper_wins_for_the_families_it_carries(self, monkeypatch):
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        slug = _slug("btc-updown-5m", start_ts)
        wrap = FakeWrapper(strike=76_123.0, slug=slug)
        back = CountingBackfill(99_999.0)
        src = self._source(monkeypatch, wrapper=wrap, backfill=back,
                           slug=slug, start_ts=start_ts)
        r = asyncio.run(src.live_round())
        assert r.strike == 76_123.0        # the oracle value, not the tape
        assert back.asked == []            # never consulted

    def test_the_backfill_supplies_a_family_the_wrapper_lacks(self, monkeypatch):
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        slug = _slug(SOL, start_ts)
        wrap = FakeWrapper(listed=False)   # the wrapper has no sol market
        back = CountingBackfill(97.53)
        src = self._source(monkeypatch, wrapper=wrap, backfill=back,
                           slug=slug, start_ts=start_ts)
        r = asyncio.run(src.live_round())
        assert r.strike == 97.53
        assert back.asked == [slug]

    def test_a_backfill_hit_is_cached_so_the_sweep_runs_once(self, monkeypatch):
        """A miss-sweep is 30 pages / ~4 s when the prefix is not found. The
        retry throttle would otherwise spend that on EVERY retry, and the
        primary family is refetched every poll."""
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        slug = _slug(SOL, start_ts)
        wrap = FakeWrapper(listed=False)
        back = CountingBackfill(97.53)
        src = self._source(monkeypatch, wrapper=wrap, backfill=back,
                           slug=slug, start_ts=start_ts)
        for _ in range(5):
            assert asyncio.run(src.live_round()).strike == 97.53
        assert wrap.asked == [SOL]         # the sweep, once
        assert back.asked == [slug]        # the tape read, once

    def test_neither_source_answering_leaves_the_strike_unknown(
            self, monkeypatch):
        """None is the answer that keeps the family unlive. It must not become
        a zero, a guess, or an exception."""
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        slug = _slug(SOL, start_ts)
        src = self._source(monkeypatch, wrapper=FakeWrapper(listed=False),
                           backfill=CountingBackfill(None), slug=slug,
                           start_ts=start_ts)
        assert asyncio.run(src.live_round()).strike is None

    def test_a_backfill_that_raises_leaves_the_strike_unknown(self, monkeypatch):
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        slug = _slug(SOL, start_ts)

        def boom(_slug):
            raise RuntimeError("tape exploded")

        src = self._source(monkeypatch, wrapper=FakeWrapper(listed=False),
                           backfill=boom, slug=slug, start_ts=start_ts)
        assert asyncio.run(src.live_round()).strike is None

    def test_without_a_backfill_the_wrapper_behaviour_is_unchanged(
            self, monkeypatch):
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        slug = _slug(SOL, start_ts)
        src = self._source(monkeypatch, wrapper=FakeWrapper(listed=False),
                           backfill=None, slug=slug, start_ts=start_ts)
        assert asyncio.run(src.live_round()).strike is None


# ---------------------------------------------------------------------------
# the property this whole change is for
# ---------------------------------------------------------------------------

def test_a_backfilled_strike_lets_the_round_reach_the_arm(monkeypatch):
    """END TO END, and the reason the fix matters.

    `main._record` writes the quote and then RETURNS before `_mkt` when
    `strike is None`, and a family absent from `_mkt` gets no book, no
    evaluation and no decision. So a strike is not a nicety: it is the
    difference between "recorded" and "tradeable", which is exactly the
    `9/9 cached, 6 live` line.
    """
    now_ms = int(time.time() * 1000)
    start_ts = (now_ms - 60_000) // 1000
    open_ms = start_ts * 1000
    slug = _slug(SOL, start_ts)
    bot = _bot({SYMBOL: FakeSpot([(open_ms - 120, 97.1),
                                  (open_ms - 20, 97.53)])})
    monkeypatch.setattr(M, "current_and_next",
                        lambda now=None, series=None: (
                            _pm_round(slug, start_ts), None))
    monkeypatch.setattr(M, "fetch_round_any",
                        lambda ts, series=None: _pm_round(slug, start_ts))
    src = PmMarketFeed(strike_feed=FakeWrapper(listed=False),
                       strike_backfill=bot._pm_strike_backfill)
    r = asyncio.run(src.live_round())
    assert r is not None
    assert r.strike == 97.53
    # the arms evaluate against `r.up`/`r.down`, and `market_prefix` is what
    # keys `_mkt` -- both must work off the slug for this family.
    assert r.up is not None and r.down is not None
    from btcpredict.config import SYMBOL_BY_PREFIX, market_prefix
    assert market_prefix(r.slug) == SOL
    assert SYMBOL_BY_PREFIX[SOL] == SYMBOL


class TestTheRecordPath:
    """`_record` is where "has a strike" becomes "is tradeable".

    Both directions are pinned, because the fix is only meaningful if the
    strike-less case REALLY is what starved these families -- a test that only
    shows the happy path would pass on a build where the guard had moved and
    the strike was irrelevant.
    """

    def _bot(self, ticks):
        b = Bot.__new__(Bot)

        class Store:
            def upsert_round(self, row):
                pass

            def insert(self, table, row):
                pass

        b.store = Store()
        b.spots = {SYMBOL: FakeSpot(ticks)}
        b.spots[SYMBOL].price = ticks[-1][1] if ticks else None
        b.spots[SYMBOL].ts_ms = ticks[-1][0] if ticks else 0
        b.vols = {SYMBOL: type("V", (), {"sigma_1m": lambda self: 5e-4})()}
        b.book = None
        b.ws_book = None
        b.mode = "live"
        b._mkt = {}
        return b

    def _round(self, monkeypatch, *, strike):
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        slug = _slug(SOL, start_ts)
        monkeypatch.setattr(M, "current_and_next",
                            lambda now=None, series=None: (
                                _pm_round(slug, start_ts), None))
        monkeypatch.setattr(M, "fetch_round_any",
                            lambda ts, series=None: _pm_round(slug, start_ts))
        src = PmMarketFeed(strike_feed=None, strike_backfill=lambda s: strike)
        return asyncio.run(src.live_round())

    def test_a_strike_puts_the_family_into_mkt(self, monkeypatch):
        now_ms = int(time.time() * 1000)
        open_ms = ((now_ms - 60_000) // 1000) * 1000
        bot = self._bot([(open_ms - 20, 97.53)])
        r = self._round(monkeypatch, strike=97.53)
        asyncio.run(bot._record(r, SYMBOL, trade=True))
        assert SOL in bot._mkt
        assert bot._mkt[SOL]["symbol"] == SYMBOL
        assert bot._mkt[SOL]["round"] is r

    def test_no_strike_stops_before_mkt_even_though_the_quote_is_written(
            self, monkeypatch):
        """The starvation, and its shape: recorded, then dropped, silently."""
        now_ms = int(time.time() * 1000)
        open_ms = ((now_ms - 60_000) // 1000) * 1000
        bot = self._bot([(open_ms - 20, 97.53)])
        r = self._round(monkeypatch, strike=None)
        asyncio.run(bot._record(r, SYMBOL, trade=True))
        assert bot._mkt == {}



# ---------------------------------------------------------------------------
# the attempt budget, and the tape's depth (both bit us live 2026-09-17)
# ---------------------------------------------------------------------------

class TestTheAttemptBudgetRestartsAtTheOpen:
    """A round is often DISCOVERED BEFORE IT OPENS, and every attempt made
    before the open is doomed by construction -- the backfill refuses
    (`start_ms > now`: the newest tick is a PRE-ROLL price) and the wrapper may
    not have listed the round yet. With `recheck_s = 5.0` the twelve-attempt
    budget is spent inside a minute, so `_STRIKE_MAX_TRIES` abandoned the round
    BEFORE it could ever be priced, for its whole life. Measured live: sol, xrp
    and doge sat at `not live` round after round while the strike was obtainable
    the moment the round opened.
    """

    def _src(self, monkeypatch, t, answer_at):
        from types import SimpleNamespace
        monkeypatch.setattr(F, "time", SimpleNamespace(time=lambda: t[0]))
        calls = []

        def back(slug):
            calls.append(t[0])
            return 97.5 if t[0] >= answer_at else None

        src = F.PmMarketFeed(strike_feed=FakeWrapper(listed=False),
                             strike_backfill=back, recheck_s=5.0)
        return src, calls

    def test_pre_open_attempts_do_not_consume_the_post_open_budget(
            self, monkeypatch):
        t = [1_000_000.0]
        # The pre-open stretch must outlast the budget: 12 attempts at
        # `recheck_s`+1 apiece is 72 s, so the round opens after 120 s.
        start_ts = 1_000_120
        slug = _slug(SOL, start_ts)
        src, calls = self._src(monkeypatch, t, start_ts)

        for _ in range(12):                        # the whole old budget
            t[0] += 6.0
            assert asyncio.run(src._strike_for(slug, start_ts)) is None
        assert len(calls) == 12                    # every one of them tried
        assert t[0] < start_ts                     # ... and all before the open

        t[0] = start_ts + 1.0                      # the round opens
        assert asyncio.run(src._strike_for(slug, start_ts)) == 97.5

    def test_the_post_open_budget_is_still_capped(self, monkeypatch):
        """The sweep cost must stay bounded: 12 attempts after the open, then
        stop asking. Nothing here can be priced, so the cap is what keeps a
        hopeless round from sweeping the wrapper for its whole life."""
        t = [1_000_000.0]
        start_ts = 1_000_000                       # already open
        slug = _slug(SOL, start_ts)
        src, calls = self._src(monkeypatch, t, answer_at=float("inf"))

        for _ in range(20):
            t[0] += 6.0
            assert asyncio.run(src._strike_for(slug, start_ts)) is None
        assert len(calls) == F.PmMarketFeed._STRIKE_MAX_TRIES


def test_the_spot_tape_is_deep_enough_to_bracket_a_rounds_open():
    """The backfill needs a tick AT OR BEFORE the round's open, so the tape's
    depth is the discovery slack it can absorb. At BTC's measured 114
    updates/s, 4,000 ticks was 35 seconds -- less than a round -- so any
    discovery lag lost the price. 40,000 is ~5 minutes."""
    from btcpredict.feeds.binance_spot import SpotFeed
    assert SpotFeed(None).ticks.maxlen >= 40_000


# ---------------------------------------------------------------------------
# THE ROUND THAT OPENED ACROSS A RESTART.
#
# MEASURED 2026-09-17. `systemctl restart` issued at 10:59:43; the process
# answered "running in live mode" at 11:00:01, so it was DOWN across the
# 11:00:00 open. `sol-updown-5m-1789642800` opened inside that gap and was the
# ONLY one of the 63 rounds since 10:30 with `strike IS NULL`.
#
# The tape-based backfill is CORRECT to refuse it: its newest tick is from AFTER
# the open, and a price from after the open is not the level the round is
# settled against. The hole was that nothing else could answer either, and
# `_cross_tick` returned on `r.strike is None` with no line -- so a round the
# arm could not touch read exactly like a market that never crossed.
# ---------------------------------------------------------------------------


def _past_round(ago_s: int = 600, family: str = SOL):
    """A round whose open is `ago_s` seconds in the past, and its open in ms."""
    start_ts = int(time.time()) - ago_s
    return _slug(family, start_ts), start_ts, start_ts * 1000


def _kbot():
    """A Bot with only what the kline fallback reads."""
    b = Bot.__new__(Bot)                  # skip network setup
    b.session = object()
    return b


def _patch_klines(monkeypatch, bars, calls, *, boom=False):
    import btcpredict.main as m

    async def fake(session, interval, limit, symbol="BTCUSDT", end_ms=None):
        calls.append({"interval": interval, "symbol": symbol, "end_ms": end_ms})
        if boom:
            raise RuntimeError("binance said no")
        return bars

    monkeypatch.setattr(m, "fetch_klines", fake)


class TestTheRestartStrikeRecovery:
    def test_it_uses_the_bar_ENDING_at_the_open(self, monkeypatch):
        """The 1 s close at T-1 IS the open price, measured to 0.04-0.11 bps
        against 4,635 rounds with known strikes.

        The bar STARTING at the open is the wrong one by construction -- its
        close is a second into the round, i.e. a second of the round's own
        drift, which is the exact quantity the strike is measured against.
        """
        slug, start_ts, open_ms = _past_round()
        calls: list = []
        _patch_klines(monkeypatch, [
            [open_ms - 3000, "0", "0", "0", "99.000", "0"],
            [open_ms - 2000, "0", "0", "0", "100.000", "0"],
            [open_ms - 1000, "0", "0", "0", "100.105", "0"],   # <- the answer
            [open_ms, "0", "0", "0", "100.500", "0"],          # the bar AT T
        ], calls)
        assert asyncio.run(_kbot()._pm_strike_backfill_kline(slug)) == 100.105
        assert calls[0]["interval"] == "1s"
        assert calls[0]["symbol"] == SYMBOL
        assert calls[0]["end_ms"] == open_ms

    def test_it_refuses_a_bar_that_is_not_exactly_the_one_ending_at_the_open(
            self, monkeypatch):
        """FAIL CLOSED. A shifted or missing bar must yield NO strike, never a
        plausible one: a wrong strike does not raise, it trades."""
        slug, _ts, open_ms = _past_round()
        _patch_klines(monkeypatch, [
            [open_ms - 2000, "0", "0", "0", "100.0", "0"],
            [open_ms, "0", "0", "0", "100.5", "0"],
        ], [])
        assert asyncio.run(_kbot()._pm_strike_backfill_kline(slug)) is None

    def test_it_refuses_a_non_positive_close(self, monkeypatch):
        slug, _ts, open_ms = _past_round()
        _patch_klines(monkeypatch, [[open_ms - 1000, "0", "0", "0", "0", "0"]], [])
        assert asyncio.run(_kbot()._pm_strike_backfill_kline(slug)) is None

    def test_it_ignores_junk_rows(self, monkeypatch):
        slug, _ts, open_ms = _past_round()
        _patch_klines(monkeypatch, [
            [],
            [open_ms - 1000],
            ["nonsense", "0", "0", "0", "100.0", "0"],
            [open_ms - 1000, "0", "0", "0", "97.53", "0"],
        ], [])
        assert asyncio.run(_kbot()._pm_strike_backfill_kline(slug)) == 97.53

    def test_a_fetch_failure_never_raises(self, monkeypatch):
        """It is called from the round refresh: a raise here must not take the
        poll loop with it."""
        slug, _ts, _o = _past_round()
        _patch_klines(monkeypatch, [], [], boom=True)
        assert asyncio.run(_kbot()._pm_strike_backfill_kline(slug)) is None

    def test_it_does_not_ask_before_the_open(self, monkeypatch):
        """A pre-open ask has no answer, and the caller retries anyway."""
        calls: list = []
        _patch_klines(monkeypatch, [[1, "0", "0", "0", "1.0", "0"]], calls)
        start_ts = int(time.time()) + 30
        slug = _slug(SOL, start_ts)
        assert asyncio.run(_kbot()._pm_strike_backfill_kline(slug)) is None
        assert calls == []                     # no request at all

    def test_it_refuses_a_family_it_cannot_map_to_a_symbol(self, monkeypatch):
        calls: list = []
        _patch_klines(monkeypatch, [[1, "0", "0", "0", "1.0", "0"]], calls)
        b = _kbot()
        assert asyncio.run(b._pm_strike_backfill_kline("hype-updown-5m-1")) is None
        assert asyncio.run(b._pm_strike_backfill_kline("not-a-slug")) is None
        assert asyncio.run(b._pm_strike_backfill_kline("")) is None
        assert calls == []

    def test_the_symbol_comes_from_the_slug(self, monkeypatch):
        """Reading BTC's kline for a sol round would be a silently wrong
        strike, which is worse than no strike."""
        slug, _ts, _o = _past_round()
        calls: list = []
        _patch_klines(monkeypatch, [], calls)
        asyncio.run(_kbot()._pm_strike_backfill_kline(slug))
        assert calls[0]["symbol"] == "SOLUSDT"


class AsyncBackfill:
    """`strike_backfill_async`'s shape: an awaitable that can refuse."""

    def __init__(self, value=None, boom=False):
        self.value, self.boom, self.asked = value, boom, []

    async def __call__(self, slug):
        self.asked.append(slug)
        if self.boom:
            raise RuntimeError("kline endpoint down")
        return self.value


def _feed(monkeypatch, wrapper, *, sync=None, asy=None, slug=None,
          start_ts=None):
    monkeypatch.setattr(M, "current_and_next",
                        lambda now=None, series=None: (
                            _pm_round(slug, start_ts), None))
    monkeypatch.setattr(M, "fetch_round",
                        lambda ts, series=None: _pm_round(slug, start_ts))
    monkeypatch.setattr(M, "fetch_round_any",
                        lambda ts, series=None: _pm_round(slug, start_ts))
    return PmMarketFeed(strike_feed=wrapper, strike_backfill=sync,
                        strike_backfill_async=asy)


class TestTheSecondChanceSource:
    def test_the_async_backfill_answers_where_the_tape_cannot(
            self, monkeypatch):
        """The measured case: unlisted family, tape empty for the round, and
        the venue's own history still has the number."""
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        slug = _slug(SOL, start_ts)
        asy = AsyncBackfill(97.53)
        src = _feed(monkeypatch, FakeWrapper(listed=False), sync=CountingBackfill(None),
                    asy=asy, slug=slug, start_ts=start_ts)
        assert asyncio.run(src._strike_for(slug, start_ts)) == 97.53
        assert asy.asked == [slug]

    def test_the_tape_wins_when_it_can_answer(self, monkeypatch):
        """A tick is FINER than a 1 s bar, so it must not be displaced."""
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        slug = _slug(SOL, start_ts)
        asy = AsyncBackfill(1.0)
        src = _feed(monkeypatch, FakeWrapper(listed=False),
                    sync=CountingBackfill(97.5), asy=asy, slug=slug,
                    start_ts=start_ts)
        assert asyncio.run(src._strike_for(slug, start_ts)) == 97.5
        assert asy.asked == []                 # never asked

    def test_the_wrapper_wins_for_the_families_it_carries(self, monkeypatch):
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        slug = _slug("btc-updown-5m", start_ts)
        asy = AsyncBackfill(1.0)
        src = _feed(monkeypatch, FakeWrapper(strike=76400.0, slug=slug),
                    sync=CountingBackfill(2.0), asy=asy, slug=slug,
                    start_ts=start_ts)
        assert asyncio.run(src._strike_for(slug, start_ts)) == 76400.0
        assert asy.asked == []

    def test_a_raising_second_chance_fails_closed(self, monkeypatch):
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        slug = _slug(SOL, start_ts)
        src = _feed(monkeypatch, FakeWrapper(listed=False),
                    sync=CountingBackfill(None), asy=AsyncBackfill(boom=True),
                    slug=slug, start_ts=start_ts)
        assert asyncio.run(src._strike_for(slug, start_ts)) is None

    def test_the_second_chance_is_optional(self, monkeypatch):
        """A feed built without it behaves exactly as before."""
        now_ms = int(time.time() * 1000)
        start_ts = (now_ms - 60_000) // 1000
        slug = _slug(SOL, start_ts)
        src = _feed(monkeypatch, FakeWrapper(listed=False),
                    sync=CountingBackfill(None), slug=slug, start_ts=start_ts)
        assert asyncio.run(src._strike_for(slug, start_ts)) is None


def test_a_strike_less_round_announces_itself():
    """The silent skip, as a regression test.

    `if r is None or not ticks or r.strike is None: return` folded a MISSING
    STRIKE into "no data yet" -- and a strike-less round is not a quiet market,
    it is a family the arm cannot evaluate at all. It must be its own branch
    with a line, once per slug (this runs on the poll loop).
    """
    tick = next(n for n in ast.walk(ast.parse(MAIN))
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_cross_tick")
    body = ast.get_source_segment(MAIN, tick) or ""
    assert "not ticks or r.strike is None" not in body      # the old fold
    assert "if r.strike is None:" in body
    assert "_no_strike_seen" in body
    assert "log.warning" in body
    assert "no strike" in body


def test_main_wires_the_kline_recovery_into_the_feed():
    assert "_pm_strike_backfill_kline" in MAIN
    assert "strike_backfill_async" in MAIN
