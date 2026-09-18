"""The FAK (IOC) path and the EV ceiling.

WHY THESE TESTS EXIST RATHER THAN A COMMENT. Two properties here are easy to
break silently and expensive to break loudly:

1. A FAK that cannot fill comes back as an EXCEPTION (`RequestRejectedError`),
   not as an accepted order with zero fill. If that is reported as a venue
   failure it lands outside `no_cooldown`, and EVERY miss then pauses orders for
   30 s -- the coupling that already made a tighter ceiling throttle this arm.
2. The ceiling is a PRICE, and the fee is charged AT that price, so
   `P(win) - fee - margin` has the unknown on both sides. Solving it is not
   decoration: the naive form overpays by the fee.

The SDK is not installed on the dev box (only on the VPS), so the broker's
failure matching is deliberately by class NAME -- which lets these tests raise a
local stand-in.
"""
from __future__ import annotations

import ast
import asyncio
import math
import threading
import time
from types import SimpleNamespace

import pytest

from btcpredict.venues.polymarket.broker import (TAKER_FEE_RATE, PmBroker,
                                                 PmBrokerConfig, ev_ceiling)


class RequestRejectedError(Exception):
    """Stand-in for the SDK's, matched by name."""


class InsufficientLiquidityError(Exception):
    pass


class FakeClient:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls: list[dict] = []

    def place_market_order(self, **kw):
        self.calls.append(kw)
        out = self.behaviour
        if isinstance(out, Exception):
            raise out
        return out


def make_broker(behaviour, *, stake=1.0, order_type="fak", plan=None):
    logs: list[str] = []
    br = PmBroker(book=None, client=FakeClient(behaviour),
                  cfg=PmBrokerConfig(order_type=order_type),
                  log=logs.append, creds={})
    p = plan or {
        "slug": "btc-updown-5m-1789590900", "side": "Up",
        "token_id": "tok-up", "ask": 0.58, "sweep_px": 0.58,
        "fillable": stake, "limit": 0.60, "shares": 5, "ceiling": 0.62,
        "tick": 0.01, "book_age_s": 0.2, "bid": 0.56,
        # The FAK path spends THIS, in dollars. It is the stake, not
        # `shares x price`: the venue's 5-share floor is a LIMIT-path rule.
        "spend": stake,
    }
    br.plan = lambda **kw: p                      # bypass the live book
    br._ensure_client = lambda: br._client
    br.logs = logs
    return br, p


# ---------------------------------------------------------------- EV ceiling

class TestTheEVCeiling:
    def test_a_zero_fee_degenerates_to_the_naive_form(self):
        # Where the two agree, they must agree EXACTLY -- otherwise the solver
        # is adding something that is not the fee.
        assert ev_ceiling(0.90, 0.03, fee_rate=0.0) == pytest.approx(0.87)

    def test_the_result_is_a_FIXED_POINT_of_its_own_fee(self):
        """x must satisfy x == w - k*x*(1-x) - m, or we overpaid by the fee."""
        w, m = 0.898, 0.03
        x = ev_ceiling(w, m)
        residual = w - TAKER_FEE_RATE * x * (1.0 - x) - m
        assert x == pytest.approx(residual, abs=1e-12)

    def test_it_lands_where_the_train_sweep_says_it_should(self):
        # The live train setting (thr 15 bps) measured a break-even ask of
        # 0.898. A 3c margin takes the ceiling to ~0.860.
        assert ev_ceiling(0.898, 0.03) == pytest.approx(0.860, abs=0.002)

    def test_a_bigger_margin_lowers_the_ceiling(self):
        xs = [ev_ceiling(0.90, m) for m in (0.0, 0.01, 0.03, 0.06)]
        assert xs == sorted(xs, reverse=True)

    def test_the_fee_makes_it_stricter_than_the_naive_form(self):
        naive = 0.898 - 0.03
        assert ev_ceiling(0.898, 0.03) < naive

    def test_an_edge_that_cannot_cover_the_fee_is_no_trade(self):
        # 0.0 means "do not trade", not "trade at the minimum tick".
        assert ev_ceiling(0.02, 0.03) == 0.0
        assert ev_ceiling(0.0, 0.0) == 0.0
        # fee-free, so this isolates the margin
        assert ev_ceiling(0.52, 0.0, fee_rate=0.0) == pytest.approx(0.52)

    def test_the_fee_is_charged_at_the_ceiling_not_at_the_signal(self):
        """At 0.52 the 7% schedule costs 1.75c/share, and the ceiling must
        absorb exactly that -- pricing the fee off the SIGNAL would understate
        it, because p*(1-p) is largest in the middle."""
        x = ev_ceiling(0.52, 0.0)
        assert x == pytest.approx(0.5025, abs=0.001)
        assert x < ev_ceiling(0.52, 0.0, fee_rate=0.0)

    def test_it_stays_a_price(self):
        for w in (0.5, 0.6, 0.7, 0.8, 0.9, 0.99):
            x = ev_ceiling(w, 0.0)
            assert 0.0 <= x <= 1.0


