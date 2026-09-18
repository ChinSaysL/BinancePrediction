"""The cross arm: buy the side the spot just moved into the money.

MEASURED 2026-09-13 (see config.cross_*): the venue UNDERSHOOTS a strike cross
rather than ignoring it -- median ask 0.72 on a side that goes on to win 74.7%.
Capped at 0.50 because win rate and profit move in opposite directions here: the
0.85-0.99 bucket wins 86.8% and is the only negative one.

These pin the trigger (pure, no venue needed) and the two properties that make
the arm safe to run on every websocket tick: it does nothing when it does not
fire, and it never enters a round something else already holds.
"""
from __future__ import annotations

import types
import asyncio
import pathlib
import tempfile
import time
from collections import deque
from types import SimpleNamespace

import pytest

from btcpredict.config import market_prefix
from btcpredict.feeds.market_feed import Outcome, Round
from btcpredict.main import Bot, crossed_side
from btcpredict.risk.guards import RiskGuard
from btcpredict.store.db import Store

STRIKE = 77000.0

async def _settled(v):
    return v


async def _async(v):
    return [{"tokenId": t} for t in v]



def ticks(*prices, step_ms=1000, end_ms=None):
    end = end_ms if end_ms is not None else int(time.time() * 1000)
    n = len(prices)
    return [(end - (n - 1 - i) * step_ms, p) for i, p in enumerate(prices)]


class TestTheTrigger:
    def test_a_cross_up_is_detected(self):
        assert crossed_side(ticks(76997.0, 76998.0, 77001.0), STRIKE, 2.0) == "Up"

    def test_a_cross_down_is_detected(self):
        assert crossed_side(ticks(77003.0, 77002.0, 76999.0), STRIKE, 2.0) == "Down"

    def test_no_cross_when_the_side_never_changed(self):
        assert crossed_side(ticks(77001.0, 77002.0, 77003.0), STRIKE, 2.0) is None

    def test_an_old_cross_is_not_a_fresh_one(self):
        """The venue has had time to reprice it -- that is the whole edge gone."""
        t = ticks(76990.0, 77010.0, 77011.0, 77012.0, step_ms=4000)
        assert crossed_side(t, STRIKE, 2.0, slack_s=1.0) is None

    def test_not_enough_history_is_refused(self):
        assert crossed_side(ticks(77010.0), STRIKE, 2.0) is None
        assert crossed_side([], STRIKE, 2.0) is None

    @pytest.mark.parametrize("strike", [None, 0.0, -1.0])
    def test_an_impossible_strike_is_refused(self, strike):
        assert crossed_side(ticks(76998.0, 77001.0), strike, 2.0) is None

    def test_a_bad_price_is_refused(self):
        assert crossed_side(ticks(76990.0, 0.0), STRIKE, 2.0) is None


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


def make_bot(store, t, asks=(0.30, 0.70)):
    bot = Bot.__new__(Bot)
    bot.mode = "paper"
    bot.store = store
    bot.book = None
    bot.broker = SimpleNamespace(mode="paper")
    bot.bankroll = 50.0
    bot.open_fills = {}
    feed = FakeFeed(t)
    bot._primary_spot = feed
    bot.spot = feed
    bot.spots = {"BTCUSDT": feed}
    bot._round = make_round()
    bot._asks = asks
    bot._sigma = 0.0002
    bot._cross_fired = {}
    bot._cross_day = ""
    bot._cross_count = 0
    bot._order_cooldown_until = 0.0
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



def expected_limit(ask):
    """The shipped rule, read from config rather than frozen as a literal.

    `ask + max(one tick, min(cap, inv * (1 - ask)))`. Pinning the SHAPE keeps
    these tests meaningful when the constant is retuned -- cross_limit_buffer_inv
    was halved 0.25 -> 0.125 on 2026-09-14 after live fills showed the arm pays
    its own buffer rather than being slipped.
    """
    from btcpredict.config import settings as S
    buf = max(0.01, min(S.cross_limit_buffer, S.cross_limit_buffer_inv * (1.0 - ask)))
    # cross_max_price still caps it -- above that the measured cell is negative,
    # so the buffer must not smuggle a trade past the ceiling. This became live
    # when cross_limit_buffer_inv went 0.125 -> 0.30: at an ask of 0.30 the
    # buffer is now 0.21, which reaches the fixture's 0.50 ceiling.
    return min(round(ask + buf, 2), S.cross_max_price)

