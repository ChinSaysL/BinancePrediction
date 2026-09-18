"""Polymarket rounds mapped into the arms' own `Round` type.

The bug this file is written against is SILENT, not loud. `main.py` opens with
`up, down = r.up, r.down` and then `token = up.token_id if ... else
down.token_id`. If `r.up` is None, nothing raises -- the arm simply evaluates
against nothing and the bot trades zero rounds while looking healthy. So the
tests that matter here are "does `.up` exist and carry the venue's token id".
"""
from __future__ import annotations

import pytest

from btcpredict.feeds.market_feed import Round
from btcpredict.venues.polymarket import markets as M
from btcpredict.venues.polymarket import rounds as R

SLUG = "btc-updown-5m-1789577400"
START = 1789577400
UP = "1" * 40
DOWN = "2" * 40


def pm(**kw) -> M.PmRound:
    base = dict(slug=SLUG, start_ts=START, condition_id="0xcond",
                up_token=UP, down_token=DOWN, tick=0.01, min_order=5.0,
                end_ms=(START + 300) * 1000, neg_risk=False, liquidity=50_000.0)
    base.update(kw)
    return M.PmRound(**base)


class TestTheShapeTheArmsActuallyRead:
    def test_it_is_the_arms_own_Round_type(self):
        # Not a lookalike: `isinstance` is what a future refactor would break.
        assert isinstance(R.to_round(pm()), Round)

    def test_up_and_down_exist_and_carry_the_venues_token_ids(self):
        # This is the whole point of the module. None here = silence.
        r = R.to_round(pm())
        assert r.up is not None and r.down is not None
        assert r.up.token_id == UP
        assert r.down.token_id == DOWN

    def test_the_outcome_names_match_the_case_the_venue_uses(self):
        # `Round.outcome()` lowercases both sides, so "Up"/"Down" is safe; a
        # "YES"/"NO" here would make r.up None.
        r = R.to_round(pm())
        assert r.outcome("up") is r.up
        assert r.outcome("DOWN") is r.down

    def test_tau_counts_down_to_the_rounds_end(self):
        r = R.to_round(pm())
        assert r.tau_s(r.start_ms + 60_000) == pytest.approx(240.0)
        assert r.tau_s(r.end_ms) == pytest.approx(0.0)

    def test_the_slug_and_dates_survive(self):
        r = R.to_round(pm())
        assert r.slug == SLUG
        assert r.start_ms == START * 1000
        assert r.end_ms == (START + 300) * 1000


class TestWhatMustNotBeInvented:
    def test_the_strike_is_None_unless_supplied(self):
        """Polymarket publishes NO reference price for this family -- the
        resolution source is the Chainlink stream. The honest source is the
        wrapper's startPrice, which both rules compare against. Guessing a
        strike would silently mis-price every trigger."""
        assert R.to_round(pm()).strike is None
        assert R.to_round(pm(), strike=75832.5).strike == pytest.approx(75832.5)

    def test_the_market_id_is_None_not_a_fake_binance_id(self):
        # `market_id` belongs to the wrapper's id space. It is threaded into
        # best_prices() and ignored by the Polymarket broker, but putting a
        # Binance number here would read like a lookup failure later.
        assert R.to_round(pm()).market_id is None

    def test_the_resolution_rule_is_recorded_on_every_round(self):
        # so no later analysis has to re-derive which variable a fill was paid on
        assert R.to_round(pm()).feed_provider == M.RESOLUTION
        assert "twap" in R.to_round(pm()).feed_provider

    def test_the_symbol_is_the_one_the_arms_filter_on(self):
        assert R.to_round(pm()).symbol == "BTCUSDT"
        assert R.to_round(pm(), symbol="ETHUSDT").symbol == "ETHUSDT"


class TestSettlementUsesPolymarketNotTheWrapper:
    def test_it_asks_gamma_for_the_strict_winner(self, monkeypatch):
        """A fill placed on Polymarket is PAID by Polymarket's rule.

        Settling against the wrapper's point winner books P&L against a
        different bet ~1 round in 10 (measured: 43 same / 5 different).
        """
        seen: list[int] = []

        def fake(ts):
            seen.append(ts)
            return "Down"

        monkeypatch.setattr(M, "settled_winner_strict", fake)
        assert R.settle_winner(pm()) == "Down"
        assert seen == [START]

    def test_an_unresolved_round_yields_None(self, monkeypatch):
        # Booking against a closed-but-unresolved market would invent P&L.
        monkeypatch.setattr(M, "settled_winner_strict", lambda ts: None)
        assert R.settle_winner(pm()) is None


class TestTheRoundSource:
    def test_a_malformed_slug_is_rejected_without_a_network_call(self):
        assert R.PmRoundSource().for_slug("not-a-slug") is None
        assert R.PmRoundSource().for_slug("") is None

    def test_a_cached_round_is_returned_without_refetching(self, monkeypatch):
        src = R.PmRoundSource()
        src._cache[SLUG] = R.to_round(pm(), strike=1.0)

        def boom(ts):                                   # pragma: no cover
            raise AssertionError("should not fetch a cached slug")

        monkeypatch.setattr(M, "fetch_round", boom)
        got = src.for_slug(SLUG)
        assert got is not None and got.strike == pytest.approx(1.0)

    def test_current_and_next_skips_a_missing_side(self, monkeypatch):
        monkeypatch.setattr(M, "current_and_next",
                            lambda now=None: (pm(), None))
        got = R.PmRoundSource().current_and_next()
        assert len(got) == 1 and got[0].slug == SLUG

    def test_the_strike_hook_is_used_when_given(self, monkeypatch):
        # The strike is the ONE field this venue cannot supply, so it is an
        # injected hook rather than a guess buried in the converter.
        monkeypatch.setattr(M, "current_and_next", lambda now=None: (pm(), None))
        got = R.PmRoundSource(strike_of=lambda r: 75832.5).current_and_next()
        assert got and got[0].strike == pytest.approx(75832.5)

    def test_without_a_hook_the_strike_stays_unknown(self, monkeypatch):
        monkeypatch.setattr(M, "current_and_next", lambda now=None: (pm(), None))
        got = R.PmRoundSource().current_and_next()
        assert got and got[0].strike is None

    def test_prune_drops_rounds_we_will_never_trade_again(self):
        src = R.PmRoundSource()
        src._cache["a"] = R.to_round(pm(), strike=1.0)
        src._cache["b"] = R.to_round(pm(), strike=2.0)
        src.prune(keep=["a"])
        assert list(src._cache) == ["a"]
        src.prune()
        assert src._cache == {}
