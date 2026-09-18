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

    def test_every_cached_family_is_subscribed(self):
        """The family set must come from the ROUND CACHE, not from `_mkt`.

        The old list was built from `_mkt`, and `_mkt` is filled BY `_record`,
        which needs the BOOK this socket provides -- a closed loop. Measured
        2026-09-17: `pm book ws connected (12 assets)` = 6 families, sol/xrp/doge
        `not live` for hours, every trigger on them refused as `no ask on the
        book` with nothing logged as an error. Subscribing all nine costs
        message volume, and that is now monitored (`poll_ms`, the backlog guard)
        rather than assumed -- so the switch governs it, not this default.
        """
        found = {p: SimpleNamespace(market_id=100 + i, slug=f"{p}-1")
                 for i, (p, _, _) in enumerate(MARKETS)}
        follows = []
        asyncio.run(self._bot(found, follows).tick())
        assert len(follows[0]) == len(MARKETS)
        assert {r.slug for r in follows[0]} == {f"{p}-1" for p, _, _ in MARKETS}

    def test_follow_all_off_subscribes_only_the_primary(self, monkeypatch):
        """The switch is the escape hatch if the poll degrades (1.00s -> 4.15s
        median was measured with six families followed on the old sweep)."""
        monkeypatch.setattr(settings, "cross_ws_follow_all", False)
        found = {p: SimpleNamespace(market_id=100 + i, slug=f"{p}-1")
                 for i, (p, _, _) in enumerate(MARKETS)}
        follows = []
        asyncio.run(self._bot(found, follows).tick())
        assert [r.slug for r in follows[0]] == ["btc-updown-5m-1"]

    def test_a_round_with_no_market_id_is_still_followed(self):
        """Polymarket rounds carry NO marketId: the arms address them by slug
        and the venue by token id. The old `if r.market_id` guard therefore
        built a permanently EMPTY follow list on that venue, so the socket was
        never subscribed and the feed was read by nothing. The FAMILY set is
        what bounds the subscription, never the wrapper's id."""
        found = {"btc-updown-5m": SimpleNamespace(market_id=None, slug="a"),
                 "eth-updown-5m": SimpleNamespace(market_id=9, slug="b")}
        follows = []
        asyncio.run(self._bot(found, follows).tick())
        assert {r.slug for r in follows[0]} == {"a", "b"}

    def test_what_follow_gets_is_a_round_not_an_id(self):
        """Both book classes accept a Round -- the one argument that means the
        same thing on a venue keyed by an integer id and one keyed by slug."""
        found = {"btc-updown-5m": SimpleNamespace(market_id=100, slug="a")}
        follows = []
        asyncio.run(self._bot(found, follows).tick())
        assert isinstance(follows[0][0], SimpleNamespace)
        assert getattr(follows[0][0], "slug", None) == "a"

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

    The re-sweep also runs OFF the poll: only the primary is fetched inline.
    Doing all six inline stalled the poll 4.4-4.7s at every 5m roll, which can
    blind a 15m family sitting at tau 5-60 on its own schedule.
    """

    @staticmethod
    def _drain(b):
        """Run the background round refresh to completion, if one was spawned."""
        t = getattr(b, "_round_refresh", None)
        if t is not None:
            asyncio.get_event_loop().run_until_complete(t) if not t.done() else None

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
        """...but the PRIMARY inline and the rest in the background."""
        asked = []
        live = self._live(int(time.time() * 1000) + 300_000)
        b = self._bot(asked, live)

        async def go():
            await b.tick()
            t = getattr(b, "_round_refresh", None)
            if t is not None:
                await t
        asyncio.run(go())
        assert asked[0] == [MARKETS[0][0]], "the primary is fetched inline"
        swept = {p for call in asked for p in call}
        assert swept == {p for p, _, _ in MARKETS}, (
            "every family is swept, one prefix per call -- a single call for "
            "all of them couples their outcomes, so one family whose round is "
            "not listed leaves the rest uncached (all 3 of xrp/sol/doge were "
            "dead this way with 0 quotes/hour)")

    def test_the_deep_sweep_never_blocks_the_poll(self):
        """The whole point: a rolled family must not stall the decision loop."""
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot.tick)
        inline = src[src.index("self.market.rounds_by_prefixes("):]
        inline = inline[:inline.index(")")]
        assert "primary" in inline, "only the primary may be swept inline"
        assert "_refresh_rounds" in src and "create_task" in src

    def test_a_live_round_is_not_swept_again(self):
        asked = []
        live = self._live(int(time.time() * 1000) + 300_000)
        b = self._bot(asked, live)
        async def go():
            await b.tick()
            t = getattr(b, "_round_refresh", None)
            if t is not None:
                await t
            await b.tick()
            t = getattr(b, "_round_refresh", None)
            if t is not None and not t.done():
                await t
        asyncio.run(go())
        # asked == [[primary], [the five others, in the background], [primary]]
        assert asked[-1] == [MARKETS[0][0]], (
            "a family whose round is still live must not be paged for again; "
            "only the primary is refetched every poll")

    def test_a_rolled_round_is_swept_again(self):
        asked = []
        live = self._live(int(time.time() * 1000) + 300_000)
        b = self._bot(asked, live)
        async def go():
            await b.tick()
            t = getattr(b, "_round_refresh", None)
            if t is not None:
                await t
            for r in b._round_cache.values():
                r.end_ms = int(time.time() * 1000) - 1
            await b.tick()
            t = getattr(b, "_round_refresh", None)
            if t is not None and not t.done():
                await t
        asyncio.run(go())
        assert len(asked[-1]) == 1, (
            "once a round has rolled its family must be swept again")
        assert asked[-1] == [p for p, _, _ in MARKETS if p != MARKETS[0][0]][-1] \
            or asked[-1][0] in [p for p, _, _ in MARKETS if p != MARKETS[0][0]]

    def test_a_round_without_a_strike_is_never_cached(self):
        """A strikeless round cannot be traded and must not pin the cache."""
        asked = []
        live = self._live(int(time.time() * 1000) + 300_000)
        for r in live.values():
            r.strike = None
        b = self._bot(asked, live)
        async def go():
            await b.tick()
            t = getattr(b, "_round_refresh", None)
            if t is not None:
                await t
            await b.tick()
            t = getattr(b, "_round_refresh", None)
            if t is not None and not t.done():
                await t
        asyncio.run(go())
        swept = {p for call in asked for p in call}
        assert swept == {p for p, _, _ in MARKETS}, (
            "every family must be swept, one prefix per call")
        assert all(len(call) == 1 for call in asked[1:]), \
            "non-primary sweeps are per-prefix so one miss cannot block the rest"


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
        """`_ck` reads the round it is handed, never `self._round`.

        This was proven with `max_round_range_bps`, whose five per-family values
        were REFUTED and removed on 2026-09-18 (see
        config.cross_max_round_range_bps), so the proof moved to the two
        per-market families of knobs that still exist. The trap is unchanged: a
        `_ck` reading `self._round` would price sol -- one of the three families
        whose strike is RECONSTRUCTED -- with BTC's one-tick floor, and xrp with
        a 0.01 spot tick, which is a 78 bps floor against a 5 bps ceiling: an
        EMPTY band, i.e. an arm that can never fire and nothing that says so.
        """
        b = Bot.__new__(Bot)
        b._round = SimpleNamespace(slug="btc-updown-5m-1")
        sol = b._ck("min_depth_ticks", SimpleNamespace(slug="sol-updown-5m-1"))
        btc = b._ck("min_depth_ticks", SimpleNamespace(slug="btc-updown-5m-1"))
        assert sol != btc, "per-market overrides must actually apply"
        assert sol == 2.0 and btc == 1.0
        assert b._ck("min_depth_ticks") == btc, "None falls back to _round"
        xrp = b._ck("tick_size", SimpleNamespace(slug="xrp-updown-5m-1"))
        assert xrp == 0.0001 and b._ck("tick_size") == 0.01

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