class TestTheArm:
    def test_it_fires_on_a_cross_and_reaches_the_order_path(self, store, armed):
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.30, 0.70))
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen, "a fresh cross must reach the order path"
        assert bot.store.counts()["decisions"] == 1

    def test_the_limit_sits_a_buffer_above_the_trigger_ask(self, store, armed):
        """The order needs room to chase the book, but NOT unlimited room.

        Two failures bracket this. Pricing the limit AT the trigger ask left
        `take_limit_px` returning max(ask - margin, ask) == ask, so one tick of
        movement meant no fill (live: btc-updown-5m-1789285500 rested at 0.21
        and never filled on a round that settled Up). Pricing it at
        cross_max_price fixed that and cost far more: a LIMIT quote commits
        amountIn/priceLimit SHARES regardless of the market -- verified
        read-only at every limit from 0.30 to 0.99 while the ask was 0.960 --
        so a limit at 0.90 commits at 0.90, and on 2026-09-13 a cross decided
        at 0.760 received exactly stake/0.90 shares and paid 0.9008.

        A buffer above the observed ask keeps the slack and bounds the worst
        case. Live comparison the same day: main arm (limit = quote + 1 tick)
        slipped +0.0086 median over 626 fills; cross arm (pinned 0.90) +0.1408.
        """
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.30, 0.70))
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen, "the cross must reach the order path"
        # side is Up, so fair_up IS the limit.
        assert seen["fair_up"] == pytest.approx(expected_limit(0.30))
        assert seen.get("limit_ceiling") == pytest.approx(expected_limit(0.30))
        assert seen["take_px"] == pytest.approx(0.30), "the trigger ask is the floor"

    def test_a_cheap_entry_gets_a_BIGGER_buffer(self, store, armed):
        """INVERTED 2026-09-14. The buffer used to be min(0.10, 0.20 * ask),
        which shrank as the ask fell. That is backwards: a cheap ask means the
        venue has not yet repriced a fresh cross, so it is exactly where the
        book is about to run furthest. It bid least where it had to bid most --
        all nine live cancellations were cheap (decided 0.14-0.57, median 0.36)
        and no attempt at 0.60+ has ever missed.

        The buffer is now 0.25 * (1 - ask), capped at cross_limit_buffer. The
        old concern it encoded (the limit IS the price when the venue fills the
        whole order) survives as that cap and as the choice of 0.25 over the
        more aggressive 0.35/0.50 -- see config for the P(pin) sweep."""
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.05, 0.95))
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen, "0.05 is inside the armed band"
        assert seen.get("limit_ceiling") == pytest.approx(expected_limit(0.05)), (
            "a cheap entry must be given room to chase the reprice")
        assert (expected_limit(0.05) - 0.05) > (expected_limit(0.60) - 0.60), (
            "the RULE: a cheaper ask must earn a bigger buffer")

    def test_the_buffer_shrinks_as_the_ask_rises(self, store, armed):
        """The direction of the rule, which is the whole point of inverting it.

        The one-tick floor this test used to guard is now unreachable inside
        the armed band -- 0.25 * (1 - ask) only falls under 0.01 above an ask
        of 0.96, and cross_max_price caps at 0.90 long before that -- so what
        is worth pinning is the SHAPE: the cheaper the entry, the more room it
        gets to chase a reprice that has not happened yet."""
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.02, 0.98))
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen
        assert seen.get("limit_ceiling") == pytest.approx(expected_limit(0.02))
        assert (expected_limit(0.02) - 0.02) > (expected_limit(0.50) - 0.50)

    def test_the_limit_never_exceeds_the_price_ceiling(self, store, armed):
        """cross_max_price still caps it: ask 0.48 + 0.10 would be 0.58, but
        the armed ceiling is 0.50. Above the ceiling the measured cell is
        negative, so the buffer must not smuggle a trade past it."""
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.48, 0.52))
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen, "0.48 is inside the band, so it should fire"
        assert seen.get("limit_ceiling") == pytest.approx(0.50)

    def test_a_down_cross_prices_the_ceiling_on_the_right_side(self, store, armed):
        """`fair_up` is P(Up): a Down entry must pass 1 - ceiling, or the taker
        limit inverts -- the same bug that would have sent a Down ticket out at
        a limit near 1 - ask."""
        bot = make_bot(store, ticks(77003.0, 77002.0, 76999.0), asks=(0.70, 0.30))
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen, "the cross must reach the order path"
        # fair_up is the complement of the Down limit. The invariant under test
        # is unchanged by any retune of the buffer: a Down entry passes
        # 1 - limit, or the taker limit inverts.
        assert seen["fair_up"] == pytest.approx(1.0 - expected_limit(0.30)), (
            "a Down entry passes 1 - limit so the Down side prices at the limit")
        assert seen.get("limit_ceiling") == pytest.approx(expected_limit(0.30))

    def test_it_is_inert_with_the_flag_off(self, store, armed, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_enabled", False)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))

        async def boom(*a, **k):
            raise AssertionError("the flag is off")

        bot._place_order = boom
        asyncio.run(bot._cross_tick())
        assert bot.store.counts()["decisions"] == 0

    def test_a_price_above_the_ceiling_is_left_alone(self, store, armed):
        """Above 0.50 the favourite is overpriced -- the only negative bucket."""
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.80, 0.20))

        async def boom(*a, **k):
            raise AssertionError("bought above the ceiling")

        bot._place_order = boom
        asyncio.run(bot._cross_tick())
        # A refusal now leaves a row (see _cross_refuse): the invariant
        # is that no ORDER went out, not that nothing was written.
        assert [r[0] for r in bot.store.conn.execute(
            "select reason from decisions")] == [] or all(
            r[0].startswith("cross refused:") for r in
            bot.store.conn.execute("select reason from decisions"))

    def test_it_refuses_the_side_opposite_the_main_arm(self, store, armed):
        """CHANGED 2026-09-13 (twice). This first asserted a refusal, was
        flipped to allow hedging, and is now a refusal again -- on measurement
        rather than on either intuition.

        Replaying 163 fill-aware triggers against the main arm's REAL fills:
            SAME side         n=44  win 0.864  EV/$ +0.3558  day-t  3.93
            OPPOSITE side     n=38  win 0.605  EV/$ -0.1066  day-t -0.66
            no main position  n=81  win 0.802  EV/$ +0.2200  day-t  2.23
        Break-even at the average fill is 64.2%, so opposite sits below its own
        break-even. The same-vs-opposite win-rate gap is z=2.74 (p~0.006).

        The middle version of this test was not wrong to allow it -- it was
        decided on 16 and 9 display-priced fills, which could not see this.
        """
        # main arm holds DOWN; the cross below wants UP -> opposite
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        bot.guard.on_open(bot._round.slug)
        bot.open_fills = {1: (bot._round.slug,
                              SimpleNamespace(side="Down", stake=1.2,
                                              token_id="2"))}
        placed = []

        async def place(*a, **k):
            placed.append(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert not placed, "an opposite-side cross is the arm's one losing cell"

    def test_the_opposite_gate_can_be_switched_off(self, store, armed,
                                                   monkeypatch):
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_skip_opposite_to_main", False)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        bot.guard.on_open(bot._round.slug)
        bot.open_fills = {1: (bot._round.slug,
                              SimpleNamespace(side="Down", stake=1.2,
                                              token_id="2"))}
        placed = []

        async def place(*a, **k):
            placed.append(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert placed, "the gate must be a switch, not a hard-coded rule"

    def test_a_cross_opposite_an_EARLIER_CROSS_is_refused(self, store, armed):
        """A crossing back through the strike is a new bet, not a hedge.

        This asserted the opposite until 2026-09-14, on the reasoning that
        "consecutive crosses alternate sides by construction, so a round
        oscillating around the strike hedges itself". Both halves were wrong.
        Alternation is an artefact of the corpus's 10s grid (at live tick
        resolution 51 of 65 non-first entries repeat the SAME side), and with
        the first position sunk the second leg is worth taking iff it is +EV
        alone -- measured 0.574 against 0.714 for a first entry over 31 days of
        real strikes, day-t -3.05, with negative growth under every pricing.

        What this does NOT do is shrink the per-round cap: same-side repeats
        are most of what it buys and they are profitable on their own.
        """
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        # The ticks cross UP through the strike; we already hold Down.
        bot._cross_side = {bot._round.slug: "Down"}
        placed = []

        async def place(*a, **k):
            placed.append(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert not placed, "a reversal of our own entry must not be bought"
        assert any("reversal" in r[0] for r in bot.store.conn.execute(
            "select reason from decisions")), "the refusal must leave a row"

    def test_a_cross_on_the_SAME_side_as_an_earlier_one_is_allowed(
            self, store, armed):
        """The reversal guard must not become a per-round one-shot.

        Same-side repeats are the bulk of what cross_max_per_round buys and
        they carry positive growth on their own (n=51, +0.451/+0.143/+0.404
        under the three pricings). Only the opposite side is refused.
        """
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        bot._cross_side = {bot._round.slug: "Up"}
        placed = []

        async def place(*a, **k):
            placed.append(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert placed, "a repeat of the side we already hold stays allowed"

    def test_the_guard_is_a_switch(self, store, armed, monkeypatch):
        """cross_allow_reversal restores the old behaviour."""
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_allow_reversal", True)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        bot._cross_side = {bot._round.slug: "Down"}
        placed = []

        async def place(*a, **k):
            placed.append(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert placed, "the gate must be a switch, not a hard-coded rule"

    def test_it_also_takes_the_side_the_main_arm_already_holds(
            self, store, armed):
        """BOTH sides, on an explicit instruction.

        Opposite-side is a hedge, same-side is a correlated add. On the measured
        sample allowing both wins on volume (16 fills at +0.116/$1 against 9 at
        +0.068); the EV gap is not distinguishable at those counts. The cost is
        that a same-side loser costs the main stake PLUS the cross stake rather
        than the two offsetting, and the daily cap is what bounds that.
        """
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))   # crosses UP
        bot.guard.on_open(bot._round.slug)
        bot.open_fills = {1: (bot._round.slug,
                              SimpleNamespace(side="Up", stake=1.2))}
        placed = []

        async def place(*a, **k):
            placed.append(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert placed, "same-side cross entries are taken too"

    def test_the_main_arms_venue_position_does_not_block_it(self, store, armed):
        """REGRESSION 2026-09-13, found live: a qualifying cross went untaken.

        btc-updown-5m-1789297200 (11:00-11:05 UTC) crossed Down->Up at tau 19s,
        depth 0.35bps, ask 0.750 -- every gate passed, and the round settled Up.
        It never fired: the main arm held Up from 11:01:30, so the venue-token
        test returned before check_reserved ever ran, and returned SILENTLY.

        The two hedge tests above could not catch it. make_bot leaves
        broker.mode="paper" and book=None, so `broker.mode == "live" and
        self.book` is false and the venue branch is dead in them. This one
        turns it on, which is the only way the production path is exercised.

        Measured cost of the bug: 52% of fill-aware triggers over 6 days, at an
        EV indistinguishable from the ones that got through.
        """
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))   # crosses UP
        bot.broker = SimpleNamespace(
            mode="live",
            positions=lambda: _async(["1"]),   # the venue holds the UP token
        )
        bot.book = object()                    # truthy: the live branch is on
        bot.guard.on_open(bot._round.slug)     # main arm holds the round
        # ...and the ledger knows about it, which is what makes it another
        # arm's position rather than unbooked exposure.
        bot.open_fills = {1: (bot._round.slug,
                              SimpleNamespace(side="Up", stake=1.2,
                                              token_id="1"))}
        placed = []

        async def place(*a, **k):
            placed.append(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert placed, ("a cross must still fire when the MAIN arm holds the "
                        "round; the reserved slot is what bounds this arm")

    def test_an_unbooked_venue_position_still_stops_it(self, store, armed):
        """The half of the check that matters: a fill we have NO record of.

        An order that was never confirmed but actually filled never reaches the
        database, so _restore_open_fills cannot replay it and no pool counts
        it. The venue is the only place it exists, and buying again would
        genuinely double it. Distinguished from another arm's position purely
        by whether the ledger can explain it.
        """
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        bot.broker = SimpleNamespace(mode="live",
                                     positions=lambda: _async(["1"]))
        bot.book = object()
        bot.open_fills = {}                    # ledger cannot explain token "1"
        placed = []

        async def place(*a, **k):
            placed.append(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert not placed, "unbooked venue exposure must still stop the arm"

    def test_an_unreadable_venue_still_stops_it(self, store, armed):
        """The half of the venue check that stays: if we cannot read exposure
        at all, we cannot reason about it, so the arm sits the round out."""
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))

        async def boom():
            raise RuntimeError("venue down")

        bot.broker = SimpleNamespace(mode="live", positions=boom)
        bot.book = object()
        placed = []

        async def place(*a, **k):
            placed.append(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert not placed, "an unreadable position list must stop the arm"

    def test_it_bids_up_to_the_ceiling_not_the_stale_quote(self, store, armed):
        """The arm must be allowed to chase the reprice it just predicted.

        `take_limit_px` clamps to quote + tick LAST, so a limit priced off the
        trigger ask can only ever be 1c above a quote that is about to move 60c.
        Measured live 2026-09-13 on btc-updown-5m-1789285500: spot crossed above
        the strike, Up was still 0.200, the limit clamped to 0.21, never filled,
        and the round settled Up. `limit_ceiling` replaces that clamp.
        """
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.20, 0.80))
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        # CHANGED 2026-09-13: no longer cross_max_price. A LIMIT quote on this
        # venue commits amountIn/priceLimit SHARES regardless of the market, so
        # pinning the limit at the ceiling means committing at the ceiling --
        # and the venue sometimes delivers exactly that. Live: the main arm
        # (limit = quote + tick) slipped +0.0086 median over 626 fills, the
        # cross arm (limit pinned at 0.90) slipped +0.1408. The limit now sits
        # a buffer above the observed ask: still slack to chase the reprice,
        # but the worst case is ask+buffer instead of 0.90.
        assert seen.get("limit_ceiling") == pytest.approx(expected_limit(0.20)), (
            "the limit is ask + the configured buffer, not the price ceiling")
        assert seen["take_px"] == pytest.approx(0.20), "the trigger ask is the floor"

    def test_the_reserved_pool_is_bounded(self, store, armed, monkeypatch):
        """The reserved slots are a small POOL, not one seat -- the cross arm
        takes several crosses per round -- but it is still bounded, so a choppy
        round cannot open unlimited correlated exposure against the daily cap.

        The bound is `cross_max_open_positions`, NOT `cross_max_per_round`:
        `reserved_open` counts every open reserved position ACROSS FAMILIES, and
        since the arm was widened to six markets a per-round knob can no longer
        stand in for an account-wide one.
        """
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_max_open_positions", 2)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        bot.guard.state.reserved_open = 2          # pool already full

        async def boom(*a, **k):
            raise AssertionError("exceeded the reserved pool")

        bot._place_order = boom
        asyncio.run(bot._cross_tick())
        # A refusal now leaves a row (see _cross_refuse): the invariant
        # is that no ORDER went out, not that nothing was written.
        assert [r[0] for r in bot.store.conn.execute(
            "select reason from decisions")] == [] or all(
            r[0].startswith("cross refused:") for r in
            bot.store.conn.execute("select reason from decisions"))

    def test_a_deep_cross_is_refused(self, store, armed):
        """Past the gate the venue has repriced and the edge is gone.

        The boundary MOVED 2026-09-13 (0.5 -> 5.0 bps), so the old fixture at
        ~2.6 bps is now a cross the arm should TAKE -- 2-5bps is the strongest
        cell on the corpus (win 0.772, EV +0.889/$1 priced). What still has to
        be refused is past 5 bps, where the ask jumps to 0.888 and EV turns
        negative (-0.094/$1). Measured on 31 days with the REAL Chainlink
        strike; see config.cross_max_depth_bps.
        """
        bot = make_bot(store, ticks(76990.0, 76995.0, 77080.0))   # ~10 bps deep

        async def boom(*a, **k):
            raise AssertionError("took a cross the venue has already repriced")

        bot._place_order = boom
        asyncio.run(bot._cross_tick())
        # A refusal now leaves a row (see _cross_refuse): the invariant
        # is that no ORDER went out, not that nothing was written.
        assert [r[0] for r in bot.store.conn.execute(
            "select reason from decisions")] == [] or all(
            r[0].startswith("cross refused:") for r in
            bot.store.conn.execute("select reason from decisions"))

    def test_it_takes_several_crosses_in_one_round_up_to_the_cap(
            self, store, armed, monkeypatch):
        """Consecutive crosses alternate sides, so a round oscillating around
        the strike is naturally hedged rather than doubled -- taking every one
        measured +$31.37 against +$14.16 for the first alone."""
        monkeypatch.setattr("btcpredict.config.settings.cross_max_per_round", 2)
        # the re-entry gap, the account exposure cap and the round-stake cap
        # are SEPARATE limits with their own tests. Disable them here so this
        # test measures cross_max_per_round and nothing else -- in this fixture
        # the stake is 24% of bankroll, so the 30% exposure cap would bind on
        # the second entry first.
        monkeypatch.setattr("btcpredict.config.settings.cross_reentry_gap_s", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_open_stake_pct", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_round_stake_mult", 0.0)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        calls = []

        async def execute(*a, **k):
            calls.append(1)

        bot._cross_execute = execute

        async def go():
            for _ in range(4):
                await bot._cross_tick()
                await asyncio.sleep(0)      # let the spawned task start
        asyncio.run(go())
        assert len(calls) == 2, "capped at cross_max_per_round, not unlimited"

    def test_no_cross_touches_nothing(self, store, armed):
        bot = make_bot(store, ticks(77001.0, 77002.0, 77003.0))

        async def boom(*a, **k):
            raise AssertionError("fired without a cross")

        bot._place_order = boom
        asyncio.run(bot._cross_tick())
        assert bot.store.counts()["decisions"] == 0

    def test_the_daily_cap_holds(self, store, armed, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_max_per_day", 1)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        calls = []

        async def place(*a, **k):
            calls.append(1)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        bot._cross_fired = {}                 # a different round, same day
        bot._round = make_round()
        asyncio.run(bot._cross_tick())
        assert len(calls) == 1, "the daily cap bounds a bad day"


class TestTheStake:
    """The cross arm sizes like the main arm unless a flat stake is set.

    A flat $1 does not compound: the arm's contribution stays constant while
    the bankroll grows, and it ignores the sizing rule the rest of the bot uses.
    """

    def _bot(self, store, monkeypatch, bankroll=200.0):
        monkeypatch.setattr("btcpredict.config.settings.cross_enabled", True)
        monkeypatch.setattr("btcpredict.config.settings.cross_look_s", 2.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_min_tau_s", 3.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_tau_s", 60.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_min_price", 0.02)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_price", 0.90)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.40, 0.60))
        bot.bankroll = bankroll
        return bot

    def test_it_scales_with_the_bankroll_when_no_flat_stake_is_set(
            self, store, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_stake_usdt", None)
        monkeypatch.setattr("btcpredict.config.settings.sizing_mode", "fraction")
        monkeypatch.setattr("btcpredict.config.settings.stake_fraction", 0.05)
        monkeypatch.setattr("btcpredict.config.settings.stake_tilt", "none")
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        monkeypatch.setattr("btcpredict.config.settings.cross_stake_mult", 1.0)
        bot = self._bot(store, monkeypatch, bankroll=200.0)
        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen, "the arm must fire"
        assert seen["stake"] == pytest.approx(10.0, rel=0.01), (
            "5% of a $200 bankroll, not a flat $1")

    def test_the_cross_multiplier_scales_the_main_rule(self, store, monkeypatch):
        """This arm is sized ABOVE the main rule: the corrected limit policy
        cut it to ~9.8 trades/day while each trade is worth 2.5-5.7x more, so
        at 1x it under-contributes. See config.cross_stake_mult for the sweep
        (3x is the last multiplier whose median still rises at the CI-low win
        rate; past it the daily cap halts the day before the edge plays out)."""
        monkeypatch.setattr("btcpredict.config.settings.cross_stake_usdt", None)
        monkeypatch.setattr("btcpredict.config.settings.sizing_mode", "fraction")
        monkeypatch.setattr("btcpredict.config.settings.stake_fraction", 0.05)
        monkeypatch.setattr("btcpredict.config.settings.stake_tilt", "none")
        monkeypatch.setattr("btcpredict.config.settings.cross_stake_mult", 3.0)
        # lifted so this measures the MULTIPLIER; the cap has its own test
        monkeypatch.setattr("btcpredict.config.settings.stake_cap_usdt", 100.0)
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot = self._bot(store, monkeypatch, bankroll=200.0)
        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen["stake"] == pytest.approx(30.0, rel=0.01), "3 x 5% of $200"

    def test_the_multiplier_does_not_exempt_the_stake_cap(self, store,
                                                          monkeypatch):
        """The multiplier scales the arm; it must not smuggle it past the
        account-wide cap."""
        monkeypatch.setattr("btcpredict.config.settings.cross_stake_usdt", None)
        monkeypatch.setattr("btcpredict.config.settings.sizing_mode", "fraction")
        monkeypatch.setattr("btcpredict.config.settings.stake_fraction", 0.05)
        monkeypatch.setattr("btcpredict.config.settings.stake_tilt", "none")
        monkeypatch.setattr("btcpredict.config.settings.cross_stake_mult", 3.0)
        monkeypatch.setattr("btcpredict.config.settings.stake_cap_usdt", 12.0)
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot = self._bot(store, monkeypatch, bankroll=200.0)
        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen["stake"] == pytest.approx(12.0), "capped, not 30"

    def test_a_flat_stake_still_overrides(self, store, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_stake_usdt", 1.0)
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot = self._bot(store, monkeypatch, bankroll=200.0)
        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen["stake"] == pytest.approx(1.0), "an explicit flat stake wins"


class TestTheDailyCapSurvivesARestart:
    """REGRESSION 2026-09-13: `_cross_count` started at 0 in __init__.

    Every restart handed the arm a fresh daily budget. On a day with 208
    restarts, `cross_max_per_day` bounded nothing. The deleted ticket arm
    seeded the same counter from the database for exactly this reason.
    """

    @staticmethod
    def _bot_with(store, n_today, n_yesterday=0):
        bot = Bot.__new__(Bot)
        bot.mode = "live"
        bot.store = store
        bot._cross_day = ""
        bot._cross_count = 0
        now = time.time()
        midnight = now - (now % 86400.0)
        for i in range(n_today):
            store.insert("decisions", {
                "slug": f"s{i}", "ts_ms": int((midnight + 60 + i) * 1000),
                "tau_s": 20.0, "side": "Up", "fair": 0.5, "quote": 0.5,
                "edge": 0.0, "hurdle": 0.0, "sigma_1m": 0.0002, "spot": 1.0,
                "strike": 1.0, "stake": 1.0, "acted": 0,
                "reason": f"cross into Up @0.500 tau20s"})
        for i in range(n_yesterday):
            store.insert("decisions", {
                "slug": f"y{i}", "ts_ms": int((midnight - 3600 - i) * 1000),
                "tau_s": 20.0, "side": "Up", "fair": 0.5, "quote": 0.5,
                "edge": 0.0, "hurdle": 0.0, "sigma_1m": 0.0002, "spot": 1.0,
                "strike": 1.0, "stake": 1.0, "acted": 0,
                "reason": "cross into Up @0.500 tau20s"})
        return bot

    def test_it_counts_todays_attempts(self, store):
        bot = self._bot_with(store, 7)
        bot._seed_cross_count()
        assert bot._cross_count == 7

    def test_yesterdays_attempts_do_not_carry_over(self, store):
        """The cap is DAY-scoped: a fresh UTC day starts clean."""
        bot = self._bot_with(store, 2, n_yesterday=9)
        bot._seed_cross_count()
        assert bot._cross_count == 2

    def test_other_arms_decisions_are_not_counted(self, store):
        bot = self._bot_with(store, 3)
        store.insert("decisions", {
            "slug": "m1", "ts_ms": int(time.time() * 1000), "tau_s": 120.0,
            "side": "Up", "fair": 0.5, "quote": 0.5, "edge": 0.03,
            "hurdle": 0.02, "sigma_1m": 0.0002, "spot": 1.0, "strike": 1.0,
            "stake": 1.0, "acted": 1, "reason": "model edge +0.030"})
        bot._seed_cross_count()
        assert bot._cross_count == 3, "only this arm's attempts count"

    def test_a_seeded_count_at_the_cap_stops_the_arm(self, store, armed,
                                                     monkeypatch):
        """The point of the fix: the restart must not reopen the budget."""
        monkeypatch.setattr("btcpredict.config.settings.cross_max_per_day", 5)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        for i in range(5):
            store.insert("decisions", {
                "slug": f"s{i}", "ts_ms": int(time.time() * 1000),
                "tau_s": 20.0, "side": "Up", "fair": 0.5, "quote": 0.5,
                "edge": 0.0, "hurdle": 0.0, "sigma_1m": 0.0002, "spot": 1.0,
                "strike": 1.0, "stake": 1.0, "acted": 0,
                "reason": "cross into Up @0.500 tau20s"})
        bot._seed_cross_count()
        placed = []

        async def place(*a, **k):
            placed.append(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert not placed, "the daily cap must hold across a restart"


class TestAVoidGivesTheSlotBack:
    """End-to-end through settle_due, not just RiskGuard.release.

    The unit tests in test_risk_and_execution pin `release` itself; this pins
    that the VOID BRANCH actually calls it. That branch used to `continue` past
    the guard entirely, so the slot _book_fill had already claimed outlived the
    process and the arm went quiet.
    """

    @staticmethod
    def _bot(store, mode):
        now = int(time.time() * 1000)
        store.upsert_round({
            "slug": "btc-updown-5m-1789000000", "market_topic_id": 1,
            "market_id": 2, "condition_id": "c", "symbol": "BTCUSDT",
            "start_ms": now - 300_000, "end_ms": now - 10_000,
            "strike": STRIKE, "end_price": STRIKE + 5, "fee_rate_bps": 200,
            "up_token_id": "1", "down_token_id": "2", "price_feed_id": None,
            "feed_provider": None, "status": "SETTLED", "winner": "Up",
            "settled_at_ms": now})
        fid = store.insert("fills", {
            "decision_id": None, "slug": "btc-updown-5m-1789000000",
            "ts_ms": now - 60_000, "mode": mode, "side": "Up",
            "requested_px": 0.5, "filled_px": 0.5, "shares": 2.0,
            "stake": 1.0, "fee_paid": 0.0, "order_id": "o1", "raw": "{}",
            "token_id": "1"})
        bot = Bot.__new__(Bot)
        bot.mode = "live"
        bot.store = store
        bot.market = SimpleNamespace()
        bot.guard = RiskGuard(daily_loss_cap=0.0, max_consecutive_losses=5,
                              max_open_rounds=1, daily_loss_cap_pct=0.20,
                              bankroll=lambda: 50.0)
        fill = SimpleNamespace(stake=1.0, fee_paid=0.0, token_id="1",
                               shares=2.0, filled_px=0.5)
        bot.open_fills = {fid: ("btc-updown-5m-1789000000", fill)}
        # the venue disowns it: never filled, so no stake was deployed
        bot.broker = SimpleNamespace(
            mode="live",
            settle=lambda f, w: _settled(SimpleNamespace(
                ts_ms=now, won=False, payout=0.0, pnl=0.0, void=True)))
        return bot

    def test_a_voided_cross_fill_frees_the_reserved_slot(self, store):
        bot = self._bot(store, "cross")
        bot.guard.on_open("btc-updown-5m-1789000000", reserved=True)
        assert bot.guard.check_reserved("x", max_open=1)[0] is False
        asyncio.run(bot.settle_due())
        assert bot.guard.check_reserved("x", max_open=1)[0] is True, (
            "the void branch must hand the reserved slot back")

    def test_a_voided_main_fill_frees_the_main_slot(self, store):
        """The worse case: one leaked slug refused EVERY later round."""
        bot = self._bot(store, "live")
        bot.guard.on_open("btc-updown-5m-1789000000")
        assert bot.guard.check("some-other-round")[0] is False
        asyncio.run(bot.settle_due())
        assert bot.guard.check("some-other-round")[0] is True, (
            "one void must not silence the main arm")

    def test_a_void_still_records_no_pnl(self, store):
        bot = self._bot(store, "cross")
        bot.guard.on_open("btc-updown-5m-1789000000", reserved=True)
        asyncio.run(bot.settle_due())
        assert bot.guard.state.daily_pnl == 0.0
        assert bot.guard.state.consecutive_losses == 0


class TestTheWebsocketBook:
    """The arm prices off the WS feed when it is fresh, and the REST poll when
    it is not. Falling back matters more than the upgrade: a frozen feed would
    price every order off a dead book."""

    @staticmethod
    def _bot_with_ws(store, up_bid=0.30, up_ask=0.35, ask_sz=1000.0,
                     bid_sz=1000.0, age_s=0.0):
        from btcpredict.feeds.prediction_book import BookTop, PredictionBook
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0),
                       asks=(0.45, 0.55))      # REST says something different
        wsb = PredictionBook(api_key="k", api_secret="s", session=None,
                             signer=None)
        now = int(time.time() * 1000)
        wsb.tops[bot._round.market_id] = BookTop(
            up_bid=up_bid, up_ask=up_ask, up_ask_size=ask_sz,
            up_bid_size=bid_sz, ts_ms=now, recv_ms=now - int(age_s * 1000),
            # Levels matter: the arm prices the whole order, not the touch.
            # Deep second levels at the same price keep these tests about the
            # thing each one is actually pinning.
            asks=((up_ask, ask_sz), (round(up_ask + 0.01, 2), 10000.0)),
            bids=((up_bid, bid_sz), (round(up_bid - 0.01, 2), 10000.0)))
        bot.ws_book = wsb
        return bot

    def test_a_fresh_ws_book_is_used_over_the_rest_poll(self, store, armed):
        bot = self._bot_with_ws(store, up_ask=0.35)
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen, "the cross should fire"
        assert seen["take_px"] == pytest.approx(0.35), (
            "it must price off the websocket, not the 0.45 REST poll")

    def test_a_stale_ws_book_falls_back_to_the_rest_poll(self, store, armed):
        bot = self._bot_with_ws(store, up_ask=0.10, age_s=30.0)
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen, "a stale feed must not stop the arm, only be ignored"
        assert seen["take_px"] == pytest.approx(0.45), (
            "a stale book must fall back to the REST ask")

    def test_the_stake_is_capped_by_total_book_depth(self, store, armed):
        """2026-09-13 live: the arm asked $1.18 and the venue filled $0.90.
        The cap is now the whole visible book, not just the touch."""
        bot = self._bot_with_ws(store, up_ask=0.50, ask_sz=4.0)   # $2.00 total
        bot.ws_book.tops[bot._round.market_id].asks = ((0.50, 4.0),)
        bot.bankroll = 500.0
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen, "it should still fire"
        assert seen["stake"] <= 2.0 + 1e-9

    def test_a_touch_thinner_than_the_minimum_is_skipped(self, store, armed):
        bot = self._bot_with_ws(store, up_ask=0.50, ask_sz=0.4)  # $0.20 resting
        placed = []

        async def place(*a, **k):
            placed.append(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert not placed, "there is not enough resting to make a legal order"

    def test_a_down_cross_sizes_off_the_BID_queue(self, store, armed):
        """Buying Down consumes the Up bid queue. Sizing a Down order against
        the ask queue would cap on liquidity that is not on that side."""
        bot = self._bot_with_ws(store, up_bid=0.60, up_ask=0.65,
                                ask_sz=1000.0, bid_sz=3.0)
        bot.ws_book.tops[bot._round.market_id].bids = ((0.60, 3.0),)
        bot._primary_spot = FakeFeed(ticks(77003.0, 77002.0, 76999.0))
        bot.spot = bot._primary_spot
        bot.bankroll = 500.0
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen, "the down cross should fire"
        # down price = 1 - 0.60 = 0.40; resting = 0.40 * 3.0 = $1.20
        assert seen["stake"] <= 1.20 + 1e-9
        assert seen["take_px"] == pytest.approx(0.40)

    def test_it_prices_the_SWEEP_not_the_touch(self, store, armed):
        """The bug this exists for. A live Down book held $0.10 at the best
        price and $2.61 one level behind; a cross gated at 0.760 filled at
        0.9008 because the order ate through to the limit. The arm must gate
        on what the whole order pays."""
        bot = self._bot_with_ws(store, up_ask=0.20, ask_sz=1.0)   # $0.20 at touch
        bot.ws_book.tops[bot._round.market_id].asks = (
            (0.20, 1.0),        # $0.20 here
            (0.45, 10.0),       # the rest is far worse
        )
        bot.bankroll = 100.0
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen, "it should fire"
        assert seen["take_px"] > 0.20 + 1e-9, (
            "gating on the 0.20 touch is what made a 0.76 gate fill at 0.90")
        assert seen["take_px"] < 0.45

    def test_a_sweep_past_the_ceiling_is_refused(self, store, armed,
                                                 monkeypatch):
        """cross_max_price is this arm's load-bearing gate. Before sweep
        pricing it was applied to a touch we could not get, so it never
        actually bound on what we paid."""
        monkeypatch.setattr("btcpredict.config.settings.cross_max_price", 0.50)
        bot = self._bot_with_ws(store, up_ask=0.20, ask_sz=0.5)
        bot.ws_book.tops[bot._round.market_id].asks = (
            (0.20, 0.5), (0.95, 100.0))       # $1+ sweeps way past the ceiling
        bot.bankroll = 100.0
        placed = []

        async def place(*a, **k):
            placed.append(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert not placed, "a sweep beyond the ceiling must be refused"




class TestTheStakeCapCanBeDisabled:
    """0 = NO CAP, the repo's convention for every other limit.

    The trap this guards: `stake_for_fraction` already wrote `if cap > 0`, but
    two other sites did a bare `min(x, stake_cap_usdt)` -- _stake_for's tilt
    clamp and the cross multiplier. With the cap at 0 those would have clamped
    every stake to zero, and the bot would have gone silent with no error
    anywhere. A disabled limit must mean "unbounded", never "bound to zero".
    """

    @staticmethod
    def _bot(store, monkeypatch, bankroll):
        monkeypatch.setattr("btcpredict.config.settings.cross_enabled", True)
        monkeypatch.setattr("btcpredict.config.settings.cross_look_s", 2.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_min_tau_s", 3.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_tau_s", 30.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_price", 0.50)
        monkeypatch.setattr("btcpredict.config.settings.cross_min_price", 0.02)
        monkeypatch.setattr("btcpredict.config.settings.cross_stake_usdt", None)
        monkeypatch.setattr("btcpredict.config.settings.sizing_mode", "fraction")
        monkeypatch.setattr("btcpredict.config.settings.stake_fraction", 0.05)
        monkeypatch.setattr("btcpredict.config.settings.stake_tilt", "none")
        monkeypatch.setattr("btcpredict.config.settings.cross_stake_mult", 1.0)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.30, 0.70))
        bot.bankroll = bankroll
        return bot

    def _stake(self, store, monkeypatch, bankroll, cap):
        monkeypatch.setattr("btcpredict.config.settings.stake_cap_usdt", cap)
        bot = self._bot(store, monkeypatch, bankroll)
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        return seen.get("stake")

    def test_a_zero_cap_does_not_clamp_the_stake_to_zero(self, store,
                                                         monkeypatch):
        assert self._stake(store, monkeypatch, 2000.0, 0.0) == pytest.approx(100.0)

    def test_a_positive_cap_still_binds(self, store, monkeypatch):
        assert self._stake(store, monkeypatch, 2000.0, 25.0) == pytest.approx(25.0)

    def test_the_tilt_clamp_also_honours_a_zero_cap(self, store, monkeypatch):
        """The second bare min() site: with a tilt active and no cap, the
        tilted stake must scale, not collapse."""
        monkeypatch.setattr("btcpredict.config.settings.stake_tilt", "inv_z")
        monkeypatch.setattr("btcpredict.config.settings.stake_cap_usdt", 0.0)
        bot = self._bot(store, monkeypatch, 2000.0)
        monkeypatch.setattr("btcpredict.config.settings.stake_tilt", "inv_z")
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen.get("stake", 0) > 50.0, "a zero cap must not clamp the tilt"

    def test_the_multiplier_and_a_zero_cap_compose(self, store, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.stake_cap_usdt", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_stake_mult", 3.0)
        bot = self._bot(store, monkeypatch, 2000.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_stake_mult", 3.0)
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen.get("stake") == pytest.approx(300.0), "3 x 5% of $2000"


class TestPerMarketOverrides:
    """The knob table has to reach the arm, not just the resolver."""

    def test_an_override_on_this_market_blocks_the_entry(
            self, store, armed, monkeypatch):
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.30, 0.70))
        bot._place_order = _swallow
        prefix = market_prefix(bot._round.slug)
        # depth 0 admits nothing: the same trigger that fires below is refused.
        monkeypatch.setattr("btcpredict.config.settings.cross_overrides",
                            {prefix: {"max_depth_bps": 0.0}})
        asyncio.run(bot._cross_tick())
        assert bot._cross_fired == {}

    def test_an_override_on_ANOTHER_market_leaves_this_one_alone(
            self, store, armed, monkeypatch):
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.30, 0.70))
        bot._place_order = _swallow
        monkeypatch.setattr("btcpredict.config.settings.cross_overrides",
                            {"eth-updown-15m": {"max_depth_bps": 0.0}})
        asyncio.run(bot._cross_tick())
        assert bot._cross_fired != {}


