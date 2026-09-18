"""The Polymarket round source, and the one property that must never break.

`main.py` settles a real position by asking its round source for the winner.
Getting the WRONG round back does not raise -- it books the wrong P&L on money
that was actually staked. So `by_topic_id` refuses instead of answering, and
`by_slug` reads Polymarket's own resolution rather than the wrapper's.
"""
from __future__ import annotations

import asyncio

import pytest

from btcpredict.feeds.market_feed import MarketFeed, Round
from btcpredict.venues.polymarket import markets as M
from btcpredict.venues.polymarket import feed as F

SLUG = "btc-updown-5m-1789577400"
START = 1789577400
UP = "1" * 40
DOWN = "2" * 40


def pm_round(**kw) -> M.PmRound:
    base = dict(slug=SLUG, start_ts=START, condition_id="0xcond",
                up_token=UP, down_token=DOWN, tick=0.01, min_order=5.0,
                end_ms=(START + 300) * 1000, neg_risk=False, liquidity=1.0)
    base.update(kw)
    return M.PmRound(**base)


class FakeStrike:
    """Stands in for the Binance feed, which supplies ONE field.

    It returns a full round, `slug` included, because the real one does -- and
    the slug is the only thing that proves the answer belongs to the round we
    asked about. The wrapper feed has no slug lookup, so it always answers with
    whichever round is currently live (verified live 2026-09-16: the wrapper's
    live slug matched Polymarket's current round and did NOT match its next).
    """

    def __init__(self, strike=75832.5, boom=False, slug=SLUG):
        self.strike = strike
        self.boom = boom
        self.slug = slug
        self.asked: list[list[str]] = []

    async def rounds_by_prefixes(self, prefixes):
        self.asked.append(list(prefixes))
        if self.boom:
            raise RuntimeError("wrapper feed down")
        return {prefixes[0]: type("R", (), {"strike": self.strike,
                                            "slug": self.slug})()}


def make(monkeypatch, *, strike_feed=None, cur=None, nxt=None,
         winner=None, status="resolved", **kw) -> F.PmMarketFeed:
    # Per-FAMILY now: `rounds_by_prefixes` asks for the round of EACH prefix it
    # was handed, so a one-argument stub is called with two.
    monkeypatch.setattr(M, "current_and_next",
                        lambda now=None, series=None: (cur, nxt))
    monkeypatch.setattr(M, "fetch_round",
                        lambda ts, series=None: kw.get("fetched", cur))
    # `by_slug` goes through `fetch_round_any`, which falls back to the CLOSED
    # view. Stubbing only `fetch_round` would send every uncached test case to
    # the real network. Both take the SERIES as well as the timestamp now.
    monkeypatch.setattr(M, "fetch_round_any",
                        lambda ts, series=None: kw.get("fetched", cur))
    monkeypatch.setattr(M, "settled_winner",
                        lambda ts, series=None: (winner, status))
    return F.PmMarketFeed(strike_feed=strike_feed, **{k: v for k, v in kw.items()
                                                      if k == "recheck_s"})


class TestDropInShape:
    def test_it_exposes_the_methods_main_py_calls(self):
        # Discovery uses rounds_by_prefixes; settlement re-fetches per round.
        for name in ("live_round", "rounds_by_prefixes", "by_slug",
                     "by_topic_id"):
            assert callable(getattr(F.PmMarketFeed, name)), name

    def test_rounds_by_prefixes_returns_the_arms_own_Round_type(self, monkeypatch):
        src = make(monkeypatch, cur=pm_round())
        got = asyncio.run(src.rounds_by_prefixes(["btc-updown-5m"]))
        assert set(got) == {"btc-updown-5m"}
        assert isinstance(got["btc-updown-5m"], Round)
        assert got["btc-updown-5m"].up.token_id == UP

    def test_a_prefix_outside_this_round_is_ignored(self, monkeypatch):
        # The guard is the SLUG CHECK, not the venue's coverage: Polymarket
        # does list eth/bnb updown markets, so "BTC only" is no longer
        # structural -- what stops a foreign prefix entering the map is that
        # the returned round's slug has to start with it.
        src = make(monkeypatch, cur=pm_round())
        assert asyncio.run(src.rounds_by_prefixes(["eth-updown-5m"])) == {}

    def test_every_requested_family_is_discovered(self, monkeypatch):
        """The venue lists eth/bnb updown markets alongside BTC's and the arms
        trade all six. Asking ONE fixed series for every prefix returned BTC
        alone, which left five families with no round, no book and a permanent
        refusal ("no fresh websocket book off-primary")."""
        eth = pm_round(slug="eth-updown-5m-1789577400", start_ts=START)

        def cn(now=None, series=None):
            return (eth if (series or "").startswith("eth") else pm_round(),
                    None)

        monkeypatch.setattr(M, "current_and_next", cn)
        src = F.PmMarketFeed()
        got = asyncio.run(src.rounds_by_prefixes(["btc-updown-5m",
                                                  "eth-updown-5m"]))
        assert set(got) == {"btc-updown-5m", "eth-updown-5m"}
        assert got["eth-updown-5m"].slug == "eth-updown-5m-1789577400"

    def test_the_current_round_wins_over_the_next(self, monkeypatch):
        """One round per family: the arms price the LIVE round and the next
        one has no book to read. It is still DISCOVERED (that is what
        `current_and_next` is for); it just does not become the entry."""
        nxt = pm_round(slug="btc-updown-5m-1789577700", start_ts=START + 300)
        src = make(monkeypatch, cur=pm_round(), nxt=nxt)
        got = asyncio.run(src.rounds_by_prefixes(["btc-updown-5m"]))
        assert got["btc-updown-5m"].slug == SLUG


