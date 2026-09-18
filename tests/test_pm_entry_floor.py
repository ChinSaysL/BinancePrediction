"""The ENTRY PRICE FLOOR -- the 500-share 1c fill, and what now stops it.

MEASURED INCIDENT, 2026-09-16 17:29:42 UTC, round `btc-updown-5m-1789579500`.
The wallet's own record: `size 500 @ 0.01`, `entry_cost_usdc 5.0`,
`entry_fees_usdc 0.3465`. Three independent numbers, one arithmetic:

    shares = max(min_order, ceil(stake / limit)) = ceil(5.00 / 0.01) = 500
    cost   = 500 * 0.01                          = $5.00
    fee    = 0.07 * 0.01 * 0.99 * 500            = $0.3465      (EXACT)

`limit` was 0.01 because `take_limit_px` returns `max(0.01, ...)` -- a floor
meant for a PRICE, for the case where the valuation is missing. But the size is
DERIVED from that price, so a degenerate valuation became a 100x POSITION
rather than a bad price. The arm's own band (`cross_min_price`, 0.10) is checked
against the polled ASK and never inspected the price we POSTED, and
`shares * limit >= MIN_NOTIONAL` cannot catch it because that product is
`stake` by construction at every price. The round resolved Up; $5.35 went.

These tests pin the refusal at the broker -- the only thing that talks to the
venue -- on BOTH order paths, and they pin the SIZE the refusal avoided, so the
next reader can connect the guard to the fill it prevented.
"""
from __future__ import annotations

import asyncio
import math
import time

import pytest

from btcpredict.venues.polymarket.book import Touch
from btcpredict.venues.polymarket.broker import (DEFAULT_MIN_ENTRY_PX, PmBroker,
                                                 PmBrokerConfig,
                                                 _below_entry_floor, taker_fee)

SLUG = "btc-updown-5m-1789579500"
STAKE = 5.00        # the live `min_stake_usdt` at the time of the incident
ASK_1C = 0.01       # the price the order actually paid


class FakeRound:
    slug = SLUG
    tick = 0.01
    min_order = 5.0

    def token(self, side: str) -> str:
        return "tok-down" if side == "Down" else "tok-up"


class FakeBook:
    """Top of book as a single deep level, so the sweep price is the ask."""

    def __init__(self, ask: float, size: float = 5000.0):
        now = int(time.time() * 1000)
        self.touch = Touch(asset_id="tok-down", bid=max(0.0, ask - 0.01),
                           bid_size=size, ask=ask, ask_size=size,
                           ts_ms=now, recv_ms=now,
                           bids=((max(0.0, ask - 0.01), size),),
                           asks=((ask, size),))

    def best(self, token_id, max_age_s):        # noqa: ANN001
        return self.touch


def make_broker(ask: float, *, floor: float = DEFAULT_MIN_ENTRY_PX):
    logs: list[str] = []
    br = PmBroker(book=FakeBook(ask), client=None,
                  cfg=PmBrokerConfig(min_entry_px=floor),
                  log=logs.append, creds={})
    br.round_for = lambda slug: FakeRound()     # bypass discovery
    br.logs = logs
    return br


def plan_for(ask: float, **kw):
    br = make_broker(ask, **kw)
    return br, br.plan(slug=SLUG, side="Down", stake=STAKE)


# ------------------------------------------------------------ the incident