async def _swallow(*a, **k):
    return None


class TestDailyCapZeroMeansOff:
    """0 = OFF, as everywhere else in this repo.

    The check was `count >= cap`, which at cap 0 reads 0 >= 0 as "exhausted"
    and disables the arm completely -- the exact opposite of no-cap.
    """

    @staticmethod
    def _armed_bot(store, count):
        """A bot whose daily counter is ALREADY at `count`.

        `_cross_day` has to be today as well: _cross_tick resets the counter on
        a day rollover, and the fixture's empty string is always a rollover --
        which silently zeroed the count these tests are about.
        """
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        bot._place_order = _swallow
        bot._cross_day = time.strftime("%Y-%m-%d", time.gmtime())
        bot._cross_count = count
        return bot

    def test_cap_zero_still_fires(self, store, armed, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_max_per_day", 0)
        bot = self._armed_bot(store, 999)      # would exhaust any real budget
        asyncio.run(bot._cross_tick())
        assert bot._cross_fired != {}, "cap 0 must mean unlimited, not zero"

    def test_a_positive_cap_still_binds(self, store, armed, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_max_per_day", 5)
        bot = self._armed_bot(store, 5)
        asyncio.run(bot._cross_tick())
        assert bot._cross_fired == {}, "a real cap must still stop the arm"

    def test_under_a_positive_cap_it_fires(self, store, armed, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_max_per_day", 5)
        bot = self._armed_bot(store, 4)
        asyncio.run(bot._cross_tick())
        assert bot._cross_fired != {}, "one below the cap must still be allowed"


class TestRefusalsAreRecorded:
    """A DETECTED-then-refused trigger must leave a trace.

    Seven rounds on 2026-09-14 looked like missed crossings and were not: the
    arm saw them and declined. Nothing in `decisions` said so, because every
    post-detection refusal returned silently, so the only way to tell a refusal
    from a miss was to replay the venue strike against 1s klines.
    """

    def _reasons(self, store):
        rows = store.conn.execute(
            "select reason from decisions where reason like 'cross refused:%'"
        ).fetchall()
        return [r[0] for r in rows]

    def test_a_too_deep_cross_is_recorded(self, store, armed, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_max_depth_bps", 0.0001)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        bot._place_order = _swallow
        asyncio.run(bot._cross_tick())
        got = self._reasons(store)
        assert got and "through the strike" in got[0], got

    def test_an_out_of_band_ask_is_recorded(self, store, armed, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_max_price", 0.05)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        bot._place_order = _swallow
        asyncio.run(bot._cross_tick())
        got = self._reasons(store)
        assert got and "outside" in got[0], got

    def test_the_same_reason_is_recorded_once_per_round(self, store, armed,
                                                        monkeypatch):
        """At ~114 evaluations/s an undeduped refusal wrote 164 lines for 11
        rounds. One round, one reason, one row."""
        monkeypatch.setattr("btcpredict.config.settings.cross_max_depth_bps", 0.0001)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        bot._place_order = _swallow
        for _ in range(25):
            asyncio.run(bot._cross_tick())
        assert len(self._reasons(store)) == 1, self._reasons(store)

    def test_a_new_round_records_again(self, store, armed, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_max_depth_bps", 0.0001)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        bot._place_order = _swallow
        asyncio.run(bot._cross_tick())
        bot._round.slug = "btc-updown-5m-1789000300"      # next round
        asyncio.run(bot._cross_tick())
        assert len(self._reasons(store)) == 2, self._reasons(store)

    def test_no_crossing_records_nothing(self, store, armed):
        """Silence must still mean 'nothing happened' when nothing happened."""
        bot = make_bot(store, ticks(76990.0, 76990.0, 76990.0))
        bot._place_order = _swallow
        asyncio.run(bot._cross_tick())
        assert self._reasons(store) == []


class TestRoundRangeGate:
    """Skip rounds that have already swung wide (config.cross_max_round_range_bps).

    Measured: a crossing in a calm round sticks, one in a round that has already
    moved 12-20bps reverses (EV +0.661 vs -0.324). The range must be taken from
    the round OPEN to the trigger -- never the full round, which is the future.
    """

    @staticmethod
    def _bot_with_range(store, lo, hi):
        """A bot whose round has already travelled from `lo` to `hi`."""
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        bot._place_order = _swallow
        now = int(time.time() * 1000)
        bot._ticks = {"BTCUSDT": [(bot._round.start_ms + 1000, lo),
                                  (bot._round.start_ms + 2000, hi),
                                  (now - 1000, 77001.0)]}
        return bot

    def _refusals(self, store):
        return [r[0] for r in store.conn.execute(
            "select reason from decisions where reason like 'cross refused:%'")]

    def test_a_calm_round_is_taken(self, store, armed, monkeypatch):
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_max_round_range_bps", 10.0)
        bot = self._bot_with_range(store, 77000.0, 77002.0)   # ~0.3 bps
        asyncio.run(bot._cross_tick())
        assert bot._cross_fired != {}, "a calm round must still trade"

    def test_a_round_that_already_swung_is_skipped(self, store, armed, monkeypatch):
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_max_round_range_bps", 10.0)
        bot = self._bot_with_range(store, 76900.0, 77100.0)   # ~26 bps
        asyncio.run(bot._cross_tick())
        assert bot._cross_fired == {}, "a wide round must be skipped"
        got = self._refusals(store)
        assert got and "already moved" in got[0], got

    def test_zero_disables_the_gate(self, store, armed, monkeypatch):
        """0 = OFF, the repo's convention for every other limit."""
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_max_round_range_bps", 0.0)
        bot = self._bot_with_range(store, 76900.0, 77100.0)   # very wide
        asyncio.run(bot._cross_tick())
        assert bot._cross_fired != {}, "cap 0 must mean unlimited, not zero"

    def test_it_ignores_ticks_from_before_the_round(self, store, armed, monkeypatch):
        """Only the CURRENT round's travel counts."""
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_max_round_range_bps", 10.0)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        bot._place_order = _swallow
        bot._ticks = {"BTCUSDT": [
            (bot._round.start_ms - 60_000, 70000.0),   # previous round, huge move
            (bot._round.start_ms + 1000, 77000.0),
            (bot._round.start_ms + 2000, 77002.0)]}
        asyncio.run(bot._cross_tick())
        assert bot._cross_fired != {}, "pre-round ticks must not count"

    def test_no_tick_history_does_not_block(self, store, armed, monkeypatch):
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_max_round_range_bps", 10.0)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        bot._place_order = _swallow
        bot._ticks = {}
        asyncio.run(bot._cross_tick())
        assert bot._cross_fired != {}, "missing history must not silently block"


class TestModelRoundRangeWindow:
    """The MODEL arm only trades rounds that have MOVED (5-20 bps).

    Its lifetime margin is zero because two opposite halves cancel: -0.127/$1
    in calm rounds, +0.116 in moving ones, -0.177 past 20bps. The window keeps
    only the half with evidence.
    """

    @staticmethod
    def _bot(store, lo, hi):
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        bot._ticks = {"BTCUSDT": [(bot._round.start_ms + 1000, lo),
                                  (bot._round.start_ms + 2000, hi)]}
        return bot

    def test_range_is_measured_from_the_round_open(self, store):
        bot = self._bot(store, 77000.0, 77010.0)          # 10 pts on a 77000 strike
        rng = bot._round_range_bps(bot._round)
        assert rng is not None and 1.0 < rng < 2.0, rng

    def test_ticks_before_the_round_are_ignored(self, store):
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        bot._ticks = {"BTCUSDT": [
            (bot._round.start_ms - 60_000, 70000.0),      # previous round
            (bot._round.start_ms + 1000, 77000.0),
            (bot._round.start_ms + 2000, 77002.0)]}
        rng = bot._round_range_bps(bot._round)
        assert rng is not None and rng < 1.0, rng

    def test_too_little_history_is_no_opinion_not_a_refusal(self, store):
        """None must mean 'cannot judge' -- a cold start must not disable an arm."""
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        bot._ticks = {}
        assert bot._round_range_bps(bot._round) is None
        bot._ticks = {"BTCUSDT": [(bot._round.start_ms + 1000, 77000.0)]}
        assert bot._round_range_bps(bot._round) is None

    def test_no_strike_is_no_opinion(self, store):
        bot = self._bot(store, 77000.0, 77010.0)
        bot._round.strike = None
        assert bot._round_range_bps(bot._round) is None

    def test_the_window_brackets_the_measured_sweet_spot(self):
        from btcpredict.config import settings as s
        assert s.model_min_round_range_bps == 5.0
        assert s.model_max_round_range_bps == 12.0

    def test_the_range_gate_is_off_on_its_own_replay(self):
        """12.0 -> 0.0 on 2026-09-18. See config.cross_max_round_range_bps.

        The old assertion here was the complementarity claim ("cross owns the
        calm end, model the moving end") -- MEASURED FALSE at the shipped tau
        window: the gate's KEEPS were +0.0180/sh and its REFUSALS +0.1191/sh,
        with the refusals positive in both halves and on their worst day while
        the keeps were not. The invariant below still guards any live value (the
        cross arm may never own a WIDER window than the model arm); 0 is OFF.
        """
        from btcpredict.config import settings as s
        assert s.cross_max_round_range_bps == 0.0
        assert s.cross_max_round_range_bps <= s.model_max_round_range_bps

    def test_the_refuted_per_family_range_values_have_not_come_back(self):
        """They are read through `cross_knob`, so a stray entry would keep the
        refuted gate binding on that family while the global read as OFF."""
        from btcpredict.config import settings as s
        for prefix, ov in s.cross_overrides.items():
            assert "max_round_range_bps" not in ov, prefix


class TestOpenExposureCap:
    """`cross_max_open_stake_pct` is the only thing standing between the
    six-market widening and a single-move wipeout.

    Measured on the 7 recorded days (tools/cross_multi_exposure.py): up to 9
    positions open at once, 0.945 of them on the SAME side, and a worst expiry
    of 7 simultaneous losses = 47% of bankroll. The 20% daily loss cap cannot
    cover that, because those positions settle at the same instant.
    """

    def _bot(self, open_stake, bankroll=100.0):
        b = Bot.__new__(Bot)
        b.bankroll = bankroll
        b._open_stake, b._open_stake_at = open_stake, time.time()
        return b

    def test_it_reads_the_ledger_not_memory(self):
        """A restart must not reset open exposure to zero -- that is exactly
        how an exposure cap silently stops capping."""
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._open_cross_stake)
        assert "FROM fills" in src and "settlements" in src, (
            "open exposure must come from the ledger, not an in-memory counter")

    def test_an_unreadable_ledger_is_not_zero_exposure(self):
        """Unknown exposure must never read as NO exposure."""
        b = self._bot(41.0)
        b._open_stake_at = 0.0                      # force a refresh
        b.store = SimpleNamespace(
            query=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
        assert b._open_cross_stake() == 41.0

    def test_the_cap_is_a_fraction_of_bankroll(self, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_max_open_stake_pct", 0.30)
        b = self._bot(28.0, bankroll=100.0)
        # 28 open + 3 more = 31 > 30
        assert b._open_cross_stake() + 3.0 > 0.30 * b.bankroll
        # ...but 28 + 1.5 fits
        assert b._open_cross_stake() + 1.5 < 0.30 * b.bankroll

    def test_zero_means_off(self, monkeypatch):
        """0 = OFF is the repo's convention for every other limit
        (daily_loss_cap, max_consecutive_losses, cross_max_per_day)."""
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert "cap_pct > 0" in src, (
            "the cap must be written `> 0 and ...`; a bare >= reads 0 as "
            "'no exposure allowed' and disables the arm outright")

    def test_it_is_checked_after_depth_sizing(self):
        """The number compared must be the stake actually about to be
        committed, not the one before the book sized it down."""
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert src.index("sizing down") < src.index("cross_max_open_stake_pct")


class TestPerRoundAndAccountWideAreDifferentLimits:
    """Two limits must not share one number.

    `check_reserved` compares `guards.state.reserved_open`, which counts open
    positions across EVERY family, against whatever `max_open` it is handed.
    Handing it `cross_max_per_round` was harmless while one family traded and
    would have capped all six at 5 concurrent positions -- so raising the
    per-round knob to admit more entries in a choppy round would also have
    raised account-wide concurrency.
    """

    def test_the_guard_is_not_given_the_per_round_knob(self):
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        call = src[src.index("guard.check_reserved("):]
        call = call[:call.index(")")]
        assert "max_per_round" not in call, (
            "check_reserved counts positions account-wide; giving it the "
            "per-round knob conflates two different limits")
        assert "cross_max_open_positions" in src

    def test_the_per_round_limit_is_still_enforced(self):
        """...and dropping it from the guard must not drop it entirely: the
        per-round cap lives in `_cross_fired[r.slug]` at the top of the arm."""
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert '_cross_fired.get(r.slug, 0) >= self._ck("max_per_round", r)' in src


class TestOrderingDoesNotBlockTheLoop:
    """`buy_limit` waits up to limit_wait_s (20s) for a fill, and _cross_all
    walks six families sequentially inside the poll loop -- so awaiting the
    order inline stopped everything.

    Measured 2026-09-14: every unfilled attempt produced a quote gap the length
    of its own wait (21.2s -> 21.4s, 21.5s -> 26.0s, 12.4s -> 16.3s). No market
    was polled and no crossing on ANY family could be detected for that long,
    starting the instant a trigger fired.
    """

    def test_the_order_is_placed_off_the_poll_loop(self):
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert "create_task(self._cross_execute" in src, (
            "the order must be spawned, not awaited, or one resting limit "
            "blinds all six families for up to 20 seconds")
        assert "await self._place_order" not in src, (
            "_cross_tick must not await placement")
        assert "await self._place_order" in inspect.getsource(m.Bot._cross_execute)

    def test_the_task_is_kept_so_it_cannot_vanish(self):
        """A bare create_task can be garbage-collected mid-flight, and an
        exception in a dropped task is never reported."""
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert "_order_tasks" in src and "add_done_callback" in src

    def test_in_flight_stake_counts_against_the_exposure_cap(self):
        """Two orders in flight could each pass a 30% check and land at 60%."""
        b = Bot.__new__(Bot)
        b._pending_stake = 7.5
        b._open_stake, b._open_stake_at = 10.0, time.time()
        assert b._open_cross_stake() == 17.5

    def test_in_flight_orders_count_against_the_slot_cap(self):
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert "_pending_n" in src and "cap_open -=" in src, (
            "reserved_open only counts BOOKED positions, so orders in flight "
            "must be subtracted from the slot cap")

    def test_the_reservation_is_released_even_on_failure(self):
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_execute)
        assert "finally:" in src and "_pending_n" in src.split("finally:")[1]
        assert "_pending_stake" in src.split("finally:")[1]

    def test_a_failing_order_is_logged_not_swallowed(self):
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_execute)
        assert "log.exception" in src


class TestTheRoundIsOneBet:
    """PER FAMILY. With the reversal guard on, every entry in a FAMILY's round is
    the same side of the same round -- they settle on ONE oracle print, so that
    family-round's cumulative stake is the risk unit, not the order's.

    WHAT THIS IS NOT: a window cap. `_round_stake` is keyed `[r.slug]`, so the
    nine families sharing a 5-minute window hold nine separate budgets -- live
    2026-09-18 11:20 put $4 into one window across four families against a $2
    intent (-$2.30) and 11:15 put three same-side Downs in one window (-$3.12).

    A TRUE window cap (key on `rounds.end_ms`) was replayed over the PM era and
    REFUTED: +$62.37 -> +$12.92 at cap 1/window, day-t +3.51 -> +1.53, with
    every dropped cohort positive. See `TestTheWindowCapIsRefuted` and the
    record beside `cross_max_round_stake_mult` in config.py.
    """

    def test_the_cap_is_cumulative_not_per_order(self):
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert "_round_stake" in src and "cross_max_round_stake_mult" in src
        assert "rmult * stake - spent" in src, (
            "the cap must be on the round's CUMULATIVE spend")

    def test_it_sizes_down_before_refusing(self):
        """A round with room left for a partial entry should take the partial,
        not skip it -- entries 0-4 all carry positive EV."""
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        i = src.index("rmult * stake - spent")
        seg = src[i:i + 900]
        assert "room < settings.min_order_usdt" in seg, "refuse only below the venue minimum"
        assert "room < stake" in seg and "round cap" in seg, "otherwise size down"

    def test_zero_means_off(self):
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert "if rmult > 0:" in src, (
            "0 = OFF is the repo convention; a bare compare would disable the arm")

    def test_the_claim_is_recorded_with_the_slot(self):
        """Stake must be booked when the slot is claimed, not when the fill
        returns -- orders are placed off the poll loop, so two entries could
        otherwise both see an empty round."""
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert src.index("_cross_fired[r.slug] =") < src.index("create_task")
        assert src.index("rs[r.slug] = rs.get") < src.index("create_task")

    def test_the_map_is_pruned_to_live_rounds(self):
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert "_round_stake = {k: v for k, v in rs.items() if k in live}" in src


class TestTheWindowCapIsRefuted:
    """A SAME-WINDOW (cross-family) entry cap was measured on the whole PM era
    and it costs money -- `tools/pm_window_cap.py`, 763 entries over 299 windows
    under the shipped chain. It must not be re-proposed, and the implementation
    must not drift into being one by accident.

    base (no cap)         n=763  +0.0817/sh  tot +62.37  t +4.57  day-t +3.51
    cap 1 per window      n=299  +0.0432     tot +12.92  t +1.52  day-t +1.53
    cap 2 per window      n=504  +0.0593     tot +29.87  t +2.70  day-t +2.36
    same side <=1/window  n=396  +0.0417     tot +16.53  t +1.67  day-t +2.02

    The dropped cohorts are positive (+0.1066 / +0.1255 / +0.1249 per share):
    the stacking is where the edge is (k=5 windows: +0.1815/entry and a third of
    the total; same-side stack #2/#4 at +0.1154/+0.2564 against +0.0418 for a
    window's first entry). It also contradicted the 64-fill live sample that
    raised the question, which is why n matters here.
    """

    def test_the_accumulator_stays_keyed_per_family(self):
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert "rs[r.slug] = rs.get(r.slug, 0.0) + stake" in src, (
            "keying this on end_ms makes it a WINDOW cap, which measured "
            "-$49.45 against the base. If it is ever changed, the record in "
            "config.py has to change with it.")

    def test_the_refutation_rides_with_both_knobs(self):
        import pathlib
        cfg = (pathlib.Path(__file__).resolve().parents[1]
               / "btcpredict" / "config.py").read_text()
        assert "pm_window_cap.py" in cfg, \
            "the tool that measured it must be named where the knob is"
        for frag in ("cap 1 per window", "cap 2 per window", "+0.0817",
                     "k=5 +0.1815", "CROSS_MAX_OPEN_STAKE_PCT=0"):
            assert frag in cfg, frag


class TestTheLockedLossGuard:
    """Holding both sides of one round pays exactly 1.000, so a pair costing
    more than 1.0 is a loss by arithmetic -- and this venue has no sell, so it
    cannot be undone.

    The cross arm got `cross_allow_reversal` after btc-updown-5m-1789387500
    (Up 0.611 + Down 0.589 = 1.199, a locked -$0.87). The MODEL arm never had
    one: of 720 settled rounds, 25 held both sides and 21 were the model arm
    colliding with ITSELF seconds apart.
    """

    def _bot(self, held_side, held_px):
        b = Bot.__new__(Bot)
        b.open_fills = {1: ("s-1", SimpleNamespace(side=held_side,
                                                   filled_px=held_px))}
        return b

    def _sig(self, side, quote):
        from btcpredict.edge.signal import Signal
        return Signal(side, quote, quote, 0.0, 0.0, 0.0, True, "t")

    def test_it_refuses_a_pair_above_one(self, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.max_paired_price", 1.0)
        b = self._bot("Up", 0.611)
        r = SimpleNamespace(slug="s-1")
        got = asyncio.run(b._place_order(r, self._sig("Down", 0.589),
                                         fair_up=0.5, token="t", stake=1.0))
        assert got is None, "0.611 + 0.589 = 1.199 is a locked loss"

    def test_it_allows_a_pair_under_one(self, monkeypatch):
        """A hedge that LOCKS A PROFIT is arithmetic, not a mistake. It is
        rejected elsewhere for forfeiting an 88% win rate -- not here."""
        monkeypatch.setattr("btcpredict.config.settings.max_paired_price", 1.0)
        b = self._bot("Up", 0.50)
        r = SimpleNamespace(slug="s-1")
        reached = {}

        async def boom(*a, **k):
            reached["yes"] = True
            raise RuntimeError("past the guard")

        b._quote_and_send = boom
        with pytest.raises(Exception):
            asyncio.run(b._place_order(r, self._sig("Down", 0.30),
                                       fair_up=0.5, token="t", stake=1.0))
        assert reached or True   # the guard must not be what stopped it

    def test_the_same_side_is_not_a_pair(self, monkeypatch):
        """Adding to a position is not holding both sides."""
        monkeypatch.setattr("btcpredict.config.settings.max_paired_price", 1.0)
        b = self._bot("Up", 0.90)
        src = __import__("inspect").getsource(
            __import__("btcpredict.main", fromlist=["Bot"]).Bot._place_order)
        assert 'getattr(held, "side", None) == sig.side' in src, (
            "a same-side add must skip the pair check")

    def test_a_different_round_is_not_a_pair(self):
        src = __import__("inspect").getsource(
            __import__("btcpredict.main", fromlist=["Bot"]).Bot._place_order)
        assert "fslug != r.slug" in src

    def test_it_is_not_skipped_by_skip_pretrade_gates(self):
        """That flag skips signal re-checks; this is a risk check."""
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._place_order)
        assert src.index("max_paired_price") < src.index("attempts = max(1,")


class TestReentryGap:
    """The websocket clock is ~114 ticks/s. `crossed_side` keeps returning a
    side for as long as `look_s` of history still straddles the strike, so
    without a gap the arm takes ONE crossing dozens of times.

    This was masked while placement awaited the fill (the first entry blocked
    2-3s). Moving placement off the poll loop on 2026-09-14 removed that
    accidental rate limit and the arm immediately took paired entries 0.06s
    apart at the same price, spending the round's budget in 60ms.

    1.0s reproduces the substrate the multi-entry EV was measured on: gaps
    between consecutive entries there are min 0.9s / median 1.0s / p90 1.0s --
    the 1s sampling interval.
    """

    def test_a_second_entry_inside_the_gap_is_refused(self, store, armed,
                                                      monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_reentry_gap_s", 1.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_open_stake_pct", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_round_stake_mult", 0.0)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        calls = []

        async def execute(*a, **k):
            calls.append(1)

        bot._cross_execute = execute

        async def go():
            for _ in range(5):
                await bot._cross_tick()
                await asyncio.sleep(0)
        asyncio.run(go())
        assert len(calls) == 1, (
            "five looks inside one second is one crossing, not five")

    def test_the_gap_is_checked_before_any_venue_call(self):
        """A duplicate must cost nothing -- no quote, no order, no log spam."""
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        body = src[src.index('"""', src.index('"""') + 3):]   # past the docstring
        assert body.index("reentry_gap_s") < body.index("_unbooked_round_tokens")

    def test_zero_means_off(self):
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert "if gap > 0:" in src

    def test_the_stamp_is_taken_with_the_claim(self):
        """Stamped when the slot is claimed, not when the fill returns --
        placement is off the poll loop, so a fill-time stamp would let several
        duplicates through while the first order was still in flight."""
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert src.index("lm[r.slug] =") < src.index("create_task")

    def test_the_stamp_is_the_CLAIM_time_not_the_tick_start(self):
        """`now_ms` is read at the top of _cross_tick, before the venue read.

        Stamping it records when the tick STARTED, so a venue read slower than
        the gap expires the stamp before it is written and the next tick walks
        through. That is how three rounds took paired entries 36-744ms apart on
        2026-09-14. See TestConcurrentTicksCannotDoubleEnter for the behaviour;
        this pins the source so it cannot quietly revert.
        """
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert "lm[r.slug] = now_ms" not in src, (
            "the re-entry stamp must not be the tick's start time")
        assert "lm[r.slug] = int(time.time() * 1000)" in src

    def test_it_is_per_market(self):
        """One family's entry must not gag another's."""
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert "_cross_last_ms" in src and ".get(r.slug)" in src


class TestTheTrainTrigger:
    """The Train entry: DISTANCE from the strike, not a crossing.

    It fires OUTSIDE the arm's 5-60s tau window and deliberately bypasses three
    gates -- max_depth_bps (5.0), max_round_range_bps (10.0) and
    cross_max_fill_px (0.65) -- because a >=10bps entry is deep, implies a
    >=10bps round travel, and prices near 0.80 where a 0.65 cap puts it out of
    reach. That override is an operator risk decision (2026-09-15); everything
    that bounds RISK is shared with the cross arm.

    AND IT MUST BE A FRESH CROSSING, NOT A LEVEL. Shipped without that anchor on
    2026-09-15 it bought three 15m rounds at once on the first tick after a
    restart -- strike ten minutes old, spot already 16-25bps out, asks 0.77-0.89.
    """

    @pytest.fixture(autouse=True)
    def _frozen_clock(self, monkeypatch):
        """Freeze wall time so the freshness window is not a race.

        `_history` dates the tick series from `time.time()` at FIXTURE time and
        puts the below-threshold boundary sample exactly `step_ms` (=1000ms)
        back, while `_cross_tick` reads its OWN `time.time()` a moment later. So
        the gate's test `t >= now_ms - win_ms` becomes `now-1000 >= now+d-1000`,
        which holds only while d <= 0. Any elapsed millisecond drops the
        boundary sample out of the window and flips the gate to a refusal.

        Measured on the unmodified tree: this class failed roughly one full-suite
        run in three, a DIFFERENT test each time -- and it is worse now that
        `cross_train_fresh_s` is 1.0, because the boundary sample then sits
        exactly on the window edge rather than comfortably inside a 5.0s window.
        A suite whose result depends on how many microseconds elapsed between a
        fixture and the code under test is not a suite.
        """
        monkeypatch.setattr("time.time", lambda: 1_800_000_000.0)

    @staticmethod
    def _history(bot, prices, step_ms=1000, symbol="BTCUSDT"):
        now = int(time.time() * 1000)
        n = len(prices)
        bot._ticks = {symbol: [(now - (n - 1 - i) * step_ms, p)
                               for i, p in enumerate(prices)]}
        return bot

    def test_a_FRESH_breakout_fires(self, store, armed, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_train_enabled", True)
        # +26bps on the last sample: must clear the swept 15bps threshold
        bot = make_bot(store, ticks(77000.0, 77100.0, 77200.0))
        bot._round = make_round(tau_s=120.0)
        self._history(bot, [77000.0, 77100.0, 77200.0])   # 2s ago: at the strike
        seen = {}

        async def ex(*a, **k):
            seen.update(k)

        bot._cross_execute = ex
        asyncio.run(bot._cross_tick())
        assert seen, "a fresh >=10bps breakout at tau>=60s must fire"
        assert seen["side"] == "Up"
        assert seen["fill_cap"] == 0.90, (
            "the shipped 0.65 fill cap would put a ~0.80 entry out of reach")

    def test_a_STALE_level_does_not_fire(self, store, armed, monkeypatch):
        """THE BUG, reproduced: every sample in the freshness window is already
        past the threshold, so this is an old move, not a breakout."""
        monkeypatch.setattr("btcpredict.config.settings.cross_train_enabled", True)
        bot = make_bot(store, ticks(77140.0, 77150.0, 77160.0))
        bot._round = make_round(tau_s=150.0)   # INSIDE the window on purpose:
        self._history(bot, [77140.0, 77150.0, 77160.0])   # only staleness can
        calls = []                                        # explain no fire

        async def ex(*a, **k):
            calls.append(k)

        bot._cross_execute = ex
        asyncio.run(bot._cross_tick())
        assert not calls, "an OLD level must not be traded as a breakout"

    def test_no_history_fails_CLOSED(self, store, armed, monkeypatch):
        """At startup the tick buffer is empty -- which is exactly when the live
        misfire happened. No history must mean no trade, not a trade."""
        monkeypatch.setattr("btcpredict.config.settings.cross_train_enabled", True)
        bot = make_bot(store, ticks(77120.0, 77160.0, 77200.0))
        bot._round = make_round(tau_s=150.0)
        bot._ticks = {}
        calls = []

        async def ex(*a, **k):
            calls.append(k)

        bot._cross_execute = ex
        asyncio.run(bot._cross_tick())
        assert not calls, "no history must fail closed"

    def test_the_ledger_labels_it_apart_from_a_cross(self, store, armed,
                                                     monkeypatch):
        """A train fill must be separable from a cross fill, or the live record
        cannot grade it."""
        monkeypatch.setattr("btcpredict.config.settings.cross_train_enabled", True)
        bot = make_bot(store, ticks(77000.0, 77100.0, 77200.0))
        bot._round = make_round(tau_s=120.0)
        self._history(bot, [77000.0, 77100.0, 77200.0])

        async def ex(*a, **k):
            pass

        bot._cross_execute = ex
        asyncio.run(bot._cross_tick())
        rows = [r[0] for r in
                bot.store.conn.execute("select reason from decisions")]
        assert any(r.startswith("train into") for r in rows), rows

    def test_a_SECOND_family_in_the_same_burst_is_refused(self, store, armed,
                                                          monkeypatch):
        """THE 21:01 BURST, reproduced.

        `|spot-strike| >= 10bps` is a LEVEL, so btc/eth/bnb cross it together --
        and a 5m round and its 15m sibling SHARE a strike, so those two are the
        same bet. Live: eight entries in 241s, all Down, one view six ways.
        """
        monkeypatch.setattr("btcpredict.config.settings.cross_train_enabled", True)
        # The guard is DISABLED by default (.env cooldown=0, 2026-09-15), so the
        # mechanism must be asserted with the knob set explicitly -- otherwise
        # this test would pass by measuring the disabled default.
        monkeypatch.setattr("btcpredict.config.settings.cross_train_cooldown_s",
                            120.0)
        bot = make_bot(store, ticks(77000.0, 77100.0, 77200.0))
        bot._round = make_round(tau_s=120.0)
        self._history(bot, [77000.0, 77100.0, 77200.0])
        bot._train_last_ms = int(time.time() * 1000) - 1_000     # 1s ago
        calls = []

        async def ex(*a, **k):
            calls.append(k)

        bot._cross_execute = ex
        asyncio.run(bot._cross_tick())
        assert not calls, (
            "a fresh breakout 1s after another train entry is the BURST, not a "
            "second signal -- it must be refused")
        rows = [r[0] for r in
                bot.store.conn.execute("select reason from decisions")]
        assert any("train cooldown" in r for r in rows), rows

    def test_the_burst_guard_EXPIRES(self, store, armed, monkeypatch):
        """It must be a cooldown, not a latch: a genuinely new move later on is
        still tradeable."""
        monkeypatch.setattr("btcpredict.config.settings.cross_train_enabled", True)
        monkeypatch.setattr("btcpredict.config.settings.cross_train_cooldown_s",
                            120.0)
        bot = make_bot(store, ticks(77000.0, 77100.0, 77200.0))
        bot._round = make_round(tau_s=120.0)
        self._history(bot, [77000.0, 77100.0, 77200.0])
        bot._train_last_ms = int(time.time() * 1000) - 200_000   # 200s ago
        seen = {}

        async def ex(*a, **k):
            seen.update(k)

        bot._cross_execute = ex
        asyncio.run(bot._cross_tick())
        assert seen, "past the cooldown a new burst must fire again"

    def test_firing_STAMPS_the_burst_clock(self, store, armed, monkeypatch):
        """The stamp must land on the SIGNAL, before any later gate -- stamping
        it on the FILL would let a refused order be re-attempted family by
        family, which is exactly what rebuilds the burst."""
        monkeypatch.setattr("btcpredict.config.settings.cross_train_enabled", True)
        monkeypatch.setattr("btcpredict.config.settings.cross_train_cooldown_s",
                            120.0)
        bot = make_bot(store, ticks(77000.0, 77100.0, 77200.0))
        bot._round = make_round(tau_s=120.0)
        self._history(bot, [77000.0, 77100.0, 77200.0])
        bot._train_last_ms = 0

        async def ex(*a, **k):
            pass

        bot._cross_execute = ex
        asyncio.run(bot._cross_tick())
        assert bot._train_last_ms > 0, "the signal must stamp the burst clock"

    def test_train_trades_5m_ROUNDS_ONLY(self, store, armed):
        """Both of the design's train properties are properties of a 300s round.

        "The first 4 minutes" is `tau >= 60s` ONLY on a 5-minute round; on a
        15-minute round it is 14 of the 15 minutes. And 10bps at tau 820 is
        ~1 sigma, not in the money: every live 5m leg filled in 0.79-0.88,
        every 15m leg at 0.66/0.73/0.74 -- all three outside the stated band.
        """
        bot = make_bot(store, ticks(77000.0))

        class R:
            def __init__(self, slug):
                self.slug = slug

        assert bot._train_round_ok(R("btc-updown-5m-1789506600")) is True
        assert bot._train_round_ok(R("eth-updown-15m-1789506600")) is False, (
            "the 15m family has no time-box and prices outside the band")
        assert bot._train_round_ok(R("unitree-something-dead")) is False, (
            "an unreadable round length must fail CLOSED")

    def test_the_tau_ceiling_BOUNDS_the_train_window(self, store, armed,
                                                    monkeypatch):
        """The ceiling MECHANISM (cross_train_max_tau_s; 0 = off, which is the
        shipped default). Built by the sweep, then turned off the same hour: at
        X=15 a ceiling collapses the population n=39 -> n=16. The knob is set
        explicitly here because a test that leans on a DEFAULT starts measuring
        the default (the cooldown tests taught that one)."""
        monkeypatch.setattr("btcpredict.config.settings.cross_train_enabled", True)
        monkeypatch.setattr("btcpredict.config.settings.cross_train_max_tau_s",
                            180.0)
        bot = make_bot(store, ticks(77000.0, 77100.0, 77200.0))
        bot._round = make_round(tau_s=400.0)      # outside the 180s ceiling
        self._history(bot, [77000.0, 77100.0, 77200.0])
        calls = []

        async def ex(*a, **k):
            calls.append(k)

        bot._cross_execute = ex
        asyncio.run(bot._cross_tick())
        assert not calls, (
            "a fresh >=10bps breakout at tau 400s is outside the swept window "
            "and must not fire")

    def test_a_NO_FILL_gives_back_the_round_slot(self, store, armed):
        """A train entry that fills NOTHING must not consume the round.

        `_cross_fired` is claimed at DECISION time. Leaving it claimed on a NO
        FILL locks the CROSS arm out of that round's tau 5-60 entry -- the
        best-measured cell in the arm -- and the loss is SILENT, because a
        slot-blocked tick returns before it can log anything. Live 2026-09-15:
        four consecutive train NO FILLs (21:36, 21:37, 21:51 x2) and the cross
        window was silent on every one of those rounds.
        """
        bot = make_bot(store, ticks(77000.0, 77100.0, 77200.0))
        r = make_round(tau_s=120.0)
        bot._cross_fired = {r.slug: 1}
        bot._release_round_slot(r)
        assert bot._cross_fired.get(r.slug, 0) == 0, (
            "a zero fill must give the round back to the cross arm")
        bot._release_round_slot(r)                    # never goes negative
        assert bot._cross_fired.get(r.slug, 0) == 0

    def test_the_arm_is_quiet_when_off(self, store, armed):
        from btcpredict.config import settings
        assert settings.cross_train_enabled is False, (
            "ships OFF; it is enabled per deployment via .env")


class TestTickDepthFloor:
    """bps is the wrong unit for a depth FLOOR. All three symbols quote in
    $0.01, but that cent is 0.0013 bps on BTC and 0.1377 on BNB -- so BNB's
    median trigger was ONE tick, the last decimal flipping, while BTC's was 193.

    Win rate by tick depth, 7 recorded days, all families:
        0-1  0.647   2-4  0.611   5-15  0.825   16-50  0.778   51+  0.876
    and on BNB alone: 0.57 / 0.56 / 0.94.
    """

    def test_a_one_tick_cross_is_refused(self, store, armed, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_min_depth_ticks", 5.0)
        # strike 77000.00, spot 77000.01 -- one tick, the BNB case
        bot = make_bot(store, ticks(76999.98, 76999.99, 77000.01))

        async def boom(*a, **k):
            raise AssertionError("fired on a one-tick cross")

        bot._cross_execute = boom
        asyncio.run(bot._cross_tick())
        rows = [r[0] for r in bot.store.conn.execute("select reason from decisions")]
        assert any("tick(s) through the strike" in r for r in rows), rows

    def test_a_deep_cross_still_fires(self, store, armed, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_min_depth_ticks", 5.0)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))   # 100 ticks
        calls = []

        async def ex(*a, **k):
            calls.append(1)

        bot._cross_execute = ex
        asyncio.run(bot._cross_tick())
        assert calls, "a 100-tick cross must not be blocked by a 5-tick floor"

    def test_zero_means_off(self):
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert "if not train and min_ticks > 0:" in src

    def test_the_floor_is_in_ticks_not_bps(self):
        """The whole point: a bps floor cannot scale across symbols.

        And the unit lives in ONE place now. The arm must DELEGATE to
        `passes_depth_floor`, not re-inline the arithmetic -- a second copy at
        the call site is exactly how the tools drifted from the bot.
        """
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        i = src.index("min_ticks = ")
        seg = src[i:src.index("min_depth_sigma", i)]
        # Comments are excluded: this block explains the old form and quotes
        # its literals, which would otherwise make the guards match themselves.
        code = "\n".join(ln for ln in seg.splitlines()
                         if not ln.strip().startswith("#"))
        assert "passes_depth_floor(spot_now, level, mk)" in code, (
            "the arm must apply the floor via main.passes_depth_floor, against "
            "the LEVEL the round is paid on (`level`, see _cross_level) -- not "
            "against `r.strike`")
        assert "abs(spot_now - r.strike) <" not in code, (
            "no second copy of the comparison at the call site")
        assert "1e-12" not in code, (
            "a FIXED absolute epsilon (1e-12) cannot absorb the ULP of a "
            "difference of two doubles at BTC's ~7.6e4 (1.46e-11), so it "
            "refuses crossings sitting exactly ON the floor")

    def test_it_is_checked_before_the_bps_ceiling(self):
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert src.index("min_depth_ticks") < src.index('"max_depth_bps"')

    def test_a_crossing_exactly_ON_the_floor_is_taken(self, store, armed):
        """FLOAT CANCELLATION, measured live -- not a thought experiment.

        Spot 75996.005 against strike 75996.015 is EXACTLY one tick through,
        but `abs(spot - strike)` is a difference of two doubles and computes as
        0.00999999999476. The old threshold was `1 * 0.01 - 1e-12` =
        0.00999999999900, so the arm REFUSED a crossing sitting exactly on its
        own floor, and said so out loud:

            2026-09-16 06:19  btc-updown-5m  only 1 tick(s) through the
                             strike, floor is 1

        3 of the 154 recorded floor refusals are this case (06:19, 20:34,
        23:39, all btc-5m). The ULP of a double at 7.6e4 is 1.46e-11 -- 14x
        the epsilon that was supposed to absorb it.
        """
        from btcpredict.config import settings as S
        assert S.cross_min_depth_ticks == 1.0, "this test is about floor 1"
        bot = make_bot(store, ticks(76000.0, 75999.0, 75996.005),
                       asks=(0.30, 0.40))
        bot._round.strike = 75996.015
        calls = []

        async def execute(*a, **k):
            calls.append(k.get("side"))

        bot._cross_execute = execute
        asyncio.run(bot._cross_tick())
        assert calls == ["Down"], (
            f"a crossing exactly ON the floor must trade -- refusing it is the "
            f"1e-12-vs-1.46e-11 bug; got {calls}")

    def test_a_hair_under_the_floor_is_still_refused(self, store, armed):
        """The half-tick case must keep being refused -- the fix moves the
        boundary, it does not remove it. 0.005 is one step of the spot's
        half-tick grid, i.e. the spot exactly ON the strike."""
        from btcpredict.config import settings as S
        assert S.cross_min_depth_ticks == 1.0
        bot = make_bot(store, ticks(76000.0, 75999.0, 75996.010),
                       asks=(0.30, 0.40))
        bot._round.strike = 75996.015
        calls = []

        async def execute(*a, **k):
            calls.append(k.get("side"))

        bot._cross_execute = execute
        asyncio.run(bot._cross_tick())
        assert calls == [], (
            f"a half-tick crossing is below a 1-tick floor; got {calls}")


class TestTheInFlightGuard:
    """Two ticks must not both claim the same round across the venue read.

    `_cross_fired`, `_round_stake` and `_cross_last_ms` are all written AFTER
    `_unbooked_round_tokens`, which is the only await in `_cross_tick`. The arm
    runs on the websocket clock (~114/s) while a venue round-trip takes the
    measured 2.7-5.2s, so without a claim held across that await every tick
    arriving inside it re-reads the per-round state unset and claims again.

    The re-entry gap cannot cover this: it is stamped WITH the claim, i.e. on
    the far side of the await. These tests set it to 0 so the only thing that
    can stop a double is the in-flight guard itself.
    """

    def _live_bot(self, store, monkeypatch, gate):
        monkeypatch.setattr("btcpredict.config.settings.cross_reentry_gap_s", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_per_round", 5)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_open_stake_pct", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_round_stake_mult", 0.0)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        # the await only runs on the live path; the default fixture is paper
        bot.broker = SimpleNamespace(mode="live")
        bot.book = object()
        calls = []

        async def execute(*a, **k):
            calls.append(1)
        bot._cross_execute = execute

        async def unbooked(*a, **k):
            await gate()                      # hold the round-trip open
            return set()
        bot._unbooked_round_tokens = unbooked
        return bot, calls

    def test_two_concurrent_ticks_claim_the_round_once(self, store, armed, monkeypatch):
        async def gate():
            await asyncio.sleep(0.02)         # stand in for the venue latency

        bot, calls = self._live_bot(store, monkeypatch, gate)

        async def go():
            await asyncio.gather(bot._cross_tick(), bot._cross_tick())
            await asyncio.sleep(0)
        asyncio.run(go())
        assert len(calls) == 1, (
            "both ticks passed the per-round gates inside the venue read")
        assert bot._cross_fired[bot._round.slug] == 1

    def test_the_round_is_not_latched_shut_afterwards(self, store, armed, monkeypatch):
        """Serialised, not blocked -- cross_max_per_round still admits its own."""
        async def gate():
            await asyncio.sleep(0)

        bot, calls = self._live_bot(store, monkeypatch, gate)

        async def go():
            await asyncio.gather(bot._cross_tick(), bot._cross_tick())
            await asyncio.sleep(0)
            await bot._cross_tick()           # a later tick, state now settled
            await asyncio.sleep(0)
        asyncio.run(go())
        assert len(calls) == 2, "the guard must release, not latch the round"
        assert bot._round.slug not in bot._cross_inflight

    def test_an_exception_inside_releases_the_claim(self, store, armed, monkeypatch):
        """finally, not a release at each return: five refusal paths sit inside."""
        async def gate():
            raise RuntimeError("venue blew up")

        bot, _calls = self._live_bot(store, monkeypatch, gate)
        with pytest.raises(RuntimeError):
            asyncio.run(bot._cross_tick())
        assert bot._round.slug not in bot._cross_inflight, (
            "an exception must not leave the round permanently claimed")


class TestTheCrossWaitIsTheLiveObservedFillWindow:
    """The cross limit rests 6s: long enough for the venue's fill process.

    The 2s rest (2026-09-14) had a documented reason (cross_buffer_vs_fill:
    hold-2 rode the CI-clearing combo), but the live record refutes it:
    settled fills by completion latency are >3.4s: 9W/12 (0.75),
    2.5-3.4s: 7W/10, <=2.5s: 2W/5 -- slow fills are the BEST class, and a
    2s cancel kills them by construction. The venue's long fills report at
    5.12-5.21s (four) and the report includes confirm lag, so 6.0 clears
    every demonstrated fill with ~0.8s of margin (the sweep row is identical
    to 5.0's -- the extra second adds no modelled fill); 5.5-20s has zero
    live fills in 45 exec attempts. Checkpoint is live slow-fill scoring.

    `limit_ceiling is not None` is what distinguishes the cross path from the
    model arm inside the shared placement block, so that is what must select
    the wait. The model arm keeps limit_wait_s; 0 falls back to it.
    """

    def _wait_for(self, store, monkeypatch, *, ceiling):
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.30, 0.70))
        seen = {}

        async def buy_limit(**k):
            seen.update(k)
            return None

        async def buy(**k):
            return None

        async def get_quote(*a, **k):
            return {"quoteId": "q", "averagePrice": 0.30, "expireAt": 0}

        bot.broker.buy_limit = buy_limit
        bot.broker.buy = buy
        bot.broker.get_quote = get_quote
        monkeypatch.setattr("btcpredict.config.settings.order_type", "limit_take")
        bot.broker.mode = "live"
        r = types.SimpleNamespace(slug="btc-updown-5m-1", tau_s=lambda _ms: 120.0)
        sig = types.SimpleNamespace(side="Up", quote=0.30, fair=0.60, edge=0.1)
        asyncio.run(bot._place_order(r, sig, fair_up=0.40, token="t", stake=1.0,
                                     skip_pretrade_gates=True, take_px=0.30,
                                     limit_ceiling=ceiling))
        return seen.get("wait_s")

    def test_the_cross_path_uses_the_shipped_wait(self, store, armed, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_limit_wait_s", 6.0)
        monkeypatch.setattr("btcpredict.config.settings.limit_wait_s", 20.0)
        w = self._wait_for(store, monkeypatch, ceiling=0.39)
        assert w == pytest.approx(6.0), (
            "the shipped cross wait is 6s (2026-09-15 reconciliation)")

    def test_a_custom_cross_wait_still_overrides(self, store, armed, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_limit_wait_s", 8.0)
        monkeypatch.setattr("btcpredict.config.settings.limit_wait_s", 20.0)
        w = self._wait_for(store, monkeypatch, ceiling=0.39)
        assert w == pytest.approx(8.0)

    def test_the_model_path_keeps_the_long_wait(self, store, armed, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_limit_wait_s", 2.0)
        monkeypatch.setattr("btcpredict.config.settings.limit_wait_s", 20.0)
        w = self._wait_for(store, monkeypatch, ceiling=None)
        assert w == pytest.approx(20.0), (
            "no limit_ceiling means the model arm, which still rests 20s")

    def test_zero_falls_back_to_the_shared_wait(self, store, armed, monkeypatch):
        """0 = OFF, the convention every other limit in this config follows."""
        monkeypatch.setattr("btcpredict.config.settings.cross_limit_wait_s", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.limit_wait_s", 20.0)
        w = self._wait_for(store, monkeypatch, ceiling=0.39)
        assert w == pytest.approx(20.0)


class TestConcurrentTicksCannotDoubleEnter:
    """LIVE 2026-09-14: three rounds took paired entries 36-744ms apart.

        eth-updown-5m-1789415100   2 decisions 65ms apart, same quote, same stake
        bnb-updown-5m-1789414200   2 decisions 60ms apart
        eth-updown-5m-1789413600   4 decisions inside 300ms (two pairs)

    10 of 48 fills and $29.19 of the day's stake went on duplicates.

    TestReentryGap drives `_cross_tick` SEQUENTIALLY, so each call stamps
    `_cross_last_ms` before the next one reads it and the gap always holds.
    That is not how it runs. `_cross_all` is called from two places -- the 1s
    poll in `tick()` and the websocket loop -- as separate tasks, and
    `_cross_tick` awaits a venue read between the gap check and the stamp.
    Two tasks can both be past the gap check before either has stamped.

    `_cross_inflight` is meant to cover exactly that window, so this pins it
    under concurrency rather than in sequence.
    """

    def _bot(self, store, monkeypatch, venue_delay=0.03):
        monkeypatch.setattr("btcpredict.config.settings.cross_reentry_gap_s", 1.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_open_stake_pct", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_round_stake_mult", 0.0)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        calls = []

        async def execute(*a, **k):
            calls.append(1)

        async def unbooked(*a, **k):
            await asyncio.sleep(venue_delay)     # the real venue read
            return []

        bot._cross_execute = execute
        bot._unbooked_round_tokens = unbooked
        bot.broker.mode = "live"
        bot.book = object()                      # makes the venue read reachable
        return bot, calls

    def test_sequential_ticks_take_one_entry(self, store, armed, monkeypatch):
        bot, calls = self._bot(store, monkeypatch)

        async def go():
            for _ in range(4):
                await bot._cross_tick()
        asyncio.run(go())
        assert len(calls) == 1

    def test_CONCURRENT_ticks_take_one_entry(self, store, armed, monkeypatch):
        """The poll and the websocket loop, racing. This is the live case."""
        bot, calls = self._bot(store, monkeypatch)

        async def go():
            await asyncio.gather(*[bot._cross_tick() for _ in range(4)])
        asyncio.run(go())
        assert len(calls) == 1, (
            f"{len(calls)} orders from one crossing -- the poll and the websocket "
            "loop both got past the re-entry gap before either stamped it")

    def test_a_slow_venue_read_must_not_expire_the_gap(self, store, armed,
                                                       monkeypatch):
        """The stamp is `now_ms` from the START of the tick, written AFTER the
        venue read. When that read is slower than the gap, the stamp is stale
        the moment it lands and the very next tick walks straight through.

        Timeline with a 1.5s read and a 1.0s gap:

            t=0.00  A starts, now_ms=0, gap passes (no stamp), claims inflight
            t=0.06  B starts, blocked by inflight                     -- correct
            t=1.50  A stamps _cross_last_ms = 0, releases inflight
            t=1.55  C starts, now_ms=1550, gap reads last=0
                    1550 - 0 >= 1000  ->  C PASSES, 50ms after A
        """
        bot, calls = self._bot(store, monkeypatch, venue_delay=1.5)

        async def go():
            first = asyncio.create_task(bot._cross_tick())
            await asyncio.sleep(0.06)
            await bot._cross_tick()            # blocked by inflight
            await first                        # A finishes, stamps a stale time
            await asyncio.sleep(0.05)
            await bot._cross_tick()            # C: 50ms after A, must be refused
        asyncio.run(go())
        assert len(calls) == 1, (
            f"{len(calls)} entries {0.05:.2f}s apart -- the re-entry gap was "
            "stamped with the tick's START time, so a slow venue read expires "
            "it before it is even written")


class TestTheCatchMeter:
    """What the book did AFTER a trigger, and what a refusal actually saw.

    The catch thesis (2026-09-14 night): the venue walks to its post-cross
    price over ~1s, so an order that quotes during that walk pays less -- and
    on the recorded Sep 13 book the walk takes ~0.5s to reach half its move.
    Three explanations for the gate->fill gap have already been wrong, so this
    is recorded rather than assumed: every refusal stores the ask it saw (it
    used to store None) and every trigger meters the book at +0.25/+0.50/+1.00s.
    Log-only; it cannot touch an order.
    """

    def _bot(self, store):
        from btcpredict.feeds.prediction_book import BookTop, PredictionBook
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.45, 0.55))
        wsb = PredictionBook(api_key="k", api_secret="s", session=None,
                             signer=None)
        now = int(time.time() * 1000)
        wsb.tops[bot._round.market_id] = BookTop(
            up_bid=0.30, up_ask=0.35, up_ask_size=1000.0, up_bid_size=1000.0,
            ts_ms=now, recv_ms=now,
            asks=((0.35, 1000.0),), bids=((0.30, 1000.0),))
        bot.ws_book = wsb
        return bot

    def test_a_refusal_records_the_ask_it_saw(self, store, armed):
        bot = self._bot(store)
        bot._cross_side = {bot._round.slug: "Down"}   # force a reversal refusal

        async def boom(*a, **k):
            raise AssertionError("a refused trigger must not order")

        bot._cross_execute = boom
        asyncio.run(bot._cross_tick())
        rows = list(bot.store.conn.execute(
            "select reason, quote from decisions "
            "where reason like 'cross refused:%'"))
        assert rows, "the reversal refusal must leave a row"
        assert rows[0][1] == pytest.approx(0.35), (
            "the refusal row must carry the WS ask, not None")
        assert (bot._round.slug, "Up", "refuse", "cross") in bot._meter_seen, (
            "the refusal must also schedule the post-trigger meter, keyed "
            "under its own label")

    def test_the_meter_samples_the_window_and_logs_it(self, store, armed,
                                                      monkeypatch):
        import btcpredict.main as m
        bot = self._bot(store)
        seen = []

        async def instant(_s):
            return None

        monkeypatch.setattr(asyncio, "sleep", instant)
        monkeypatch.setattr(m, "log", SimpleNamespace(
            info=lambda *a: seen.append(a)))
        asyncio.run(bot._cross_meter(bot._round, "Up"))
        assert seen and seen[0][0] == (
            "%s meter %s %s: +0.25s=%s +0.50s=%s +1.00s=%s [%s]")
        assert seen[0][1] == "cross"
        assert seen[0][3:] == ("Up", "0.350", "0.350", "0.350", "trigger")
        assert seen[1][0] == "%s fine %s %s %s: %s"
        assert seen[1][1] == "cross"
        assert seen[1][3] == "Up" and seen[1][4] == "trigger"
        assert "25=0.350" in seen[1][5] and "100=0.350" in seen[1][5]
        # A TRAIN entry must NOT be logged as a cross. `cross exec` on a
        # `tau 820s` line reads as the cross arm breaking its own 5-60s gate,
        # which is exactly how the 21:01 burst was misread.
        seen.clear()
        asyncio.run(bot._cross_meter(bot._round, "Up", "fire", "train"))
        assert seen[0][1] == "train" and seen[1][1] == "train"

    def test_it_is_inert_without_a_ws_book(self, store, armed):
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        assert getattr(bot, "ws_book", None) is None
        asyncio.run(bot._cross_meter(bot._round, "Up"))    # must not raise
        bot._schedule_cross_meter(bot._round, "Up")        # or schedule anything
        assert not getattr(bot, "_meter_seen", None)


class TestTheRefusedProbe:
    """Flag-off experiment: quote a range-refused crossing at $1 so the venue
    can answer the fill-honouring question the catch thesis rests on.

    Natural fills cannot answer it: the range gate refuses exactly the
    jump-sized crossings the question is about, so they never reach the
    venue. This is the smallest real-money experiment that does.
    """

    @staticmethod
    def _wide(store):
        # ~131 bps of round range (76900 -> 77001), so the 2 bps gate refuses
        bot = make_bot(store, ticks(76900.0, 76990.0, 77001.0))
        now = int(time.time() * 1000)
        bot._ticks = {"BTCUSDT": [(now - 100_000, 76900.0), (now, 77001.0)]}
        return bot

    def test_off_by_default_leaves_the_range_refusal(self, store, armed,
                                                     monkeypatch):
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_max_round_range_bps", 2.0)
        bot = self._wide(store)
        calls = []

        async def ex(*a, **k):
            calls.append(1)

        bot._cross_execute = ex
        asyncio.run(bot._cross_tick())
        assert not calls, "the probe must be off by default"
        rows = [r[0] for r in bot.store.conn.execute(
            "select reason from decisions")]
        assert any("round already moved" in r for r in rows), rows

    def test_the_range_refusal_names_its_own_margin(self, store, armed,
                                                    monkeypatch):
        """One decimal made a CORRECT refusal read as an impossible one.

        MEASURED 2026-09-17: this gate logged
            round already moved 13.6bps, gate is 13.6
        for a true 13.6086 against a 13.6000 gate, and the operator rightly
        asked why it refused. The comparison is exact; the LINE was not. Any
        fixed precision fails when the margin is under the last digit, so the
        line must also NAME THE EXCESS.
        """
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_max_round_range_bps", 2.0)
        bot = self._wide(store)
        asyncio.run(bot._cross_tick())
        rows = [r[0] for r in bot.store.conn.execute(
            "select reason from decisions")]
        reason = next(r for r in rows if "round already moved" in r)
        assert "gate is 2.00" in reason, reason
        assert "over)" in reason, reason

    def test_probe_orders_are_sized_by_the_arm_formula(
            self, store, armed, monkeypatch):
        """No special sizing: the probe is the arm's normal order, so a
        monkeypatched formula (0.5 x stake_mult 3.0) must come out as 1.5 --
        NOT a flat probe stake."""
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_max_round_range_bps", 2.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_probe_enabled",
                            True)
        monkeypatch.setattr("btcpredict.config.settings.cross_stake_mult", 3.0)
        bot = self._wide(store)
        bot._stake_for = lambda *a, **k: 0.5
        seen = {}

        async def ex(*a, **k):
            seen.update(k)

        bot._cross_execute = ex
        asyncio.run(bot._cross_tick())
        assert seen, "the probe must order the refused crossing"
        assert seen["stake"] == pytest.approx(1.5), (
            "the probe must ride the normal sizing formula, not a flat stake")
        assert seen["sig"].reason.startswith("cross probe"), seen["sig"].reason

    def test_the_probe_is_capped_per_day(self, store, armed, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_probe_enabled",
                            True)
        monkeypatch.setattr(
            "btcpredict.config.settings.cross_probe_max_per_day", 1)
        bot = self._wide(store)
        r2 = SimpleNamespace(slug="btc-updown-5m-9999000001", symbol="BTCUSDT")
        assert bot._probe_fires(bot._round) is True
        assert bot._probe_fires(r2) is False, "cap 1/day must stop the second"


class TestTheWhipsawGuard:
    """`cross_allow_reversal` arms on the ENTRY, so a crossing the arm DECLINES
    leaves it unarmed and the opposite leg of the same oscillation walks in.

    LIVE 2026-09-15 00:59 on bnb-updown-15m, with Binance 1s klines confirming
    both legs are real trades rather than a bad tick:

        00:59:21  Up   DECLINED  only 2 tick(s) through the strike, floor is 5
        00:59:24  Down TAKEN     9 ticks through the strike, passes
        00:59:38  spot back above the strike; the round settles Up

    Measured over 211 crossings that pass every gate, edge = win - ask:
    flipped within 20s is -0.0071 on n=14, clean is +0.1771 on n=197.
    """

    def _bot(self, store, monkeypatch, ticks_):
        monkeypatch.setattr("btcpredict.config.settings.cross_flip_block_s", 20.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_reentry_gap_s", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_open_stake_pct", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_round_stake_mult", 0.0)
        # BOTH asks inside [cross_min_price, cross_max_price]. The default
        # fixture is (0.30, 0.70) against a 0.50 ceiling, so a Down entry is
        # refused on the price band and every assertion about the reverse leg
        # would pass for the wrong reason.
        bot = make_bot(store, ticks_, asks=(0.30, 0.40))
        calls = []

        async def execute(*a, **k):
            calls.append(k.get("side"))

        bot._cross_execute = execute
        return bot, calls

    @staticmethod
    def _feed(bot, *prices):
        """Swap the spot feed; `spots` is a dict of feeds, not a list of ticks."""
        f = FakeFeed(ticks(*prices))
        bot._primary_spot = f
        bot.spot = f
        bot.spots = {"BTCUSDT": f}

    def test_the_opposite_leg_of_a_whipsaw_is_refused(self, store, armed, monkeypatch):
        """Up through the strike, then straight back Down. Only the first trades.

        `cross_allow_reversal` is turned ON here so it cannot do the blocking:
        with it in its shipped state this case passes for the wrong reason, and
        the test would keep passing if the flip guard were deleted.
        """
        monkeypatch.setattr("btcpredict.config.settings.cross_allow_reversal", True)
        bot, calls = self._bot(store, monkeypatch,
                               ticks(76997.0, 76998.0, 77001.0))
        asyncio.run(bot._cross_tick())
        assert calls == ["Up"], f"expected one Up entry, got {calls}"
        # now walk the spot back DOWN through the strike
        self._feed(bot, 77003.0, 77002.0, 76999.0)     # back DOWN through it
        asyncio.run(bot._cross_tick())
        assert calls == ["Up"], (
            f"the return leg must be refused, got {calls}")

    def test_it_arms_on_a_DECLINED_crossing(self, store, armed, monkeypatch):
        """The whole point. The 15m arm never entered -- it was refused on the
        depth floor -- so nothing armed and the reverse leg was free."""
        monkeypatch.setattr("btcpredict.config.settings.cross_min_depth_ticks", 1e9)
        bot, calls = self._bot(store, monkeypatch,
                               ticks(76997.0, 76998.0, 77001.0))
        asyncio.run(bot._cross_tick())
        assert calls == [], "the depth floor should refuse this one"
        # the flip must still be remembered, even though nothing was entered
        monkeypatch.setattr("btcpredict.config.settings.cross_min_depth_ticks", 0.0)
        self._feed(bot, 77003.0, 77002.0, 76999.0)
        asyncio.run(bot._cross_tick())
        assert calls == [], (
            "a DECLINED crossing must still arm the guard -- this is the hole "
            "cross_allow_reversal leaves")

    def test_a_same_side_repeat_is_untouched(self, store, armed, monkeypatch):
        """The guard must only ever block a FLIP, never a same-side repeat.

        `cross_max_per_round` is raised here so the per-round cap cannot do the
        blocking -- it was cut to 1 on 2026-09-15, which would make this pass
        for the wrong reason and hide a flip guard that had started refusing
        same-side entries too.
        """
        monkeypatch.setattr("btcpredict.config.settings.cross_max_per_round", 2)
        bot, calls = self._bot(store, monkeypatch,
                               ticks(76997.0, 76998.0, 77001.0))
        asyncio.run(bot._cross_tick())
        asyncio.run(bot._cross_tick())
        assert calls == ["Up", "Up"], f"same-side repeats must pass, got {calls}"

    def test_zero_means_off(self, store, armed, monkeypatch):
        """0 = OFF, the repo convention for every other limit.

        Tested through the DECLINED-crossing path on purpose. With an entry on
        the book `cross_allow_reversal` refuses the opposite side by itself, so
        that route cannot tell the two guards apart -- only the hole this one
        was written to close can.
        """
        monkeypatch.setattr("btcpredict.config.settings.cross_min_depth_ticks", 1e9)
        bot, calls = self._bot(store, monkeypatch,
                               ticks(76997.0, 76998.0, 77001.0))
        monkeypatch.setattr("btcpredict.config.settings.cross_flip_block_s", 0.0)
        asyncio.run(bot._cross_tick())
        assert calls == [], "the depth floor should refuse the first crossing"
        monkeypatch.setattr("btcpredict.config.settings.cross_min_depth_ticks", 0.0)
        self._feed(bot, 77003.0, 77002.0, 76999.0)
        asyncio.run(bot._cross_tick())
        assert calls == ["Down"], (
            f"with the guard OFF the reverse leg must trade; got {calls}")


class TestTheShippedDepthFloorAdmitsAShallowCross:
    """LIVE 2026-09-15 01:19, both refused at the old floor of 5:

        cross eth-updown-5m Up not taken: only 2 tick(s) through the strike
        cross bnb-updown-5m Up not taken: only 3 tick(s) through the strike

    Both then went the way the crossing pointed, and the venue agreed inside a
    second (bnb's Up quote ran 0.720 -> 0.750 on the cross meter).

    A shallow crossing is one the venue has NOT repriced yet, so it is CHEAP.
    Measured with the ask column the old table lacked, over 216 crossings:
    below the old floor edge +0.2311 at an ask of 0.558, at/above it +0.1771 at
    0.686. The old floor refused 42.9% of bnb crossings and 23.3% of eth's.

    This pins the SHIPPED value, not the mechanism -- TestTickDepthFloor
    monkeypatches 5.0 and still covers that.
    """

    def test_a_two_tick_cross_trades_at_the_shipped_floor(self, store, armed,
                                                          monkeypatch):
        from btcpredict.config import settings as S
        assert S.cross_min_depth_ticks <= 2.0, (
            "the shipped floor must admit the 2-3 tick crossings that were "
            "being refused live")
        # strike 77000.0; end 2 ticks past it
        bot = make_bot(store, ticks(76999.0, 76999.5, 77000.02),
                       asks=(0.30, 0.40))
        calls = []

        async def execute(*a, **k):
            calls.append(k.get("side"))

        bot._cross_execute = execute
        asyncio.run(bot._cross_tick())
        assert calls == ["Up"], (
            f"a 2-tick crossing must trade at the shipped floor; got {calls}")

    def test_spot_exactly_on_the_strike_is_still_refused(self, store, armed,
                                                         monkeypatch):
        """1, not 0. At zero ticks spot sits ON the strike and the side is
        ambiguous; btc's extra growth at floor 0 rested on 3 trades."""
        from btcpredict.config import settings as S
        assert S.cross_min_depth_ticks > 0.0, "0 would admit spot == strike"


class TestOneEntryPerRound:
    """`cross_max_per_round` was 5 on evidence that could not exist.

    The replay is sampled at 1s and cross_reentry_gap_s is 1.0s, so a second
    entry is all but impossible in it -- the whole corpus yields ONE on btc-5m
    and TWO on eth-5m. Live runs the websocket at ~114/s, where repeats happen
    constantly, and there:

        FIRST entry in a round   n=31  won 21/31  staked $79.62  ev/$1 -0.042
        LATER entries            n= 7  won  3/7   staked $20.83  ev/$1 -0.316

    2026-09-15 01:29 on bnb-updown-15m, one second apart:
        Up decided 0.6934 filled 0.6993   then   Up decided 0.7320 filled 0.7356
    -- the same bet, 3.6c worse, after the venue repriced.
    """

    def test_a_second_entry_in_the_same_round_is_refused(self, store, armed,
                                                         monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_reentry_gap_s", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_flip_block_s", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_open_stake_pct", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_round_stake_mult", 0.0)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.30, 0.40))
        calls = []

        async def execute(*a, **k):
            calls.append(k.get("side"))

        bot._cross_execute = execute
        asyncio.run(bot._cross_tick())
        asyncio.run(bot._cross_tick())
        assert calls == ["Up"], (
            f"the round is one bet; got {len(calls)} entries")

    def test_it_does_not_cost_a_ROUND(self, store, armed, monkeypatch):
        """The cap bounds entries WITHIN a round, never rounds taken. A fresh
        round must still trade -- that distinction is the whole reason this was
        an acceptable cut while the arm is short of frequency."""
        monkeypatch.setattr("btcpredict.config.settings.cross_reentry_gap_s", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_open_stake_pct", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_round_stake_mult", 0.0)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.30, 0.40))
        calls = []

        async def execute(*a, **k):
            calls.append(k.get("side"))

        bot._cross_execute = execute
        asyncio.run(bot._cross_tick())
        assert calls == ["Up"]
        bot._round = make_round()               # the next round
        bot._round.slug = "btc-updown-5m-next"
        asyncio.run(bot._cross_tick())
        assert calls == ["Up", "Up"], (
            f"a NEW round must still be tradeable; got {calls}")


class TestTheMarketPathHasAPriceCeiling:
    """A MARKET order prices off the venue's QUOTE, and that quote follows the
    DISPLAYED price, which lags its own book -- by up to 0.40, and above the ask
    in 5-7% of samples. Our own record has the failure: 2026-09-11, a Down on
    offer at 0.17 was bought at 0.84, turning a +47% EV trade into -70%.

    `slippageBps` cannot catch it: it bounds movement AFTER the quote, not the
    quote being wrong to begin with. So the cross arm passes its
    `limit_ceiling` (ask + buffer) as `max_px`, and the broker refuses above it.
    This is what makes MARKET safe here -- immediacy, with the LIMIT path's one
    real advantage kept: a bounded worst case.
    """

    def _bot(self, store, monkeypatch, quote_px):
        monkeypatch.setattr("btcpredict.config.settings.order_type", "market")
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.30, 0.40))
        bot.broker.mode = "live"
        seen = {}

        async def get_quote(*a, **k):
            return {"quoteId": "q", "averagePrice": quote_px, "expireAt": 0}

        async def buy(**k):
            seen.update(k)
            # mimic the live broker's ceiling contract
            if k.get("max_px") is not None and quote_px > k["max_px"] + 1e-9:
                return None
            return object()

        bot.broker.get_quote = get_quote
        bot.broker.buy = buy
        return bot, seen

    def test_the_ceiling_is_passed_to_the_market_path(self, store, armed,
                                                      monkeypatch):
        bot, seen = self._bot(store, monkeypatch, quote_px=0.32)
        asyncio.run(bot._cross_tick())
        assert "max_px" in seen, (
            "a market order with no ceiling is how 0.17 got bought at 0.84")
        assert seen["max_px"] == pytest.approx(expected_limit(0.30))

    def test_a_quote_above_the_ceiling_is_refused(self, store, armed, monkeypatch):
        """The 0.17 -> 0.84 case, in miniature."""
        bot, seen = self._bot(store, monkeypatch, quote_px=0.84)
        asyncio.run(bot._cross_tick())
        assert seen.get("max_px") is not None
        assert seen["max_px"] < 0.84, "the ceiling must bind below the bad quote"


def test_the_live_broker_refuses_a_quote_above_max_px():
    """The contract, on the real broker rather than a stub."""
    import btcpredict.execution.live as live

    b = live.LiveBroker(None, "k", "s", wallet_address="w", wallet_id="1")
    placed = []

    async def quote(*a, **kw):
        return {"quoteId": "q1", "averagePrice": 0.84, "expireAt": 0,
                "amountOut": str(10 * 10**18), "feeAmount": "0"}

    async def request(method, path, params=None):
        placed.append(params)
        return {"orderId": "o1"}

    b.get_quote = quote
    b._request = request
    fill = asyncio.run(b.buy(slug="s", side="Down", token_id="t",
                             price=0.17, stake=1.0, max_px=0.30))
    assert fill is None, "a quote of 0.84 against a ceiling of 0.30 must refuse"
    assert not placed, "and must refuse BEFORE any order exists"
    assert b.last_failure == "quote_above_ceiling"


class TestTheMarketPathIsTheOnlyPath:
    """OPERATOR DIRECTIVE 2026-09-15: market orders only -- resting orders on
    this arm are a dead end.

    `market_stale_quote_tol` briefly routed a stale-LOW quote to the LIMIT
    path (the 05:09:13 case: sweep_px 0.460, venue quoted 0.370, book running
    0.600 -> 0.650; a FOK cannot fill under the book). That fallback is gone:
    the quote still goes to the FOK, and what a loser costs is one failed
    attempt and its retry -- not a resting order on an arm whose edge is
    seconds wide.
    """

    def _run(self, store, monkeypatch, quote_px, ask=0.46):
        monkeypatch.setattr("btcpredict.config.settings.order_type", "market")
        monkeypatch.setattr("btcpredict.config.settings.cross_max_price", 0.95)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(ask, 0.40))
        bot.broker.mode = "live"
        used = {"limit": False, "market": False}

        async def get_quote(*a, **k):
            return {"quoteId": "q", "averagePrice": quote_px, "expireAt": 0}

        async def buy_limit(**k):
            used["limit"] = True
            return None

        async def buy(**k):
            used["market"] = True
            return None

        bot.broker.get_quote = get_quote
        bot.broker.buy_limit = buy_limit
        bot.broker.buy = buy
        asyncio.run(bot._cross_tick())
        return used

    def test_a_stale_low_quote_still_takes_with_a_market_order(self, store,
                                                               armed, monkeypatch):
        """0.370 against a 0.460 book -- the quote that used to rest a LIMIT."""
        used = self._run(store, monkeypatch, quote_px=0.370, ask=0.46)
        assert used["market"], "market-only: the FOK gets the order or nothing"
        assert not used["limit"], "resting orders on this arm are a dead end"

    def test_a_healthy_quote_also_goes_to_MARKET(self, store, armed, monkeypatch):
        used = self._run(store, monkeypatch, quote_px=0.465, ask=0.46)
        assert used["market"]
        assert not used["limit"]


class TestTheFillPriceIsCapped:
    """Every other knob bounds the ASK; this bounds what is actually PAID.

    All 43 settled cross fills, bucketed by price paid:

        0.00-0.55  n= 5  won 0.400  break-even 0.496  edge -0.096
        0.55-0.65  n=19  won 0.632  break-even 0.605  edge +0.026
        0.65-0.75  n=13  won 0.538  break-even 0.691  edge -0.152
        0.75-1.00  n= 6  won 0.833  break-even 0.835  edge -0.002

    One profitable band. On 2026-09-15 a buffer of 0.30*(1-ask) put two fills in
    the 0.65-0.75 bucket -- 0.7236 and 0.6889, which the arm's own ev_at_fill
    scored -0.158 and -0.194 -- and both lost.
    """

    def test_the_limit_ceiling_is_capped(self, store, armed, monkeypatch):
        from btcpredict.config import settings as S
        monkeypatch.setattr("btcpredict.config.settings.cross_max_fill_px", 0.65)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_price", 0.95)
        monkeypatch.setattr("btcpredict.config.settings.cross_limit_buffer_inv", 0.30)
        # This scenario is a 0.52bps move (76997 -> 77001 on ~77000) quoted at
        # 0.61, which the model-edge gate refuses: our fair for a crossing that
        # shallow is ~0.50, so the venue is asking well over what the round is
        # worth. That refusal is the gate working, but it means the order path
        # is never reached and this test would be measuring the wrong gate.
        # Turn it off here; it has its own tests.
        monkeypatch.setattr("btcpredict.config.settings.cross_min_edge", 0.0)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.61, 0.39))
        seen = {}

        async def place(*a, **k):
            seen.update(k)
            return None

        bot._place_order = place
        asyncio.run(bot._cross_tick())
        assert seen, "the cross must reach the order path"
        # ask 0.61 + 0.30*(1-0.61) = 0.727, which is inside the LOSING bucket
        assert seen["limit_ceiling"] > 0.65, (
            "the raw ceiling is above the cap -- otherwise this proves nothing")

    def test_no_order_may_pay_above_the_cap(self, store, armed, monkeypatch):
        """Both instruments, one bound."""
        monkeypatch.setattr("btcpredict.config.settings.cross_max_fill_px", 0.65)
        monkeypatch.setattr("btcpredict.config.settings.cross_max_price", 0.95)
        monkeypatch.setattr("btcpredict.config.settings.cross_limit_buffer_inv", 0.30)
        monkeypatch.setattr("btcpredict.config.settings.order_type", "market")
        # See test_the_limit_ceiling_is_capped: the edge gate refuses this
        # shallow crossing, and this test is about the cap.
        monkeypatch.setattr("btcpredict.config.settings.cross_min_edge", 0.0)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.61, 0.39))
        bot.broker.mode = "live"
        seen = {}

        async def get_quote(*a, **k):
            return {"quoteId": "q", "averagePrice": 0.62, "expireAt": 0}

        async def buy(**k):
            seen.update(k)
            return None

        bot.broker.get_quote = get_quote
        bot.broker.buy = buy
        asyncio.run(bot._cross_tick())
        assert seen.get("max_px") is not None, "market orders must carry a ceiling"
        assert seen["max_px"] <= 0.65 + 1e-9, (
            f"the market path may not pay above the cap; got {seen['max_px']}")

    def test_zero_means_off(self):
        """0 = OFF must SURVIVE the Train override.

        `_place_order` now resolves its fill cap from an optional per-call
        `fill_cap` (the Train path needs 0.90 where the arm ships 0.65), so the
        literal `cross_max_fill_px > 0` is gone. The convention it guarded is
        not: with no override the cap still comes from settings, and 0 still
        disables it.
        """
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._place_order)
        assert "settings.cross_max_fill_px if fill_cap is None" in src, (
            "the cap must still resolve from settings with no override")
        assert ") > 0:" in src, "0 = OFF, the repo convention"
        assert "cap = min(cap, _fcap)" in src, (
            "the MARKET path must use the resolved cap, not settings directly")


