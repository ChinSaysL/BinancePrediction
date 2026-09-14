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
        # ask 0.30 -> buffer 0.25*(1-0.30) = 0.175 -> limit 0.47
        assert seen["fair_up"] == pytest.approx(0.47)
        assert seen.get("limit_ceiling") == pytest.approx(0.47)
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
        # 0.25 * (1 - 0.05) = 0.2375 -> limit 0.29
        assert seen.get("limit_ceiling") == pytest.approx(0.29), (
            "a cheap entry must be given room to chase the reprice")

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
        # 0.25 * (1 - 0.02) = 0.245 -> limit 0.27
        assert seen.get("limit_ceiling") == pytest.approx(0.27)

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
        # Down ask 0.30 -> buffer 0.25*(1-0.30) = 0.175 -> limit 0.47; fair_up
        # is its complement. The invariant under test is unchanged: a Down
        # entry passes 1 - limit, or the taker limit inverts.
        assert seen["fair_up"] == pytest.approx(0.53), (
            "a Down entry passes 1 - limit so the Down side prices at the limit")
        assert seen.get("limit_ceiling") == pytest.approx(0.47)

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
        assert seen.get("limit_ceiling") == pytest.approx(0.40), (
            "ask 0.20 + 0.25 * (1 - 0.20) = 0.40")
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
        bot = make_bot(store, ticks(76997.0, 76998.0, 77001.0))
        calls = []

        async def place(*a, **k):
            calls.append(1)
            return None

        bot._place_order = place
        for _ in range(4):
            asyncio.run(bot._cross_tick())
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

    def test_cross_and_model_windows_are_complementary(self):
        """Each arm's dead zone is the other's edge -- that is the whole design."""
        from btcpredict.config import settings as s
        assert s.cross_max_round_range_bps <= s.model_max_round_range_bps, \
            "cross owns the calm end, model the moving end"


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
        call = src[src.index("check_reserved("):]
        call = call[:call.index(")")]
        assert "max_per_round" not in call, (
            "check_reserved counts positions account-wide; giving it the "
            "per-round knob conflates two different limits")
        assert "cross_max_open_positions" in call

    def test_the_per_round_limit_is_still_enforced(self):
        """...and dropping it from the guard must not drop it entirely: the
        per-round cap lives in `_cross_fired[r.slug]` at the top of the arm."""
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert '_cross_fired.get(r.slug, 0) >= self._ck("max_per_round", r)' in src