class TestTheMeasuredIncident:
    def test_the_order_is_identified_by_its_own_fee(self):
        """Three venue fields, one arithmetic -- nothing here is an opinion."""
        shares = math.ceil(STAKE / ASK_1C)
        assert shares == 500
        assert shares * ASK_1C == pytest.approx(5.00)
        assert taker_fee(ASK_1C, shares) == pytest.approx(0.3465, abs=1e-9)

    def test_a_one_cent_book_can_no_longer_produce_that_order(self):
        br, plan = plan_for(ASK_1C)
        assert plan is None
        assert br.last_failure == "px_below_floor"

    def test_the_refusal_names_the_size_it_avoided(self):
        """The log has to say 500, or the next reader cannot connect the
        refusal to the fill it prevented."""
        br = make_broker(0.80)
        br._ensure_client = lambda: object()
        asyncio.run(br.buy_limit(
            slug=SLUG, side="Down", token_id="tok-down", limit_px=ASK_1C,
            stake=STAKE, wait_s=0.0))
        assert br.last_failure == "px_below_floor"
        assert any("500 shares" in m for m in br.logs), br.logs

    def test_the_PLAN_path_cannot_emit_a_1c_limit_at_all(self):
        """Which path produced the 0.01 limit -- measured, not assumed.

        `plan()` reaches `slippage` past the ask, so even a 1c book plans at
        0.03 and 167 shares. The clamped 0.01 can only arrive through
        `buy_limit`, where the caller hands in a price `take_limit_px` already
        floored. That is why the floor guards BOTH paths: on the plan path it
        is the belt, and `buy_limit` is where the braces were missing.
        """
        br, plan = plan_for(ASK_1C, floor=0.0)
        assert plan is not None
        assert plan["limit"] == pytest.approx(0.03)
        assert plan["shares"] == 167

    def test_a_low_limit_cannot_inflate_the_size_past_the_floor(self):
        """The floor bounds the size to `stake / floor`, by construction."""
        assert math.ceil(STAKE / DEFAULT_MIN_ENTRY_PX) == 100
        assert math.ceil(STAKE / ASK_1C) == 500     # 5x the bounded worst case


# ---------------------------------------------------- it does not over-block

class TestItDoesNotOverBlock:
    def test_the_default_floor_sits_below_every_measured_cell(self):
        # cross trades 0.45-0.90, train 0.66-0.89. The floor must not touch
        # either -- it exists to refuse a MISSING valuation, not a cheap one.
        assert DEFAULT_MIN_ENTRY_PX <= 0.45

    def test_a_normal_ask_still_plans(self):
        br, plan = plan_for(0.80)
        assert plan is not None, br.logs
        assert plan["ask"] == pytest.approx(0.80)
        assert plan["shares"] * plan["limit"] >= STAKE

    def test_the_floor_is_configurable(self):
        br, plan = plan_for(0.10, floor=0.20)
        assert plan is None
        assert br.last_failure == "px_below_floor"


# ----------------------------------------------------------- the predicate

class TestThePredicate:
    def test_the_floor_itself_still_trades(self):
        assert _below_entry_floor(0.01, 0.05)
        assert _below_entry_floor(0.049, 0.05)
        assert not _below_entry_floor(0.05, 0.05)      # the floor is inclusive
        assert not _below_entry_floor(0.80, 0.05)

    def test_a_nan_price_is_refused_not_accepted(self):
        """Written as `px < floor` this would be FALSE for a NaN and the NaN
        would sail through to `ceil(stake / nan)`. The comparison is inverted
        on purpose, and NaN is the reason."""
        assert _below_entry_floor(float("nan"), 0.05)


# --------------------------------------------------------- the resting path

class TestTheRestingPath:
    def test_buy_limit_refuses_below_the_floor_before_the_venue_call(self):
        """A resting bid fills only when someone crosses into it, so a clamped
        limit rests here exactly as expensively as it fills as a taker."""
        br = make_broker(0.80)
        built: list[int] = []

        def _client():
            built.append(1)
            return object()

        br._ensure_client = _client
        fill = asyncio.run(br.buy_limit(
            slug=SLUG, side="Down", token_id="tok-down", limit_px=ASK_1C,
            stake=STAKE, wait_s=0.0))
        assert fill is None
        assert br.last_failure == "px_below_floor"
        assert built == []          # nothing was built, nothing was sent


# ------------------------------------------------- the refusal has to be free

class TestTheRefusalCostsNothing:
    def test_both_order_paths_consult_the_floor(self):
        """One definition + two call sites. A floor enforced on one of two
        order paths is not a floor -- and the FAK path reaches it through
        `plan()`, so the taker side is covered by the first call site."""
        src = open("btcpredict/venues/polymarket/broker.py",
                   encoding="utf-8").read()
        assert src.count("_below_entry_floor(") == 3

    def test_main_rates_the_floor_refusal_as_no_cooldown(self):
        """The name is a contract between two modules: without it in the set,
        correctly declining a degenerate price pauses every family for 30 s."""
        src = open("btcpredict/main.py", encoding="utf-8").read()
        i = src.index("no_cooldown = {")
        assert '"px_below_floor"' in src[i:i + 1400]

    def test_the_floor_is_wired_from_settings(self):
        from btcpredict.config import settings as s
        from btcpredict.venues.registry import pm_config
        cfg = pm_config(s)
        assert cfg.min_entry_px == pytest.approx(
            float(getattr(s, "pm_min_entry_px", 0.05)))