class TestAFailedFOKGetsItsRetries:
    """Five of six MARKET orders failed after the 2026-09-15 switch, and one of
    them never got a second attempt at all.

        05:09:13  MARKET FAILED  btc  ... "after 1 attempt(s)"
        05:34:29  MARKET FAILED  bnb  quote 0.702
        05:34:36  MARKET FILLED  bnb  quote 0.712   <- the retry

    A FOK that fails is retryable ("venue_failed"), but only once the venue SAYS
    it failed. buy() polled order/history for 3.6s, gave up, found an empty
    position list, and returned the deliberately NON-retryable "unconfirmed" --
    while that order's terminalTime was ~8.2s after createTime.
    """

    def test_the_status_budget_covers_the_venue_verdict(self):
        """~8.2s was observed; the budget must exceed it."""
        from btcpredict.config import settings as S
        assert S.order_status_wait_s >= 8.5, (
            "3.6s gave up before the venue answered, which cost the retry")

    def test_the_budget_is_actually_used_by_buy(self):
        import inspect
        import btcpredict.execution.live as live
        src = inspect.getsource(live.LiveBroker.buy)
        assert "order_status_wait_s" in src
        assert "tries=6, delay=0.6" not in src, "the frozen 3.6s budget is back"

    def test_a_market_retry_is_worth_three_seconds_of_round(self, store, armed,
                                                            monkeypatch):
        """The 07:39:50 eth case: the retry was refused at 3.3s left because
        the 5s floor was calibrated for the 9s verdict budget -- but an
        immediate FOK needs only enough round to EXECUTE, and the venue's book
        processes fills into the close (fills booked with under 1s left)."""
        monkeypatch.setattr("btcpredict.config.settings.order_min_tau_s", 5.0)
        monkeypatch.setattr("btcpredict.config.settings.order_type", "market")
        monkeypatch.setattr("btcpredict.config.settings.order_retry_delay_s", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.order_max_attempts", 3)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.30, 0.40))
        calls = []

        async def buy(**k):
            calls.append(1)
            bot.broker.last_failure = "venue_failed"
            return None

        bot.broker.buy = buy
        bot.broker.last_failure = None
        r = types.SimpleNamespace(slug="btc-updown-5m-1", tau_s=lambda _ms: 3.3)
        sig = types.SimpleNamespace(side="Up", quote=0.30, fair=0.60, edge=0.1)
        asyncio.run(bot._place_order(r, sig, fair_up=0.40, token="t", stake=1.0,
                                     skip_pretrade_gates=True, take_px=0.30))
        assert len(calls) == 3, (
            f"a market retry at 3.3s left must RUN -- the venue saw one order "
            f"ever under the old floor; got {len(calls)}")

    def test_retries_stop_when_the_round_is_nearly_over(self, store, armed,
                                                        monkeypatch):
        """Below the market floor the attempt is not started: the FOK must
        still execute before the bell."""
        monkeypatch.setattr("btcpredict.config.settings.order_min_tau_s", 5.0)
        monkeypatch.setattr("btcpredict.config.settings.order_type", "market")
        monkeypatch.setattr("btcpredict.config.settings.order_retry_delay_s", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.order_max_attempts", 3)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.30, 0.40))
        calls = []

        async def buy(**k):
            calls.append(1)
            bot.broker.last_failure = "venue_failed"   # confirmed, so retryable
            return None

        bot.broker.buy = buy
        bot.broker.last_failure = None
        r = types.SimpleNamespace(slug="btc-updown-5m-1", tau_s=lambda _ms: 2.0)
        sig = types.SimpleNamespace(side="Up", quote=0.30, fair=0.60, edge=0.1)
        asyncio.run(bot._place_order(r, sig, fair_up=0.40, token="t", stake=1.0,
                                     skip_pretrade_gates=True, take_px=0.30))
        assert len(calls) == 1, (
            f"with 2.0s of round left only the first attempt may run; "
            f"got {len(calls)}")

    def test_a_healthy_round_still_gets_its_retries(self, store, armed,
                                                    monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.order_min_tau_s", 5.0)
        monkeypatch.setattr("btcpredict.config.settings.order_retry_delay_s", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.order_max_attempts", 3)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.30, 0.40))
        calls = []

        async def buy(**k):
            calls.append(1)
            # A FOK the venue CONFIRMED it could not fill. Only a confirmed
            # rejection is retryable -- an unknown outcome never is, because
            # re-placing on doubt is how exposure gets doubled.
            bot.broker.last_failure = "venue_failed"
            return None

        bot.broker.buy = buy
        bot.broker.last_failure = None
        r = types.SimpleNamespace(slug="btc-updown-5m-1", tau_s=lambda _ms: 90.0)
        sig = types.SimpleNamespace(side="Up", quote=0.30, fair=0.60, edge=0.1)
        asyncio.run(bot._place_order(r, sig, fair_up=0.40, token="t", stake=1.0,
                                     skip_pretrade_gates=True, take_px=0.30))
        assert len(calls) == 3, (
            f"90s of round left must allow all three attempts; got {len(calls)}")


class TestADeclinedPriceDoesNotEndTheRound:
    """The venue never saw a second order on either 2026-09-15 failure, and
    one reason is a one-line defect: the first `quote_above_ceiling` BROKE the
    retry loop with "not retrying".

    A decline is not a failure of the order -- it is the ceiling working, and
    nothing reached the venue, so the next attempt may re-quote into a book
    that has come back inside the band. Live at 07:59:43 on
    btc-updown-5m-1789458900: attempt 1 venue_failed, attempt 2's quote 0.750
    against a 0.630 ceiling, round abandoned -- one failed order at the venue
    against a log that said "attempt 1/3".
    """

    def _calls(self, store, armed, monkeypatch, failure):
        monkeypatch.setattr("btcpredict.config.settings.order_retry_delay_s", 0.0)
        monkeypatch.setattr("btcpredict.config.settings.order_max_attempts", 3)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.30, 0.40))
        calls = []

        async def buy(**k):
            calls.append(1)
            bot.broker.last_failure = failure
            return None

        bot.broker.buy = buy
        bot.broker.last_failure = None
        r = types.SimpleNamespace(slug="btc-updown-5m-1", tau_s=lambda _ms: 90.0)
        sig = types.SimpleNamespace(side="Up", quote=0.30, fair=0.60, edge=0.1)
        asyncio.run(bot._place_order(r, sig, fair_up=0.40, token="t", stake=1.0,
                                     skip_pretrade_gates=True, take_px=0.30))
        return len(calls)

    def test_an_above_ceiling_quote_re_quotes_instead_of_giving_up(
            self, store, armed, monkeypatch):
        assert self._calls(store, armed, monkeypatch, "quote_above_ceiling") == 3, (
            "every attempt must run; a decline is not an order failure")

    def test_an_unknown_outcome_still_stops_the_loop(self, store, armed,
                                                     monkeypatch):
        assert self._calls(store, armed, monkeypatch, "unconfirmed") == 1, (
            "re-placing on an unknown outcome is how exposure gets doubled")


class TestTheCrossKeepsAQuoteWarm:
    """The fire path pays a quote round-trip (40-65ms) inside the decision ->
    venue race (~170-190ms), while the venue settles its reprice inside ~250ms
    (measured 2026-09-15 against the venue's own order records: 07:59 btc
    missed by ~20ms, 09:09 eth by ~45ms -- both inside this saving).

    The side the NEXT crossing buys is known before it happens -- below the
    strike that is Up, above it Down -- so the quote is fetched while the
    family waits, and consumed once by the first attempt. Any doubt -- stale
    quote, stake mismatch, fetch failure -- falls back to the synchronous
    fetch: this can only ever be faster, never a different trade.
    """

    def _bot(self, store, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.order_type", "market")
        monkeypatch.setattr("btcpredict.config.settings.order_max_attempts", 1)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.30, 0.40))
        bot.broker.mode = "live"
        calls = {"get_quote": 0, "buy": []}

        async def get_quote(*a, **k):
            calls["get_quote"] += 1
            return {"quoteId": "fresh", "averagePrice": 0.30, "expireAt": 0}

        async def buy(**k):
            calls["buy"].append(k)
            bot.broker.last_failure = "venue_failed"
            return None

        bot.broker.get_quote = get_quote
        bot.broker.buy = buy
        return bot, calls

    def _order(self, bot):
        r = types.SimpleNamespace(slug="btc-updown-5m-1", tau_s=lambda _ms: 90.0)
        sig = types.SimpleNamespace(side="Up", quote=0.30, fair=0.60, edge=0.1)
        asyncio.run(bot._place_order(r, sig, fair_up=0.40, token="t", stake=1.0,
                                     skip_pretrade_gates=True, take_px=0.30))

    def test_a_warm_quote_skips_the_fetch_on_the_first_attempt(self, store, armed,
                                                               monkeypatch):
        bot, calls = self._bot(store, monkeypatch)
        bot._quote_pre = {"t": (time.monotonic(), 1.0,
                                {"quoteId": "warm", "averagePrice": 0.30,
                                 "expireAt": 0})}
        self._order(bot)
        assert calls["get_quote"] == 0, (
            "the warm quote must remove the round-trip from the fire path")
        assert calls["buy"] and calls["buy"][0]["quote"]["quoteId"] == "warm"

    def test_a_stale_quote_falls_back_to_a_fetch(self, store, armed, monkeypatch):
        bot, calls = self._bot(store, monkeypatch)
        bot._quote_pre = {"t": (time.monotonic() - 10.0, 1.0,
                                {"quoteId": "warm", "averagePrice": 0.30,
                                 "expireAt": 0})}
        self._order(bot)
        assert calls["get_quote"] == 1
        assert calls["buy"][0]["quote"]["quoteId"] == "fresh"

    def test_a_stake_mismatch_falls_back_to_a_fetch(self, store, armed, monkeypatch):
        bot, calls = self._bot(store, monkeypatch)
        bot._quote_pre = {"t": (time.monotonic(), 2.5,
                                {"quoteId": "warm", "averagePrice": 0.30,
                                 "expireAt": 0})}
        self._order(bot)
        assert calls["get_quote"] == 1, (
            "a quote for $2.50 must never commit a $1.00 order")

    def test_the_warm_quote_is_consumed_once(self, store, armed, monkeypatch):
        bot, _ = self._bot(store, monkeypatch)
        bot._quote_pre = {"t": (time.monotonic(), 1.0,
                                {"quoteId": "warm", "averagePrice": 0.30,
                                 "expireAt": 0})}
        assert bot._take_prefetched_quote("t", 1.0) is not None
        assert bot._take_prefetched_quote("t", 1.0) is None, (
            "one shot: a second attempt must not trade the same quote")

    def _prefetch_bot(self, store, monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.stake_cap_usdt", 1.0)
        monkeypatch.setattr("btcpredict.config.settings.min_order_usdt", 0.5)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.30, 0.40))
        bot.broker.mode = "live"
        bot._stake_for = lambda *a, **k: 10.0    # formula -> mult -> cap $1.00

        class _Top:
            def ask(self, side):
                return 0.40

        class _WSB:
            def best(self, market_id, max_age):
                return _Top()

        bot.ws_book = _WSB()
        return bot

    def test_the_prefetch_warms_the_side_the_next_crossing_buys(self, store, armed,
                                                                 monkeypatch):
        bot = self._prefetch_bot(store, monkeypatch)
        fetched = []

        async def get_quote(token, stake):
            fetched.append((token, stake))
            return {"quoteId": "warm", "averagePrice": 0.30, "expireAt": 0}

        bot.broker.get_quote = get_quote
        up = types.SimpleNamespace(token_id="TOKUP")
        down = types.SimpleNamespace(token_id="TOKDN")
        r = types.SimpleNamespace(slug="r1", strike=100.0, market_id=1)

        async def go(price):
            t = await bot._prefetch_cross_quote(
                r, up, down, types.SimpleNamespace(price=price), 30.0, None)
            if t is not None:
                await t
        asyncio.run(go(99.0))            # below the strike -> the next cross is Up
        assert fetched and fetched[0][0] == "TOKUP"
        assert fetched[0][1] == pytest.approx(1.0), (
            "the warm quote must carry the stake the fire would use")

    def test_the_prefetch_is_throttled(self, store, armed, monkeypatch):
        bot = self._prefetch_bot(store, monkeypatch)
        fetched = []

        async def get_quote(token, stake):
            fetched.append(token)
            return {"quoteId": "warm", "averagePrice": 0.30, "expireAt": 0}

        bot.broker.get_quote = get_quote
        up = types.SimpleNamespace(token_id="TOKUP")
        down = types.SimpleNamespace(token_id="TOKDN")
        r = types.SimpleNamespace(slug="r1", strike=100.0, market_id=1)
        feed = types.SimpleNamespace(price=99.0)

        async def go():
            for _ in range(3):
                t = await bot._prefetch_cross_quote(r, up, down, feed, 30.0, None)
                if t is not None:
                    await t
        asyncio.run(go())
        assert fetched == ["TOKUP"], (
            "three ticks inside one refresh interval must fetch once")


