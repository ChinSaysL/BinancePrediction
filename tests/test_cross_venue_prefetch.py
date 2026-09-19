"""The venue-ask prefetch: ~292ms off the order path, and it must not move a price.

WHY THIS FILE EXISTS. With the bookkeeping dispatched off the tick, the order
path became the largest remaining stall: decision -> verdict 1001ms median,
1999ms p90, measured over the live log 2026-09-19. Only two of its steps are
network -- `venue_top` at ~292ms and the FAK itself at 452ms median -- and the
FAK is the order, so it is irreducible. `venue_top` is not: it has to finish
before the FAK can be priced, but nothing says it has to START at the decision.

THE GUARD THAT MATTERS MOST is `test_a_warm_ask_does_not_claim_the_book_was_
unreadable`. On the warm path the synchronous fetch never runs, so `vt` stays
None -- and the `elif` that reports an unreadable venue book keys off exactly
that. Without the `_warm is None` guard a SUCCESSFUL prefetch logs the line
whose whole job is to report the opposite, and it is the line the fail-closed
`cross_ladder_band_pad` check is read against.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from btcpredict.config import Settings
from btcpredict.main import Bot


class TestTheKnob:
    def test_it_ships_off(self) -> None:
        """0 = the old path, byte for byte. This is unmeasured in live."""
        assert Settings.model_fields[
            "cross_venue_prefetch_max_age_s"].default == 0.0

    def test_off_means_nothing_is_cached_and_nothing_is_taken(
            self, monkeypatch) -> None:
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_venue_prefetch_max_age_s", 0.0)
        b = Bot.__new__(Bot)
        b._vtop_pre = {"tok": (time.monotonic(), 0.61)}
        assert b._take_prefetched_venue_top("tok") is None, (
            "with the knob off the cache must not be consulted at all")


class TestTakingTheWarmAsk:
    def _bot(self, monkeypatch, *, max_age=0.5):
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_venue_prefetch_max_age_s", max_age)
        b = Bot.__new__(Bot)
        b._vtop_pre = {}
        return b

    def test_a_fresh_ask_is_returned(self, monkeypatch) -> None:
        b = self._bot(monkeypatch)
        b._vtop_pre["tok"] = (time.monotonic(), 0.67)
        assert b._take_prefetched_venue_top("tok") == pytest.approx(0.67)

    def test_it_is_one_shot(self, monkeypatch) -> None:
        """A retry must not build its ceiling from a base the first attempt
        already spent, and which is now a whole round older."""
        b = self._bot(monkeypatch)
        b._vtop_pre["tok"] = (time.monotonic(), 0.67)
        assert b._take_prefetched_venue_top("tok") == pytest.approx(0.67)
        assert b._take_prefetched_venue_top("tok") is None

    def test_a_stale_ask_is_refused(self, monkeypatch) -> None:
        """⛔ THE FAILURE MODE THIS BOUND EXISTS FOR. A base that is too LOW
        builds a ceiling under the ask that exists and the FAK finds nothing --
        the exact miss `cross_venue_take` was added to fix. The venue ask moves
        a median 0.070 per ~320ms, so an old prefetch gives that back."""
        b = self._bot(monkeypatch, max_age=0.2)
        b._vtop_pre["tok"] = (time.monotonic() - 5.0, 0.67)
        assert b._take_prefetched_venue_top("tok") is None

    def test_a_missing_entry_is_not_an_error(self, monkeypatch) -> None:
        b = self._bot(monkeypatch)
        assert b._take_prefetched_venue_top("never-fetched") is None


class TestTheSourceOfTruth:
    def test_a_warm_ask_does_not_claim_the_book_was_unreadable(self) -> None:
        """⭐ See this file's docstring. `vt` is None on the warm path because
        the synchronous fetch never ran -- NOT because the venue was
        unreadable. The two must not share a branch."""
        import ast
        import pathlib
        src = pathlib.Path(__file__).resolve().parents[1] / "btcpredict" / "main.py"
        text = src.read_text()
        i = text.index('log.info("take base %s %s: venue book unreadable')
        head = text[:i]
        j = head.rindex("elif")
        assert "_warm is None" in head[j:], (
            "the unreadable-book branch no longer requires that the warm path "
            "was NOT taken: a successful prefetch will now log that the venue "
            "book could not be read")

    def test_the_warm_path_still_marks_the_base_as_the_venue(self) -> None:
        """`base_src` is what the fail-closed pad check reads
        (`if base_src != "venue"`). A warm venue ask IS the venue's ask, so
        marking it anything else would make the pad refuse every prefetched
        order."""
        import pathlib
        text = (pathlib.Path(__file__).resolve().parents[1]
                / "btcpredict" / "main.py").read_text()
        i = text.index("_warm = (self._take_prefetched_venue_top")
        window = text[i:i + 900]
        assert 'base, base_src = _warm, "venue"' in window

    def test_the_ceiling_is_re_derived_from_the_warm_base(self) -> None:
        """`_take_cap_for` must re-run on the warm base exactly as it does on
        a synchronously fetched one, or the ceiling belongs to a price we are
        no longer using."""
        import pathlib
        text = (pathlib.Path(__file__).resolve().parents[1]
                / "btcpredict" / "main.py").read_text()
        i = text.index("_warm = (self._take_prefetched_venue_top")
        window = text[i:i + 900]
        assert "cap = self._take_cap_for(r, base, fill_cap)" in window


class TestFillingTheCache:
    def test_it_does_nothing_when_the_knob_is_off(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_venue_prefetch_max_age_s", 0.0)
        b = Bot.__new__(Bot)
        b.broker = type("B", (), {"mode": "live"})()
        out = asyncio.run(b._prefetch_venue_top(None, None, None, None,
                                                margin_bps=0.0))
        assert out is None

    def test_it_does_nothing_when_venue_take_is_off(self, monkeypatch) -> None:
        """Nothing consumes the warm ask unless the re-base is on, so fetching
        it would be a REST call per tick for no reader."""
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_venue_prefetch_max_age_s", 0.5)
        monkeypatch.setattr("btcpredict.config.settings.cross_venue_take", False)
        b = Bot.__new__(Bot)
        b.broker = type("B", (), {"mode": "live"})()
        out = asyncio.run(b._prefetch_venue_top(None, None, None, None,
                                                margin_bps=0.0))
        assert out is None

    def test_paper_mode_never_probes_the_live_venue(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_venue_prefetch_max_age_s", 0.5)
        monkeypatch.setattr("btcpredict.config.settings.cross_venue_take", True)
        b = Bot.__new__(Bot)
        b.broker = type("B", (), {"mode": "paper"})()
        out = asyncio.run(b._prefetch_venue_top(None, None, None, None))
        assert out is None


class TestItActuallyFetches:
    """The half the structural tests cannot see: does a warm ask appear?"""

    def _wire(self, monkeypatch, *, spot, strike, asks, max_age=0.5):
        from types import SimpleNamespace as NS
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_venue_prefetch_max_age_s", max_age)
        monkeypatch.setattr("btcpredict.config.settings.cross_venue_take", True)
        # ⛔ THESE TESTS ARE ABOUT THE POLYMARKET PATH. `Bot._venue_top`
        # dispatches on `settings.venue`, so without this the arm takes the
        # BINANCE branch (`LiveBroker.best_prices`) and the monkeypatched
        # `venue_top` is never called -- the test then passes or fails for
        # reasons that have nothing to do with what it is checking.
        monkeypatch.setattr("btcpredict.config.settings.venue", "polymarket")
        calls = []

        async def fake_top(token):
            calls.append(token)
            a = asks.get(token)
            return NS(ask=a, bid=a - 0.01) if a else None

        monkeypatch.setattr("btcpredict.main.venue_top", fake_top)
        b = Bot.__new__(Bot)
        b.broker = type("B", (), {"mode": "live"})()
        b._vtop_pre = {}
        r = NS(strike=strike, slug="btc-updown-5m-1")
        up, down = NS(token_id="UP"), NS(token_id="DOWN")
        feed = NS(price=spot)
        return b, r, up, down, feed, calls

    def _run(self, b, r, up, down, feed, margin=0.0):
        async def go():
            t = await b._prefetch_venue_top(r, up, down, feed,
                                            margin_bps=margin)
            if t is not None:
                await t
        asyncio.run(go())

    def test_below_the_strike_it_warms_the_UP_token(self, monkeypatch) -> None:
        """The side is knowable before the crossing -- that is the whole
        premise. Below the strike the next cross is Up."""
        b, r, up, down, feed, calls = self._wire(
            monkeypatch, spot=99.0, strike=100.0, asks={"UP": 0.61})
        self._run(b, r, up, down, feed)
        assert calls == ["UP"]
        assert b._take_prefetched_venue_top("UP") == pytest.approx(0.61)

    def test_above_the_strike_it_warms_the_DOWN_token(self, monkeypatch) -> None:
        b, r, up, down, feed, calls = self._wire(
            monkeypatch, spot=101.0, strike=100.0, asks={"DOWN": 0.58})
        self._run(b, r, up, down, feed)
        assert calls == ["DOWN"]
        assert b._take_prefetched_venue_top("DOWN") == pytest.approx(0.58)

    def test_exactly_at_the_strike_it_does_nothing(self, monkeypatch) -> None:
        """No side to predict, so no call to make."""
        b, r, up, down, feed, calls = self._wire(
            monkeypatch, spot=100.0, strike=100.0, asks={"UP": 0.61})
        self._run(b, r, up, down, feed)
        assert calls == []

    def test_an_unreadable_venue_caches_nothing(self, monkeypatch) -> None:
        """A failed probe must leave the cache EMPTY, so the order path falls
        through to the synchronous fetch rather than to a phantom."""
        b, r, up, down, feed, calls = self._wire(
            monkeypatch, spot=99.0, strike=100.0, asks={})
        self._run(b, r, up, down, feed)
        assert calls == ["UP"]
        assert b._vtop_pre == {}
        assert b._take_prefetched_venue_top("UP") is None

    def test_a_raising_probe_is_swallowed(self, monkeypatch) -> None:
        """It runs on a task of its own; an exception there must not surface
        in the arm, and must not poison the cache."""
        from types import SimpleNamespace as NS
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_venue_prefetch_max_age_s", 0.5)
        monkeypatch.setattr("btcpredict.config.settings.cross_venue_take", True)
        monkeypatch.setattr("btcpredict.config.settings.venue", "polymarket")

        async def boom(_token):
            raise RuntimeError("venue said no")

        monkeypatch.setattr("btcpredict.main.venue_top", boom)
        b = Bot.__new__(Bot)
        b.broker = type("B", (), {"mode": "live"})()
        b._vtop_pre = {}
        self._run(b, NS(strike=100.0, slug="s"), NS(token_id="UP"),
                  NS(token_id="DOWN"), NS(price=99.0))
        assert b._vtop_pre == {}

    def test_a_still_fresh_entry_is_not_refetched(self, monkeypatch) -> None:
        """One REST call per staleness window, not one per tick: at a 1s poll
        an unguarded prefetch would be a probe every second per family."""
        b, r, up, down, feed, calls = self._wire(
            monkeypatch, spot=99.0, strike=100.0, asks={"UP": 0.61})
        self._run(b, r, up, down, feed)
        self._run(b, r, up, down, feed)
        self._run(b, r, up, down, feed)
        assert calls == ["UP"], "only the first tick may pay the round trip"

    def test_the_cache_cannot_grow_without_bound(self, monkeypatch) -> None:
        """The arm trades 9 families x 2 sides; anything past that is dead
        rounds, and a dict that only ever grows is a leak for the life of the
        process."""
        b, r, up, down, feed, _ = self._wire(
            monkeypatch, spot=99.0, strike=100.0, asks={"UP": 0.61})
        now = time.monotonic()
        for i in range(200):
            b._vtop_pre["tok%d" % i] = (now - i, 0.5)
        self._run(b, r, up, down, feed)
        assert len(b._vtop_pre) <= 64


class TestTheImminenceGate:
    """⛔ THE DEFECT THAT SENT v1 STRAIGHT BACK OFF.

    v1 had no such gate: it fired on EVERY no-crossing tick, at a dedup of
    `max_age * 0.5` = 0.175s, which is ~19 REST probes a second against the
    ~9-12 an HOUR the order path pays. The tau window and the `_cross_fired`
    slot are already spent before the prefetch runs, so the only question left
    is whether a crossing is actually NEAR -- and the margin is that question.
    """

    def _bot(self, monkeypatch, *, band, calls):
        from types import SimpleNamespace as NS
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_venue_prefetch_max_age_s", 0.5)
        monkeypatch.setattr("btcpredict.config.settings.cross_venue_take", True)
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_venue_prefetch_margin_bps", band)
        monkeypatch.setattr("btcpredict.config.settings.venue", "polymarket")

        async def fake_top(token):
            calls.append(token)
            return NS(ask=0.61, bid=0.60)

        monkeypatch.setattr("btcpredict.main.venue_top", fake_top)
        b = Bot.__new__(Bot)
        b.broker = type("B", (), {"mode": "live"})()
        b._vtop_pre = {}
        return b, NS(strike=100.0, slug="s"), NS(token_id="UP"), NS(token_id="DOWN"), NS(price=99.0)

    def _run(self, b, r, up, down, feed, margin):
        async def go():
            t = await b._prefetch_venue_top(r, up, down, feed, margin_bps=margin)
            if t is not None:
                await t
        asyncio.run(go())

    def test_a_near_crossing_is_prefetched(self, monkeypatch) -> None:
        calls = []
        b, r, up, down, feed = self._bot(monkeypatch, band=2.0, calls=calls)
        self._run(b, r, up, down, feed, margin=1.5)
        assert calls == ["UP"]

    def test_a_far_signal_is_not(self, monkeypatch) -> None:
        """This is the traffic v1 spent: 76% of ticks sit outside 2 bps."""
        calls = []
        b, r, up, down, feed = self._bot(monkeypatch, band=2.0, calls=calls)
        self._run(b, r, up, down, feed, margin=25.0)
        assert calls == []

    def test_the_band_is_two_sided(self, monkeypatch) -> None:
        """A crossing is near from either side of the level."""
        calls = []
        b, r, up, down, feed = self._bot(monkeypatch, band=2.0, calls=calls)
        self._run(b, r, up, down, feed, margin=-1.5)
        assert calls == ["UP"]

    def test_an_unknown_margin_does_not_spend_a_round_trip(self, monkeypatch) -> None:
        """No level, no answer -- and 'I cannot tell' must not mean 'probe'."""
        calls = []
        b, r, up, down, feed = self._bot(monkeypatch, band=2.0, calls=calls)
        self._run(b, r, up, down, feed, margin=None)
        assert calls == []

    def test_band_zero_means_no_gate(self, monkeypatch) -> None:
        """Explicitly recoverable, but it is what v1 did. The default is 2.0."""
        calls = []
        b, r, up, down, feed = self._bot(monkeypatch, band=0.0, calls=calls)
        self._run(b, r, up, down, feed, margin=999.0)
        assert calls == ["UP"]

    def test_the_shipped_band_is_not_zero(self) -> None:
        assert Settings.model_fields[
            "cross_venue_prefetch_margin_bps"].default == 2.0


class TestTheRefreshInterval:
    def test_it_is_its_own_knob_not_half_the_freshness_bound(self) -> None:
        """v1 reused `max_age * 0.5`, so a 0.35s bound refetched every 0.175s.
        'How old may a warm ask be' and 'how often may we ask' are unrelated."""
        import pathlib
        text = (pathlib.Path(__file__).resolve().parents[1]
                / "btcpredict" / "main.py").read_text()
        # The CODE, not the prose: the comment above it names the old bug on
        # purpose, so a bare substring search matches its own explanation.
        i = text.index("refresh = float(settings.cross_venue_prefetch_refresh_s)")
        window = text[i:i + 220]
        assert "time.monotonic() - e[0] < refresh" in window, (
            "the dedup no longer compares against the refresh interval")
        assert "max_age" not in window, (
            "the refresh interval is tied to the freshness bound again")
        assert Settings.model_fields[
            "cross_venue_prefetch_refresh_s"].default == 1.0

    def test_a_second_probe_inside_the_interval_is_refused(self, monkeypatch) -> None:
        from types import SimpleNamespace as NS
        calls = []
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_venue_prefetch_max_age_s", 5.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_venue_take", True)
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_venue_prefetch_margin_bps", 2.0)
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_venue_prefetch_refresh_s", 60.0)
        monkeypatch.setattr("btcpredict.config.settings.venue", "polymarket")

        async def fake_top(token):
            calls.append(token)
            return NS(ask=0.61, bid=0.60)

        monkeypatch.setattr("btcpredict.main.venue_top", fake_top)
        b = Bot.__new__(Bot)
        b.broker = type("B", (), {"mode": "live"})()
        b._vtop_pre = {}

        async def go():
            for _ in range(5):
                t = await b._prefetch_venue_top(
                    NS(strike=100.0, slug="s"), NS(token_id="UP"),
                    NS(token_id="DOWN"), NS(price=99.0), margin_bps=0.5)
                if t is not None:
                    await t
        asyncio.run(go())
        assert calls == ["UP"], "one probe per refresh interval, not per tick"


class TestTheRateIsMeasuredNotEstimated:
    def test_the_probe_count_is_logged(self) -> None:
        """⭐ v1 shipped on an ESTIMATED rate and came back off when the
        estimate turned out to be 19/s. The log states it now."""
        import pathlib
        text = (pathlib.Path(__file__).resolve().parents[1]
                / "btcpredict" / "main.py").read_text()
        assert "venue prefetch: %d probes" in text
        assert "/hr" in text[text.index("venue prefetch: %d probes"):
                             text.index("venue prefetch: %d probes") + 400]

    def test_consumption_is_counted_too(self) -> None:
        """Probes with no hits is the signature of a prefetch that is pure
        cost -- exactly what v1 was for the five minutes it ran."""
        import pathlib
        text = (pathlib.Path(__file__).resolve().parents[1]
                / "btcpredict" / "main.py").read_text()
        assert "_vtop_hit_n" in text
