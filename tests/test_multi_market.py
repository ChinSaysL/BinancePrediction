"""Per-market cross knobs, and the websocket book following every market.

Both are the multi-market experiment's blockers, and both are the kind of
branch that a fixture can leave dead: the recorded-only families never reached
the ws-book read because it was gated on `trade`, and every knob was global, so
a test could pass while the market it named was ignored.
"""
from __future__ import annotations

import asyncio
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
        assert body.index("_cross_tick") < body.index('entry_mode == "off"'), \
            "turning the main arm off must not skip the cross arm"


class TestRecordedMarketsAreRefreshedSlowly:
    """Trade latency must not depend on an unrelated market's popularity."""

    def _bot(self, asked, follows=None):
        b = Bot.__new__(Bot)
        b.ws_book = SimpleNamespace(follow=lambda ids: None)
        b.market = SimpleNamespace(
            rounds_by_prefixes=lambda prefixes: _capture(prefixes, asked))
        b.refresh_vol_if_stale = _noop
        b._record = _noop_record
        return b

    def test_first_tick_sweeps_everything(self):
        asked = []
        asyncio.run(self._bot(asked).tick())
        assert len(asked[0]) == len(MARKETS)

    def test_the_next_tick_asks_only_for_the_traded_family(self):
        asked = []
        b = self._bot(asked)
        asyncio.run(b.tick())
        asyncio.run(b.tick())
        assert asked[1] == [MARKETS[0][0]], \
            "a quiet recorded market must not be paged for on every poll"

    def test_it_sweeps_again_once_the_interval_lapses(self):
        from btcpredict.config import RECORDED_REFRESH_S
        asked = []
        b = self._bot(asked)
        asyncio.run(b.tick())
        b._recorded_at -= RECORDED_REFRESH_S + 1
        asyncio.run(b.tick())
        assert len(asked[1]) == len(MARKETS)


def _capture(prefixes, sink):
    sink.append(list(prefixes))
    async def _c():
        return {}
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