class TestTheStrikeComesFromTheWrapper:
    def test_it_is_taken_from_the_injected_feed(self, monkeypatch):
        sf = FakeStrike(75832.5)
        src = make(monkeypatch, strike_feed=sf, cur=pm_round())
        got = asyncio.run(src.rounds_by_prefixes(["btc-updown-5m"]))
        assert got["btc-updown-5m"].strike == pytest.approx(75832.5)
        assert sf.asked == [["btc-updown-5m"]]

    def test_without_a_feed_the_strike_is_unknown_not_zero(self, monkeypatch):
        src = make(monkeypatch, cur=pm_round())
        got = asyncio.run(src.rounds_by_prefixes(["btc-updown-5m"]))
        assert got["btc-updown-5m"].strike is None

    def test_a_wrapper_failure_yields_unknown_rather_than_raising(self, monkeypatch):
        # A dead strike feed must not stop the round from being discovered --
        # the arms that need a strike refuse on their own.
        src = make(monkeypatch, strike_feed=FakeStrike(boom=True),
                   cur=pm_round())
        got = asyncio.run(src.rounds_by_prefixes(["btc-updown-5m"]))
        assert got and got["btc-updown-5m"].strike is None

    def test_the_strike_is_fetched_once_per_slug(self, monkeypatch):
        sf = FakeStrike()
        src = make(monkeypatch, strike_feed=sf, cur=pm_round())
        asyncio.run(src.rounds_by_prefixes(["btc-updown-5m"]))
        asyncio.run(src.rounds_by_prefixes(["btc-updown-5m"]))
        assert len(sf.asked) == 1

    def test_a_strike_for_another_round_is_refused(self, monkeypatch):
        """A wrong strike is worse than none: `crossed_side` gates on
        `strike is None`, so a borrowed level makes the arm look for a
        crossing of a price this market was never settled against."""
        sf = FakeStrike(75832.5, slug="btc-updown-5m-1789577700")
        src = make(monkeypatch, strike_feed=sf, cur=pm_round())
        got = asyncio.run(src.rounds_by_prefixes(["btc-updown-5m"]))
        assert got["btc-updown-5m"].strike is None

    def test_a_miss_is_retried_then_refilled(self, monkeypatch):
        """The answer depends on WHICH round is live, and a round is wrapped
        when it is discovered -- before it is the live one. Caching the miss
        would freeze that round at "unknown" for its whole life, which is
        exactly what main.py's `strike is None` staleness rule asks us to fix.
        """
        sf = FakeStrike(75832.5, slug="btc-updown-5m-1789577700")
        src = make(monkeypatch, strike_feed=sf, cur=pm_round(), recheck_s=0)
        assert asyncio.run(src.rounds_by_prefixes(["btc-updown-5m"]))[
            "btc-updown-5m"].strike is None

        sf.slug = SLUG                      # the wrapper rolls to our round
        assert asyncio.run(src.rounds_by_prefixes(["btc-updown-5m"]))[
            "btc-updown-5m"].strike == pytest.approx(75832.5)

    def test_the_retry_is_throttled_and_bounded(self, monkeypatch):
        """main.py re-fetches the primary family EVERY POLL, and one ask is a
        paginated sweep of the wrapper's live list, so an unthrottled retry
        would sweep once a second forever."""
        sf = FakeStrike(75832.5, slug="btc-updown-5m-1789577700")
        src = make(monkeypatch, strike_feed=sf, cur=pm_round())   # recheck 5s
        for _ in range(20):
            asyncio.run(src.rounds_by_prefixes(["btc-updown-5m"]))
        assert len(sf.asked) == 1                    # throttled to one ask

        src._strike_checked.clear()                  # clock advances
        for _ in range(40):
            src._strike_checked.clear()              # ...and again
            asyncio.run(src.rounds_by_prefixes(["btc-updown-5m"]))
        # The cap is on TOTAL asks, not on retries, so the one ask spent above
        # counts against it.
        assert len(sf.asked) == src._STRIKE_MAX_TRIES