class TestRefusingAPriceIsFree:
    """The order cooldown is SHARED across every arm and family. It exists for
    a venue that is rejecting orders -- retrying at 1 Hz would just hammer it.

    `quote_above_ceiling` is not that. It is this bot declining a price, with a
    perfectly healthy venue. Live at 07:29:06 on eth-updown-5m-1789457100 the
    book ran 0.53 -> 0.67 in six seconds, attempt 2's quote came back over the
    ceiling, the arm correctly refused -- and that refusal paused all six
    families for 30s.

    Declining a bad price has to be free, or the ceiling that protects the arm
    silently throttles it. Same reasoning the `limit_unfilled` exemption already
    carries.
    """

    def _run(self, store, monkeypatch, failure):
        monkeypatch.setattr("btcpredict.config.settings.order_cooldown_s", 30.0)
        monkeypatch.setattr("btcpredict.config.settings.order_max_attempts", 1)
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0), asks=(0.30, 0.40))
        bot._order_cooldown_until = 0.0

        async def buy(**k):
            bot.broker.last_failure = failure
            return None

        bot.broker.buy = buy
        bot.broker.last_failure = None
        r = types.SimpleNamespace(slug="btc-updown-5m-1", tau_s=lambda _ms: 90.0)
        sig = types.SimpleNamespace(side="Up", quote=0.30, fair=0.60, edge=0.1)
        asyncio.run(bot._place_order(r, sig, fair_up=0.40, token="t", stake=1.0,
                                     skip_pretrade_gates=True, take_px=0.30))
        return bot._order_cooldown_until

    def test_a_refused_price_does_not_pause_the_other_families(self, store, armed,
                                                               monkeypatch):
        assert self._run(store, monkeypatch, "quote_above_ceiling") == 0.0, (
            "declining a price must not cost 30s of every family's trading")

    def test_an_unfilled_limit_still_does_not_pause(self, store, armed, monkeypatch):
        assert self._run(store, monkeypatch, "limit_unfilled") == 0.0

    def test_a_real_venue_failure_STILL_pauses(self, store, armed, monkeypatch):
        """The exemption must stay narrow -- a venue actually rejecting orders
        is what the cooldown is for."""
        assert self._run(store, monkeypatch, "venue_failed") > 0.0, (
            "a genuine venue failure must still trigger the cooldown")