# ------------------------------------------------------------------- the FAK

class TestTheFAKPath:
    def test_a_kill_is_a_no_fill_not_a_failure(self):
        """THE integration hazard. A kill must NOT read as a venue failure."""
        br, _ = make_broker(RequestRejectedError("no orders to match"))
        assert asyncio.run(br.buy_fak(slug="s", side="Up", token_id="tok-up",
                                      price=0.58, stake=1.0)) is None
        assert br.last_failure == "fak_unfilled"
        assert "no orders to match" in br.last_order_error

    def test_insufficient_liquidity_is_also_a_no_fill(self):
        br, _ = make_broker(InsufficientLiquidityError("thin"))
        assert asyncio.run(br.buy_fak(slug="s", side="Up", token_id="tok-up",
                                      price=0.58, stake=1.0)) is None
        assert br.last_failure == "fak_unfilled"

    def test_a_schema_error_stays_loud(self):
        """`slip 0` shipped once by being classed as a benign rejection."""
        br, _ = make_broker(ValueError("amount must be positive"))
        assert asyncio.run(br.buy_fak(slug="s", side="Up", token_id="tok-up",
                                      price=0.58, stake=1.0)) is None
        assert br.last_failure == "post_failed"
        assert any("FAILED" in m for m in br.logs)

    def test_a_rejected_order_object_is_a_no_fill(self):
        br, _ = make_broker(SimpleNamespace(ok=False, code="x",
                                            message="nothing to match"))
        assert asyncio.run(br.buy_fak(slug="s", side="Up", token_id="tok-up",
                                      price=0.58, stake=1.0)) is None
        assert br.last_failure == "fak_unfilled"

    def test_it_sends_FAK_at_the_ceiling(self):
        br, _ = make_broker(SimpleNamespace(ok=True, order_id="oid",
                                            status="matched"))
        br._confirm = lambda tok, t0, shares: (5.0, 0.60, 0.0168)
        f = asyncio.run(br.buy_fak(slug="btc-1", side="Up", token_id="tok-up",
                                   price=0.58, stake=1.0, max_px=0.62))
        sent = br._client.calls[0]
        assert sent["order_type"] == "FAK"          # IOC, not GTC
        assert sent["max_price"] == 0.62            # the ONLY risk bound
        assert sent["side"] == "BUY"
        # `amount` is the STAKE, in dollars. It used to be the venue's 5-share
        # minimum (`5 x limit`), which turned a $1 order into $3-4.45 at the
        # prices these arms trade -- see `test_the_spend_is_the_stake_not_five_
        # shares` for the measurement that ended that.
        assert sent["amount"] == pytest.approx(1.0)
        assert f is not None and f.filled_px == 0.60 and f.shares == 5.0
        assert not f.unverified

    def test_the_spend_is_the_stake_not_five_shares(self):
        """THE $1-SIZING CONTRACT (measured on the venue 2026-09-17).

        This test used to assert `amount == 5 * 0.89`: the FAK spent the venue's
        5-SHARE minimum, so a nominal $1 stake became $4.45 at a 0.89 ask. On
        the AMOUNT path that floor does not exist -- `place_market_order(
        amount=1.0)` was accepted where $1 is 2.70 shares at a 0.37 ask, while
        the LIMIT path answers `Size (1) lower than the minimum: 5` -- so the
        spend is the stake and the share count is the venue's business.
        """
        high = {"slug": "s", "side": "Up", "token_id": "t", "ask": 0.88,
                "sweep_px": 0.88, "fillable": 1.0, "limit": 0.89,
                "shares": 5, "ceiling": 0.90, "tick": 0.01,
                "book_age_s": 0.1, "bid": 0.86, "spend": 1.0}
        br, _ = make_broker(SimpleNamespace(ok=True, order_id="o"),
                            stake=1.0, plan=high)
        br._confirm = lambda tok, t0, shares: (1.0, 0.88, 0.0)
        import asyncio
        asyncio.run(br.buy_fak(slug="s", side="Up", token_id="t",
                               price=0.88, stake=1.0, max_px=0.90))
        assert br._client.calls[0]["amount"] == pytest.approx(1.0), (
            "a $1 stake must stay $1 at any price -- spending 5 shares "
            "multiplies the stake by up to 5x")

    def test_an_unconfirmed_fill_is_flagged_not_booked_as_clean(self):
        br, _ = make_broker(SimpleNamespace(ok=True, order_id="o"))
        br._confirm = lambda tok, t0, shares: None
        import asyncio
        f = asyncio.run(br.buy_fak(slug="s", side="Up", token_id="tok-up",
                                   price=0.58, stake=1.0))
        assert f is not None and f.unverified
        assert br.last_failure == "unverified"

    def test_no_plan_means_no_order(self):
        br, _ = make_broker(SimpleNamespace(ok=True))
        br.plan = lambda **kw: None
        import asyncio
        assert asyncio.run(br.buy_fak(slug="s", side="Up", token_id="t",
                                      price=0.58, stake=1.0)) is None
        assert br.last_failure == "no_fresh_book"
        assert br._client.calls == []               # nothing was sent

    def test_the_loop_keeps_running_while_the_post_is_in_flight(self):
        """THE MEASURED FIX, enforced by measurement rather than by comment.

        The venue's websocket consumer is an asyncio task on the same loop the
        broker used to block: `place_market_order` is a SYNCHRONOUS SDK call and
        `_confirm` after it is too, so a fire froze everything for 400-1000 ms.
        Live 2026-09-17 20:49:
            24,984  pm FAK no fill ... ack 407 ms
            24,993  pm book ws lagging: venue lag 551ms after draining 399 frames
        399 frames applied 9 ms after the ack, i.e. the book we then priced the
        NEXT decision from was ~400 ms behind, and `tok-lag` at every fire read
        361-486 ms. This test would have failed on that build: the ticker it
        runs beside the order got no turns at all.
        """
        br, _ = make_broker(None)
        ran_on: dict[str, object] = {}
        ticks = {"n": 0}

        def blocking(**_kw):
            ran_on["thread"] = threading.current_thread()
            time.sleep(0.20)                        # one venue POST
            raise RequestRejectedError("no orders found to match")

        br._client.place_market_order = blocking

        async def main():
            async def ticker():
                while True:
                    ticks["n"] += 1
                    await asyncio.sleep(0.01)

            t = asyncio.create_task(ticker())
            try:
                return await br.buy_fak(slug="s", side="Up", token_id="tok-up",
                                        price=0.58, stake=1.0)
            finally:
                t.cancel()

        assert asyncio.run(main()) is None
        assert ran_on["thread"] is not threading.main_thread(), (
            "the SDK call must run on a worker thread, not the event loop")
        # 200 ms of POST against a 10 ms ticker: the loop must have turned many
        # times (it got ZERO with the loop blocked).
        assert ticks["n"] >= 5, f"the loop was starved: {ticks['n']} ticks"


