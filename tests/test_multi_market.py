"""Per-market cross knobs, and the websocket book following every market.

Both are the multi-market experiment's blockers, and both are the kind of
branch that a fixture can leave dead: the recorded-only families never reached
the ws-book read because it was gated on `trade`, and every knob was global, so
a test could pass while the market it named was ignored.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from btcpredict.config import MARKETS, cross_knob, market_prefix, settings
from btcpredict.main import Bot


class TestMarketPrefix:
    def test_strips_the_round_epoch(self):
        assert market_prefix("btc-updown-5m-1788872400") == "btc-updown-5m"
        assert market_prefix("bnb-updown-15m-1787283000") == "bnb-updown-15m"

    def test_leaves_a_bare_prefix_alone(self):
        assert market_prefix("btc-updown-5m") == "btc-updown-5m"

    def test_none_is_none(self):
        assert market_prefix(None) is None
        assert market_prefix("") is None


class TestCrossKnob:
    def test_falls_back_to_the_global(self, monkeypatch):
        monkeypatch.setattr(settings, "cross_overrides", {})
        monkeypatch.setattr(settings, "cross_max_depth_bps", 0.5)
        assert cross_knob("eth-updown-5m", "max_depth_bps") == 0.5

    def test_override_wins_for_its_own_market_only(self, monkeypatch):
        monkeypatch.setattr(settings, "cross_overrides",
                            {"eth-updown-5m": {"max_depth_bps": 2.0}})
        monkeypatch.setattr(settings, "cross_max_depth_bps", 0.5)
        assert cross_knob("eth-updown-5m", "max_depth_bps") == 2.0
        # the live market must not move because another one was tuned
        assert cross_knob("btc-updown-5m", "max_depth_bps") == 0.5
        assert cross_knob(None, "max_depth_bps") == 0.5

    def test_an_unset_knob_falls_through_past_a_set_one(self, monkeypatch):
        monkeypatch.setattr(settings, "cross_overrides",
                            {"eth-updown-5m": {"max_depth_bps": 2.0}})
        monkeypatch.setattr(settings, "cross_min_tau_s", 10.0)
        assert cross_knob("eth-updown-5m", "min_tau_s") == 10.0


class TestBotKnobResolution:
    def _bot(self, slug):
        b = Bot.__new__(Bot)
        b._round = SimpleNamespace(slug=slug)
        return b

    def test_ck_uses_the_round_under_the_arm(self, monkeypatch):
        monkeypatch.setattr(settings, "cross_overrides",
                            {"eth-updown-15m": {"max_price": 0.77}})
        monkeypatch.setattr(settings, "cross_max_price", 0.90)
        assert self._bot("eth-updown-15m-1787283000")._ck("max_price") == 0.77
        assert self._bot("btc-updown-5m-1788872400")._ck("max_price") == 0.90

    def test_ck_survives_no_round(self, monkeypatch):
        monkeypatch.setattr(settings, "cross_overrides", {})
        monkeypatch.setattr(settings, "cross_max_price", 0.90)
        b = Bot.__new__(Bot)
        assert b._ck("max_price") == 0.90


class TestMarketsTable:
    def test_covers_btc_eth_bnb_on_5m_and_15m(self):
        want = {f"{c}-updown-{tf}" for c in ("btc", "eth", "bnb")
                for tf in ("5m", "15m")}
        assert want <= {p for p, _, _ in MARKETS}

    def test_btc_5m_is_still_the_only_traded_family(self):
        assert MARKETS[0][0] == "btc-updown-5m"


class TestWsBookFollowsEveryMarket:
    def _bot(self, found, follows):
        b = Bot.__new__(Bot)
        b.ws_book = SimpleNamespace(follow=lambda ids: follows.append(list(ids)))
        b.market = SimpleNamespace(
            rounds_by_prefixes=lambda prefixes: _done(found))
        b.refresh_vol_if_stale = _noop
        b._record = _noop_record
        return b

    def test_only_the_traded_market_is_subscribed(self):
        """Subscribing all six cost 3-4x the decision cadence (1.00s -> 4.15s
        median), and a slower poll is what makes a real crossing invisible."""
        found = {p: SimpleNamespace(market_id=100 + i, slug=f"{p}-1")
                 for i, (p, _, _) in enumerate(MARKETS)}
        follows = []
        asyncio.run(self._bot(found, follows).tick())
        assert follows == [[100]], "only MARKETS[0] may be followed"

    def test_a_missing_primary_id_subscribes_nothing(self):
        found = {"btc-updown-5m": SimpleNamespace(market_id=None, slug="a"),
                 "eth-updown-5m": SimpleNamespace(market_id=9, slug="b")}
        follows = []
        asyncio.run(self._bot(found, follows).tick())
        assert follows == [], "a non-primary id must never be followed"

    def test_nothing_live_means_no_handshake(self):
        follows = []
        asyncio.run(self._bot({}, follows).tick())
        assert follows == []


def _done(v):
    """market.rounds_by_prefixes is awaited, so hand tick() a coroutine."""
    async def _c():
        return v
    return _c()


async def _noop(*a, **k):
    return None


async def _noop_record(*a, **k):
    return None


class TestEntryModeOff:
    """The `off` value must actually stop the main arm -- an enum member with no
    code path behind it is worse than no option at all."""

    def test_off_is_a_valid_entry_mode(self):
        from btcpredict.config import Settings
        assert "off" in Settings.model_fields["entry_mode"].annotation.__args__

    def test_off_returns_before_the_model_runs(self, monkeypatch):
        import btcpredict.main as m
        src = m.Bot._record.__doc__ or ""
        # the gate is a source-level guarantee; assert the branch exists and is
        # positioned after the cross tick rather than trusting the comment
        import inspect
        body = inspect.getsource(m.Bot._record)
        assert 'settings.entry_mode == "off"' in body
        # The cross arm is dispatched from tick(), not from _record, since it
        # was widened to every CROSS_TRADED family on 2026-09-14. The invariant
        # is unchanged and now sits one level up: _record's `off` return cannot
        # skip the cross arm, because tick() calls _cross_all() after the market
        # loop and outside any entry_mode branch.
        tick = inspect.getsource(m.Bot.tick)
        assert "_cross_all()" in tick, \
            "turning the main arm off must not skip the cross arm"
        assert "entry_mode" not in tick, \
            "the cross dispatch must not sit behind an entry_mode branch"
        # ...and _record must still register the per-market context before that
        # `off` return, or an off main arm would empty the cross arm's world.
        assert body.index("self._mkt") < body.index('entry_mode == "off"'), \
            "the cross arm's per-market context must be built before the off gate"


class TestRoundsAreCachedUntilTheyRoll:
    """Trade latency must not depend on an unrelated market's popularity.

    `rounds_by_prefixes` pages a volume-sorted list and stops only once EVERY
    requested prefix is found, so one quiet family sinks through the day --
    bnb-updown-5m sat at offset 1450, 30 pages, 3.95s, and took the decision
    loop from a 1.00s median to 4.15s. With six TRADED families that sweep
    cannot run every poll.

    It does not have to: a round's metadata (strike, token ids, end time) is
    fixed once it opens, and the two things that move -- spot and the ask --
    arrive on their own feeds. So a family is re-swept only when its round has
    actually rolled. This replaces the older RECORDED_REFRESH_S interval, which
    could not work once the recorded families became tradeable.
    """

    def _bot(self, asked, rounds):
        b = Bot.__new__(Bot)
        b.ws_book = SimpleNamespace(follow=lambda ids: None)
        b.market = SimpleNamespace(
            rounds_by_prefixes=lambda prefixes: _capture(prefixes, asked, rounds))
        b.refresh_vol_if_stale = _noop
        b._record = _noop_record
        return b

    @staticmethod
    def _live(end_ms):
        return {p: SimpleNamespace(market_id=100 + i, slug=f"{p}-1",
                                   end_ms=end_ms, strike=100.0)
                for i, (p, _, _) in enumerate(MARKETS)}

    def test_first_tick_sweeps_everything(self):
        asked = []
        live = self._live(int(time.time() * 1000) + 300_000)
        asyncio.run(self._bot(asked, live).tick())
        assert len(asked[0]) == len(MARKETS), "a cold cache must sweep all"

    def test_a_live_round_is_not_swept_again(self):
        asked = []
        live = self._live(int(time.time() * 1000) + 300_000)
        b = self._bot(asked, live)
        asyncio.run(b.tick())
        asyncio.run(b.tick())
        assert asked[1] == [MARKETS[0][0]], (
            "a family whose round is still live must not be paged for again; "
            "only the primary is refetched every poll")

    def test_a_rolled_round_is_swept_again(self):
        asked = []
        live = self._live(int(time.time() * 1000) + 300_000)
        b = self._bot(asked, live)
        asyncio.run(b.tick())
        for r in b._round_cache.values():
            r.end_ms = int(time.time() * 1000) - 1
        asyncio.run(b.tick())
        assert len(asked[1]) == len(MARKETS), (
            "once a round has rolled its family must be swept again")

    def test_a_round_without_a_strike_is_never_cached(self):
        """A strikeless round cannot be traded and must not pin the cache."""
        asked = []
        live = self._live(int(time.time() * 1000) + 300_000)
        for r in live.values():
            r.strike = None
        b = self._bot(asked, live)
        asyncio.run(b.tick())
        asyncio.run(b.tick())
        assert len(asked[1]) == len(MARKETS)


def _capture(prefixes, sink, rounds):
    sink.append(list(prefixes))
    async def _c():
        return {p: rounds[p] for p in prefixes if p in rounds}
    return _c()


class TestCrossRunsOnTheFeedClock:
    """The cross arm's latency must not depend on the impulse arm's switch.

    With impulse off, `_idle` used to sleep the whole poll budget, dropping
    cross from ~114 price updates/s to one look per second -- which is how
    btc-updown-5m-1789336500's 1.5s crossing went unseen.
    """

    def _bot(self, calls):
        import asyncio as aio
        b = Bot.__new__(Bot)
        b._stop = aio.Event()

        async def cross():
            calls.append("cross")
            b._stop.set()          # one pass is enough for the assertion

        async def impulse():
            calls.append("impulse")

        b._cross_tick = cross
        b._impulse_tick = impulse
        b.spot = SimpleNamespace(wait_tick=_never)
        return b

    def test_cross_ticks_with_impulse_OFF(self, monkeypatch):
        monkeypatch.setattr(settings, "entry_mode", "model")
        monkeypatch.setattr(settings, "cross_enabled", True)
        calls = []
        asyncio.run(self._bot(calls)._idle(0.05))
        assert "cross" in calls, "cross must run on the feed clock without impulse"
        assert "impulse" not in calls, "impulse is off and must not be called"

    def test_neither_arm_means_a_plain_sleep(self, monkeypatch):
        monkeypatch.setattr(settings, "entry_mode", "model")
        monkeypatch.setattr(settings, "cross_enabled", False)
        calls = []
        asyncio.run(self._bot(calls)._idle(0.01))
        assert calls == []


async def _never(timeout):
    return False


class TestCrossRunsAcrossEveryTradedMarket:
    """The cross arm was widened from BTC-5m to every family in CROSS_TRADED on
    2026-09-14. These pin the parts of that widening which fail SILENTLY."""

    def _bot(self, mkt):
        b = Bot.__new__(Bot)
        b.mode = "live"
        b._mkt = mkt
        return b

    def test_every_market_is_ticked(self):
        seen = []

        async def one(ctx=None):
            seen.append(ctx["round"].slug)

        b = self._bot({p: {"round": SimpleNamespace(slug=f"{p}-1")}
                       for p, _, _ in MARKETS})
        b._cross_tick = one
        asyncio.run(b._cross_all())
        assert len(seen) == len(MARKETS), "every traded family must be ticked"

    def test_one_market_raising_does_not_stop_the_others(self):
        """A six-market arm whose whole loop dies because one family returned a
        malformed round is strictly worse than the single-market one."""
        seen = []

        async def one(ctx=None):
            if ctx["round"].slug.startswith("eth-updown-5m"):
                raise ValueError("malformed round")
            seen.append(ctx["round"].slug)

        b = self._bot({p: {"round": SimpleNamespace(slug=f"{p}-1")}
                       for p, _, _ in MARKETS})
        b._cross_tick = one
        asyncio.run(b._cross_all())
        assert len(seen) == len(MARKETS) - 1

    def test_no_context_falls_back_to_the_single_market_path(self):
        calls = []

        async def one(ctx=None):
            calls.append(ctx)

        b = self._bot({})
        b._cross_tick = one
        asyncio.run(b._cross_all())
        assert calls == [None], "an empty world must still tick the primary"

    def test_knobs_resolve_against_the_round_not_self_round(self):
        """`_ck` reads the round it is handed. Reading `self._round` here would
        price eth-updown-15m with BTC's range gate -- and since the gate is in
        BPS while a 15m round has 3x as long to travel, BTC's 10.0 blocks 77%
        of eth-15m's rounds against 40% of BTC's."""
        b = Bot.__new__(Bot)
        b._round = SimpleNamespace(slug="btc-updown-5m-1")
        btc = b._ck("max_round_range_bps", SimpleNamespace(slug="btc-updown-5m-1"))
        eth15 = b._ck("max_round_range_bps", SimpleNamespace(slug="eth-updown-15m-1"))
        assert btc != eth15, "per-market overrides must actually apply"
        assert b._ck("max_round_range_bps") == btc, "None falls back to _round"

    def test_per_round_state_survives_other_markets(self):
        """`_cross_fired` used to be pruned to the CURRENT round's slug, which
        with six families means each market wipes the others -- every market
        would forget it had fired and the per-round cap would never bind."""
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert "if k == r.slug" not in src, (
            "per-round state is pruned to a single slug; with six families that "
            "erases every other market's fired count"
        )
        assert "live" in src and "_cross_fired = {" in src