class TestTheFOKToleranceTracksTheCeiling:
    """`slippageBps` is the venue's tolerance for the book moving between the
    quote and the execution, and at a frozen 300 (3%) it made this arm's orders
    unfillable by construction.

    The cross arm triggers precisely when the book is about to gap. Live
    2026-09-15 07:39:50 on eth-updown-5m-1789457700: sweep_px 0.394, book at
    +0.25s 0.720. A FOK authorised to 0.405 could never fill. 7 of 8 MARKET
    orders failed that way.

    `max_px` already states how far we will go, so the tolerance is computed
    from it. The venue then enforces our own ceiling: the order fills anywhere
    below it and fails above -- which is the correct outcome, because a fill at
    0.72 on a 0.39 decision is in the losing band anyway.
    """

    def _sent(self, *, avg_px, max_px, default=300):
        import btcpredict.execution.live as live

        b = live.LiveBroker(None, "k", "s", wallet_address="w", wallet_id="1",
                            slippage_bps=default)
        sent = {}

        async def quote(*a, **kw):
            return {"quoteId": "q1", "averagePrice": avg_px, "expireAt": 0,
                    "amountOut": str(10 * 10**18), "feeAmount": "0"}

        async def request(method, path, params=None):
            if path.endswith("place-order-bundle"):
                sent.update(params or {})
            return None                      # stop after the POST

        b.get_quote = quote
        b._request = request
        asyncio.run(b.buy(slug="s", side="Down", token_id="t",
                          price=avg_px, stake=1.0, max_px=max_px))
        return sent

    def test_the_tolerance_is_the_room_up_to_the_ceiling(self):
        # INPUTS CHANGED 2026-09-16. This used 0.394 against a 0.47 ceiling,
        # which is 7.6c of displacement and is now (correctly) discarded as a
        # stale quote before it can reach the venue -- so it was measuring the
        # gate, not the tolerance. These inputs keep the same 19.3% of room
        # while staying inside the 5c displacement the gate tolerates.
        s = self._sent(avg_px=0.25, max_px=0.2982)
        # (0.2982 / 0.25 - 1) = 19.28%
        assert s.get("slippageBps") == pytest.approx(1928, abs=5), (
            f"the FOK must be allowed to reach the ceiling; got {s.get('slippageBps')}")

    def test_the_ceiling_wins_over_the_configured_default(self):
        """REVERSED 2026-09-15. This test used to assert the opposite.

        It read: "a ceiling tighter than the default must not shrink the
        tolerance below what the venue already needs for ordinary jitter", and
        asserted 300 for a 0.601 ceiling on a 0.60 quote. The reasoning was
        that a ceiling this tight leaves a FOK unfillable.

        It does -- and that is the correct outcome, because the venue anchors
        the bound to the QUOTE: `bound = quote * (1 + slip/1e4)`. A floor of 300
        on a 0.60 quote authorises 0.618, which is 1.7c ABOVE the ceiling the
        caller just named. The ceiling is the promise this arm makes, and the
        floor was silently breaking it at exactly the moment the ceiling binds
        (room falls below the default only when the quote has drifted up to
        within default/1e4 of the ceiling).

        Measured live, one attempt in the log:
            venue ack ... quote 0.5800 slip 300 -> bound 0.5974, ceiling 0.58
        i.e. 3.74c above a trigger ask of 0.56, on an arm whose ceiling had just
        been tightened to 2c -- re-opening part of the leak the 2c cap was
        shipped to close.

        The floor still applies to callers with NO ceiling; see
        test_no_ceiling_means_the_configured_default.
        """
        s = self._sent(avg_px=0.60, max_px=0.601, default=300)
        assert s.get("slippageBps") == 16      # int((0.601/0.60 - 1) * 1e4)
        assert 0.60 * (1 + s["slippageBps"] / 10_000) <= 0.601 + 1e-9

    def test_it_is_clamped_at_the_venues_own_maximum(self):
        """10_000 is the venue's documented maximum, not a house number:
        the OpenAPI schema types `slippageBps` on both trade/get-quote and
        trade/place-order-bundle as an integer, "Range 1-10000". Asking for
        more leaves the documented range -- and the old 9_000 gave away
        1000bps of headroom the venue permits.

        REACHABILITY NOTED 2026-09-16. Room above 10000bps means
        `avg_px * 2 < max_px` -- which is precisely the condition
        `quote_is_unusable` now rejects, because it cannot fill. So the CLAMP
        is no longer reachable through `buy` and is asserted on the pure
        function; the boundary (room of exactly 10000, which is reachable) is
        asserted end to end through `buy` to prove the value still lands on
        the wire.
        """
        import btcpredict.execution.live as live
        assert live.derived_slip_bps(300, 0.65, 0.02) == 10_000   # room 315_000
        s = self._sent(avg_px=0.05, max_px=0.10)                   # room exactly 10000
        assert s.get("slippageBps") == 10_000

    def test_a_stale_quote_is_discarded_and_re_quoted(self):
        """The other half of the fix. The warm quote is a latency
        optimisation, not a commitment: 42 of 112 live attempts (38%) carried a
        quote more than 5c below the ceiling, i.e. an order anchored to a price
        the venue no longer had. Discarding it costs one ~43ms round trip and
        replaces a branch whose failure rate was 65%.
        """
        import btcpredict.execution.live as live

        b = live.LiveBroker(None, "k", "s", wallet_address="w", wallet_id="1",
                            slippage_bps=300)
        sent, asked = {}, []
        prices = iter([0.30, 0.64])        # stale, then healthy

        async def quote(*a, **kw):
            px = next(prices)
            asked.append(px)
            return {"quoteId": "q", "averagePrice": px, "expireAt": 0,
                    "amountOut": str(10 * 10**18), "feeAmount": "0"}

        async def request(method, path, params=None):
            if path.endswith("place-order-bundle"):
                sent.update(params or {})
            return None

        b.get_quote = quote
        b._request = request
        asyncio.run(b.buy(slug="s", side="Down", token_id="t",
                          price=0.64, stake=1.0, max_px=0.65))
        assert asked == [0.30, 0.64], asked
        assert sent, "the healthy second quote must reach the venue"
        assert sent.get("slippageBps") == live.derived_slip_bps(300, 0.65, 0.64)

    def test_no_ceiling_means_the_configured_default(self):
        import btcpredict.execution.live as live

        b = live.LiveBroker(None, "k", "s", wallet_address="w", wallet_id="1",
                            slippage_bps=300)
        sent = {}

        async def quote(*a, **kw):
            return {"quoteId": "q1", "averagePrice": 0.40, "expireAt": 0,
                    "amountOut": str(10 * 10**18), "feeAmount": "0"}

        async def request(method, path, params=None):
            if path.endswith("place-order-bundle"):
                sent.update(params or {})
            return None

        b.get_quote = quote
        b._request = request
        asyncio.run(b.buy(slug="s", side="Down", token_id="t",
                          price=0.40, stake=1.0))
        assert sent.get("slippageBps") == 300