class TestTheSwitch:
    def test_the_default_is_the_limit_path(self):
        # FAK costs +153 ms against a horizon that is already binding, so it
        # must not become the default by accident.
        assert PmBrokerConfig().order_type == "limit"

    def test_buy_delegates_to_fak_when_asked(self):
        import asyncio
        br, _ = make_broker(SimpleNamespace(ok=True, order_id="o"),
                            order_type="fak")
        br._confirm = lambda tok, t0, shares: (5.0, 0.60, 0.0)
        f = asyncio.run(br.buy(slug="s", side="Up", token_id="tok-up",
                               price=0.58, stake=1.0, max_px=0.62))
        assert br._client.calls and br._client.calls[0]["order_type"] == "FAK"
        assert f is not None

    def test_the_limit_path_never_sends_order_type(self):
        """`limit` must keep the 261 ms sign+post shape: a stray FAK here would
        add 153 ms to every order in the arm."""
        import asyncio
        sent = {}

        class C:
            def create_limit_order(self, **kw):
                sent.update(kw)
                return "signed"

            def post_order(self, s):
                return SimpleNamespace(ok=True, order_id="o", status="live")

        br, _ = make_broker(SimpleNamespace(ok=True), order_type="limit")
        br._client = C()
        br._ensure_client = lambda: br._client
        br._confirm = lambda tok, t0, shares: (5.0, 0.60, 0.0)
        asyncio.run(br.buy(slug="s", side="Up", token_id="tok-up",
                           price=0.58, stake=1.0, max_px=0.62))
        assert sent and "order_type" not in sent


def test_main_rates_a_FAK_kill_as_no_cooldown():
    """The name must be in main.py's set, or every miss pauses orders 30 s.

    Read with the AST, not a substring. The failure name is a contract between
    two modules, so a unit test of either side alone would pass while the pair
    broke -- and a substring check on a FORMATTED set literal is not the
    instrument: the first version of this test sliced 600 characters and missed
    the name because the explanatory comment pushed it past the cut. Read the
    parsed set and there is nothing to get wrong.
    """
    src = open("btcpredict/main.py", encoding="utf-8").read()
    found = None
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "no_cooldown"
                   for t in node.targets):
            continue
        assert isinstance(node.value, ast.Set), "no_cooldown is not a set"
        found = {e.value for e in node.value.elts
                 if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    assert found is not None, "no_cooldown = {...} not found in main.py"
    assert "fak_unfilled" in found
    # and the names the ceiling work already relies on must still be there
    for keep in ("limit_unfilled", "quote_above_ceiling"):
        assert keep in found


def test_the_fee_rate_matches_the_venue_schedule():
    # crypto_fees_v2: rate 0.07, takerOnly. If this drifts, the ceiling drifts.
    assert TAKER_FEE_RATE == pytest.approx(0.07)
    assert math.isclose(ev_ceiling(0.9, 0.0, fee_rate=TAKER_FEE_RATE),
                        0.9 - TAKER_FEE_RATE * ev_ceiling(0.9, 0.0) *
                        (1 - ev_ceiling(0.9, 0.0)), abs_tol=1e-12)