class TestSettlementNeverBorrowsTheWrongAnswer:
    def test_by_topic_id_refuses_instead_of_answering(self, monkeypatch):
        """A Binance marketTopicId means nothing here.

        Answering numerically would return an unrelated market whose winner
        would then be booked against a real position.
        """
        src = make(monkeypatch, cur=pm_round())
        assert asyncio.run(src.by_topic_id(12345)) is None
        assert asyncio.run(src.by_topic_id(0)) is None
        assert asyncio.run(src.by_topic_id(None)) is None

    def test_an_uncached_ended_round_still_resolves(self, monkeypatch):
        """A round settles AFTER the bell, when the venue has stopped listing
        it as open -- and the feed's cache is empty on every restart. The
        open-only lookup returning None therefore made the round unresolvable
        for the life of the process, on the MONEY path (`settle_due`), which is
        the same silent death as a fill that is never booked.
        """
        src = make(monkeypatch, cur=pm_round(), winner="Up")
        monkeypatch.setattr(M, "fetch_round", lambda ts, series=None: None)
        called: list[float] = []

        def any_round(ts, series=None):
            called.append(ts)
            return pm_round()

        monkeypatch.setattr(M, "fetch_round_any", any_round)
        r = asyncio.run(src.by_slug(SLUG))
        assert r is not None and r.winner_name == "Up"
        assert called == [START]

    def test_by_slug_uses_polymarkets_own_resolution(self, monkeypatch):
        # NOTE the order: `make()` installs its own stub, so the test's must be
        # applied AFTER construction.
        src = make(monkeypatch, cur=pm_round())
        seen: list[float] = []

        def resolved(ts, series=None):
            seen.append(ts)
            return "Down", "resolved"

        monkeypatch.setattr(M, "settled_winner", resolved)
        r = asyncio.run(src.by_slug(SLUG))
        assert r is not None
        assert r.winner_name == "Down"
        assert seen == [START]

    def test_an_unresolved_round_has_no_winner(self, monkeypatch):
        src = make(monkeypatch, cur=pm_round(), winner=None,
                   status="closed")
        r = asyncio.run(src.by_slug(SLUG))
        assert r is not None and r.winner_name is None

    def test_a_settled_round_is_never_re_read(self, monkeypatch):
        src = make(monkeypatch, cur=pm_round())
        calls: list[float] = []

        def resolved(ts, series=None):
            calls.append(ts)
            return "Up", "resolved"

        monkeypatch.setattr(M, "settled_winner", resolved)
        first = asyncio.run(src.by_slug(SLUG))
        second = asyncio.run(src.by_slug(SLUG))
        assert first.winner_name == second.winner_name == "Up"
        assert len(calls) == 1

    def test_an_unresolved_round_is_not_re_asked_every_tick(self, monkeypatch):
        # settle_due runs every ~100 ms and resolution takes ~58 s after the
        # bell, so an unthrottled re-read is ~600 pointless gamma calls/round.
        src = make(monkeypatch, cur=pm_round(), recheck_s=60.0)
        calls: list[float] = []

        def unresolved(ts, series=None):
            calls.append(ts)
            return None, "closed"

        monkeypatch.setattr(M, "settled_winner", unresolved)
        for _ in range(5):
            asyncio.run(src.by_slug(SLUG))
        assert len(calls) == 1

    def test_a_malformed_slug_is_rejected_without_a_call(self, monkeypatch):
        src = make(monkeypatch, cur=pm_round())
        assert asyncio.run(src.by_slug("not-a-slug")) is None
        assert asyncio.run(src.by_slug("")) is None

    def test_prune_releases_settled_rounds(self, monkeypatch):
        src = make(monkeypatch, cur=pm_round(), winner="Up")
        asyncio.run(src.by_slug(SLUG))
        assert src._rounds
        src.prune(keep=[])
        assert src._rounds == {} and src._strikes == {}


class TestItMatchesTheWrapperFeedsInterface:
    def test_both_sources_share_the_two_methods_main_py_calls(self):
        for name in ("rounds_by_prefixes", "by_topic_id"):
            assert callable(getattr(MarketFeed, name)), name
            assert callable(getattr(F.PmMarketFeed, name)), name

    def test_only_the_pm_source_offers_by_slug(self):
        # `_resolve_round` keys off exactly this capability.
        assert hasattr(F.PmMarketFeed, "by_slug")
        assert not hasattr(MarketFeed, "by_slug")
