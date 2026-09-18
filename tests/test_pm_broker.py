"""The Polymarket broker: cost model, safety properties, and the latency budget.

The three things this module can get wrong are all invisible in a smoke test,
so each has a test here:

1. **Paying for discovery on the hot path.** The venue charges ~260 ms per API
   call and the order path is `sign + post`. Nothing else may run between them.
   `test_the_hot_path_is_exactly_two_venue_calls` is the regression guard.

2. **Reporting a fill from the ack.** `post_order` returns 200 + an order id
   before the outcome exists. A broker that believes it books phantom P&L, and
   `settle()` would then pay out on a position that was never opened.

3. **Booking P&L on a fill the venue never confirmed.** An unverified fill must
   VOID, not settle, and must never invent a payout.

Fakes throughout: no SDK, no key, no network.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from btcpredict.execution.base import Fill
from btcpredict.venues.polymarket import broker as B
from btcpredict.venues.polymarket.book import Touch
from btcpredict.venues.polymarket.markets import PmRound

SLUG = "btc-updown-5m-1789577400"
UP = "1" * 40
DOWN = "2" * 40


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- fakes
class FakeBook:
    """Just enough of PmBook: `best(token_id, max_age_s) -> Touch | None`."""

    def __init__(self, touches: dict[str, Touch | None]) -> None:
        self.touches = touches
        self.reads = 0

    def best(self, asset_id, max_age_s):
        self.reads += 1
        t = self.touches.get(str(asset_id))
        if t is None:
            return None
        return t if t.age_s() <= max_age_s else None


def touch(asset_id: str, *, bid=0.50, ask=0.52, ask_size=500.0,
          age_ms=0, levels=None) -> Touch:
    import time
    now = int(time.time() * 1000)
    asks = levels if levels is not None else ((ask, ask_size),)
    return Touch(asset_id=asset_id, bid=bid, bid_size=400.0, ask=ask,
                 ask_size=ask_size, ts_ms=now - age_ms,
                 recv_ms=now - age_ms, asks=tuple(asks))


class FakeClient:
    """Mirrors the REAL SDK's shape, which is not the obvious one.

    Deliberate details that are themselves the regression guards:

    * `list_positions` yields objects with **`current_size` and no `size`**.
      The real `Position` has no `size` field, and reading the wrong name
      returns 0 without raising -- which makes every fill look unfilled.
    * There is **no `list_orders`**; the open-order read is `list_open_orders`.
    * `post_order` returns an object with **`ok`**, and a rejection carries
      `code`/`message` and no order id.

    All three were live bugs found by tools/pm_sdk_audit.py.
    """

    def __init__(self, *, trades=None, trades_seq=None, positions=None,
                 open_orders=None, post_raises=False, balance_raw=10_000_000,
                 order_ok=True, reject_code=None, reject_message="",
                 has_list_orders=False) -> None:
        self.calls: list[str] = []
        self.trades = list(trades or [])
        self.trades_seq = list(trades_seq or [])
        self.positions = list(positions or [])
        self.open_orders = list(open_orders or [])
        self.post_raises = post_raises
        self.balance_raw = balance_raw
        self.order_ok = order_ok
        self.reject_code = reject_code
        self.reject_message = reject_message
        self.has_list_orders = has_list_orders
        self.closed = False
        self.signed: list[dict] = []

    # -- the two hot-path calls
    def create_limit_order(self, **kw):
        self.calls.append("create_limit_order")
        self.signed.append(kw)
        return {"signed": kw}

    def post_order(self, signed):
        self.calls.append("post_order")
        if self.post_raises:
            raise RuntimeError("venue said no")
        if not self.order_ok:
            return _ord(None, None, ok=False, code=self.reject_code,
                        message=self.reject_message)
        return _ord("oid-1", "live")

    # -- cold path
    def list_account_trades(self, *, token_id=None, **kw):
        self.calls.append("list_account_trades")
        if self.trades_seq:
            src = self.trades_seq.pop(0) or []
        else:
            src = self.trades
        out = []
        for t in src:
            if token_id is not None and t["asset_id"] != token_id:
                continue
            out.append(_trade(t))
        return out

    def list_positions(self, **kw):
        self.calls.append("list_positions")
        return [_position(p) for p in self.positions]

    def list_open_orders(self, **kw):
        self.calls.append("list_open_orders")
        return [_open_order(o) for o in self.open_orders]

    def get_balance_allowance(self, asset_type="COLLATERAL"):
        self.calls.append("get_balance_allowance")
        return type("B", (), {"balance": self.balance_raw})()

    def cancel_order(self, *, order_id):
        self.calls.append("cancel_order")
        return {"canceled": True}

    def close(self):
        self.closed = True


def _ord(oid, status, *, ok=True, code=None, message=""):
    return type("R", (), {"order_id": oid, "status": status, "ok": ok,
                          "code": code, "message": message})()


def _trade(t):
    return type("T", (), {
        "asset_id": t["asset_id"], "size": t["size"], "price": t["price"],
        "fee_rate_bps": t.get("fee_rate_bps"),
        "trader_side": t.get("trader_side", "TAKER"),
        "matched_at": t.get("matched_at", _now_iso()),
        "transaction_hash": t.get("tx", "0xdead")})()


def _position(p):
    """`current_size`, and NO `size` -- exactly like the real Position."""
    return type("P", (), {
        "asset_id": p["asset_id"], "current_size": p["size"],
        "avg_price": p["price"],
        "current_value": p.get("value", 0.0),
        "redeemable": p.get("redeemable", False),
        "total_pnl": p.get("pnl", 0.0),
        "entry_fees_usdc": p.get("fees", 0.0),
        "condition_id": p.get("condition_id", "")})()


def _open_order(o):
    return type("O", (), {
        "id": o.get("id", "o1"), "asset_id": o.get("asset_id", UP),
        "price": o.get("price", 0.5), "original_size": o.get("size", 5.0),
        "size_matched": o.get("filled", 0.0),
        "status": o.get("status", "LIVE"),
        "order_type": o.get("order_type", "GTC")})()


def make_broker(book=None, client=None, **cfg_kw):
    cfg = B.PmBrokerConfig(**cfg_kw)
    br = B.PmBroker(book=book or FakeBook({}), client=client, cfg=cfg,
                    log=lambda m: None)
    br._rounds.by_slug[SLUG] = PmRound(
        slug=SLUG, start_ts=1789577400, condition_id="0xcond",
        up_token=UP, down_token=DOWN, tick=0.01, min_order=5.0,
        end_ms=1789577700_000, neg_risk=False, liquidity=50_000.0)
    return br


# ---------------------------------------------------------------- ticks
class TestTickRounding:
    def test_a_buy_ceiling_rounds_up(self):
        # Rounding a ceiling DOWN can drop the limit under the ask we are
        # trying to cross, turning a marketable order into a resting one.
        assert B._round_to_tick(0.5201, 0.01, up=True) == pytest.approx(0.53)

    def test_a_floor_rounds_down(self):
        assert B._round_to_tick(0.5299, 0.01, up=False) == pytest.approx(0.52)

    def test_exact_grid_values_do_not_drift(self):
        for px in (0.34, 0.65, 0.90):
            assert B._round_to_tick(px, 0.01, up=True) == pytest.approx(px)

    def test_a_degenerate_tick_is_ignored(self):
        assert B._round_to_tick(0.5237, 0.0, up=True) == pytest.approx(0.5237)


# ---------------------------------------------------------------- fees
class TestTakerFee:
    def test_it_matches_the_venues_own_schedule(self):
        # feeSchedule {rate 0.07, exponent 1, takerOnly true}
        assert B.taker_fee(0.50, 10) == pytest.approx(0.07 * 0.5 * 0.5 * 10)

    def test_it_is_worth_less_at_the_extremes(self):
        # The whole point of p(1-p): a 0.95 contract pays almost nothing.
        assert B.taker_fee(0.95, 100) < B.taker_fee(0.50, 100)

    def test_the_maker_side_pays_zero(self):
        assert B.taker_fee(0.5, 100, rate=0.0) == 0.0


# ---------------------------------------------------------------- planning
class TestPlan:
    def test_it_refuses_a_stale_book(self):
        # Fails CLOSED: a frozen feed that prices every order off a dead book
        # is worse than no price at all.
        b = FakeBook({UP: touch(UP, age_ms=9000)})
        assert make_broker(book=b, max_book_age_s=2.0).plan(
            slug=SLUG, side="Up", stake=2.0) is None

    def test_it_refuses_a_missing_touch(self):
        assert make_broker(book=FakeBook({})).plan(
            slug=SLUG, side="Up", stake=2.0) is None

    def test_the_limit_never_exceeds_the_ceiling(self):
        b = FakeBook({UP: touch(UP, ask=0.60)})
        p = make_broker(book=b).plan(slug=SLUG, side="Up", stake=2.0,
                                     max_px=0.55)
        assert p is not None and p["limit"] <= 0.55 + 1e-9

    def test_the_ceiling_drops_a_tick_rather_than_rounding_over_it(self):
        # want = min(0.60+0.02, 0.605) = 0.605 -> ceil to 0.61 > ceiling.
        # The legal answer is the tick BELOW the ceiling, not 0.61.
        b = FakeBook({UP: touch(UP, ask=0.60)})
        p = make_broker(book=b).plan(slug=SLUG, side="Up", stake=2.0,
                                     max_px=0.605)
        assert p["limit"] == pytest.approx(0.60)

    def test_it_reaches_past_the_ask_by_the_configured_slippage(self):
        b = FakeBook({UP: touch(UP, ask=0.52)})
        p = make_broker(book=b, slippage=0.02).plan(
            slug=SLUG, side="Up", stake=2.0)
        assert p["limit"] == pytest.approx(0.54)

    def test_it_prices_the_sweep_not_the_touch(self):
        # Gating on the touch prices the order against liquidity that may be
        # one tick wide. Here the touch holds 1 share and the rest is a tick
        # up, so the sweep price must be worse than the ask.
        b = FakeBook({UP: touch(UP, ask=0.50, ask_size=1.0,
                                levels=((0.50, 1.0), (0.90, 100.0)))})
        p = make_broker(book=b).plan(slug=SLUG, side="Up", stake=20.0)
        assert p["sweep_px"] > p["ask"]

    def test_it_takes_the_venue_minimum_in_shares(self):
        b = FakeBook({UP: touch(UP, ask=0.90)})
        p = make_broker(book=b).plan(slug=SLUG, side="Up", stake=1.0)
        assert p["shares"] >= 5.0          # orderMinSize, measured

    def test_the_rounds_token_is_used_not_the_callers(self):
        # A wrong token id buys the wrong side; the round's id comes from the
        # venue and wins.
        b = FakeBook({UP: touch(UP), DOWN: None})
        p = make_broker(book=b).plan(slug=SLUG, side="Up", stake=2.0)
        assert p["token_id"] == UP


# ---------------------------------------------------------------- hot path
class TestTheHotPath:
    def test_the_hot_path_is_exactly_two_venue_calls(self):
        """sign + post. Anything more is discovery leaking onto the hot path.

        260 ms per call, so a third call is +260 ms against a 250 ms edge.
        """
        c = FakeClient(trades=[{"asset_id": UP, "size": 4.0, "price": 0.52}])
        b = FakeBook({UP: touch(UP)})
        br = make_broker(book=b, client=c)
        f = asyncio.run(br.buy(slug=SLUG, side="Up", token_id=UP,
                               price=0.52, stake=2.0))
        assert f is not None
        venue = [x for x in c.calls
                 if x in ("create_limit_order", "post_order")]
        assert venue == ["create_limit_order", "post_order"]

    def test_the_signer_is_primed_outside_the_order(self):
        # The first sign costs ~500 ms; it must be spent at boot.
        c = FakeClient()
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c)
        br.prime()
        n = len(c.calls)
        c.calls.clear()
        asyncio.run(br.buy(slug=SLUG, side="Up", token_id=UP,
                           price=0.52, stake=2.0))
        assert len([x for x in c.calls if x == "create_limit_order"]) == 1
        assert n >= 1

    def test_balance_is_cached_off_the_hot_path(self):
        # A 260 ms call that has no business in a 280 ms budget.
        c = FakeClient(balance_raw=10_000_000)
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c)
        assert asyncio.run(br.balance()) == pytest.approx(10.0)
        asyncio.run(br.balance())
        asyncio.run(br.balance())
        assert c.calls.count("get_balance_allowance") == 1

    def test_equity_equals_collateral_and_does_not_double_count(self):
        # A position is worth nothing until the round settles, and the money
        # spent on it has already left the collateral. Adding open exposure
        # back would count it twice.
        c = FakeClient(balance_raw=8_100_000)
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c)
        assert asyncio.run(br.equity()) == pytest.approx(8.1)
        assert asyncio.run(br.equity()) == pytest.approx(8.1)

    def test_a_failed_post_returns_none_and_books_nothing(self):
        c = FakeClient(post_raises=True,
                       trades=[{"asset_id": UP, "size": 4.0, "price": 0.52}])
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c)
        assert asyncio.run(br.buy(slug=SLUG, side="Up", token_id=UP,
                                  price=0.52, stake=2.0)) is None

    def test_a_refused_plan_never_reaches_the_venue(self):
        c = FakeClient()
        br = make_broker(book=FakeBook({}), client=c)      # no book at all
        assert asyncio.run(br.buy(slug=SLUG, side="Up", token_id=UP,
                                  price=0.52, stake=2.0)) is None
        assert c.calls == []


# ---------------------------------------------------------------- the ack trap
class TestTheAckIsNotAFill:
    def test_an_empty_account_yields_an_UNVERIFIED_fill(self):
        # The order POSTed and returned 200. The venue still holds nothing.
        # Believing the ack is how a broker books phantom P&L.
        c = FakeClient()
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c,
                         confirm_budget_s=0.2)
        f = asyncio.run(br.buy(slug=SLUG, side="Up", token_id=UP,
                               price=0.52, stake=2.0))
        assert f is not None and f.unverified is True

    def test_the_real_position_wins_over_the_request(self):
        # Requested ~4 shares at the limit; the venue recorded 1.7 at 0.379.
        # A partial fill priced at the limit is a fabricated fill.
        c = FakeClient(trades=[{"asset_id": UP, "size": 1.7, "price": 0.379}])
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c)
        f = asyncio.run(br.buy(slug=SLUG, side="Up", token_id=UP,
                               price=0.52, stake=2.0))
        assert f.shares == pytest.approx(1.7)
        assert f.filled_px == pytest.approx(0.379)
        assert f.stake == pytest.approx(1.7 * 0.379)
        assert f.unverified is False

    def test_a_position_appearing_late_is_still_caught(self):
        # Measured: a post-only bid filled in the SAME second, and the cancel
        # ack claimed the order could not be found. The read must retry.
        c = FakeClient(trades_seq=[[], [],
                                   [{"asset_id": UP, "size": 5.0,
                                     "price": 0.38}]])
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c,
                         confirm_budget_s=2.0)
        f = asyncio.run(br.buy(slug=SLUG, side="Up", token_id=UP,
                               price=0.52, stake=2.0))
        assert f.unverified is False
        assert f.shares == pytest.approx(5.0)

    def test_a_fill_carries_the_venues_fee_schedule(self):
        c = FakeClient(trades=[{"asset_id": UP, "size": 5.0, "price": 0.50}])
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c)
        f = asyncio.run(br.buy(slug=SLUG, side="Up", token_id=UP,
                               price=0.52, stake=2.0))
        assert f.fee_paid == pytest.approx(0.07 * 0.5 * 0.5 * 5.0)


# ---------------------------------------------------------------- settlement
class TestSettlement:
    def _fill(self, side="Up", shares=5.0, px=0.50, unverified=False) -> Fill:
        return Fill(slug=SLUG, side=side, requested_px=px, filled_px=px,
                    shares=shares, stake=shares * px,
                    fee_paid=B.taker_fee(px, shares), order_id="oid-1",
                    ts_ms=0, token_id=UP, unverified=unverified)

    def test_a_winner_pays_shares_minus_cost(self):
        br = make_broker(book=FakeBook({}), client=FakeClient())
        s = asyncio.run(br.settle(self._fill(), "Up"))
        assert s.won is True and s.payout == pytest.approx(5.0)
        want = 5.0 - 5.0 * 0.50 - B.taker_fee(0.50, 5.0)
        assert s.pnl == pytest.approx(want)

    def test_a_loser_pays_nothing(self):
        br = make_broker(book=FakeBook({}), client=FakeClient())
        s = asyncio.run(br.settle(self._fill(), "Down"))
        assert s.won is False and s.payout == 0.0 and s.pnl < 0

    def test_an_unverified_fill_that_never_filled_is_VOID(self):
        # No trade ever appears -> nothing was staked -> the recorded P&L
        # must be zero, not a fabricated loss on a phantom 5 shares.
        c = FakeClient()
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c,
                         confirm_budget_s=0.2)
        s = asyncio.run(br.settle(self._fill(unverified=True), "Up"))
        assert s.void is True and s.pnl == 0.0 and s.payout == 0.0

    def test_an_unverified_fill_that_DID_fill_is_reconciled_first(self):
        c = FakeClient(trades=[{"asset_id": UP, "size": 5.0, "price": 0.38}])
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c,
                         confirm_budget_s=0.5)
        f = self._fill(unverified=True)
        s = asyncio.run(br.settle(f, "Up"))
        assert s.void is False and f.unverified is False
        assert f.stake == pytest.approx(5.0 * 0.38)
        assert s.pnl == pytest.approx(5.0 - 5.0 * 0.38
                                      - B.taker_fee(0.38, 5.0))

    def test_the_winning_side_is_the_fills_side_not_the_favourites(self):
        br = make_broker(book=FakeBook({}), client=FakeClient())
        s = asyncio.run(br.settle(self._fill(side="Down"), "Down"))
        assert s.won is True


class TestItSatisfiesTheBrokerProtocol:
    def test_the_shape_matches_execution_base(self):
        # An arm must be pointable at either venue with no second code path.
        from btcpredict.execution.base import Broker
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=FakeClient())
        assert isinstance(br, Broker)
        assert br.mode == "live"


class TestTheSurfaceMainPyActuallyUses:
    """main.py reads more than the protocol: equity, positions, buy_limit,
    active_orders, cancel_orders, redeem. Each is asserted here so a venue
    swap fails in a test rather than at the first order of a live round."""

    def test_positions_are_keyed_the_way_main_py_reads_them(self):
        # `_venue_position_tokens` builds {p.get("tokenId") for p in pos}.
        c = FakeClient(positions=[{"asset_id": UP, "size": 5.0,
                                   "price": 0.38}])
        br = make_broker(book=FakeBook({}), client=c)
        pos = asyncio.run(br.positions())
        assert pos and pos[0]["tokenId"] == UP
        assert pos[0]["size"] == pytest.approx(5.0)
        assert pos[0]["avgPrice"] == pytest.approx(0.38)

    def test_a_zero_size_position_is_not_reported(self):
        c = FakeClient(positions=[{"asset_id": UP, "size": 0.0,
                                   "price": 0.0}])
        br = make_broker(book=FakeBook({}), client=c)
        assert asyncio.run(br.positions()) == []

    def test_a_failed_position_read_is_not_an_empty_account(self):
        # An empty list means "holds nothing"; a failed read means "cannot
        # see". The two must be distinguishable, because the arm sits out on
        # exposure it cannot verify.
        class Bad(FakeClient):
            def list_positions(self):
                raise RuntimeError("transport down")

        br = make_broker(book=FakeBook({}), client=Bad())
        assert asyncio.run(br.positions()) == []
        assert br.last_failure == "positions_read_failed"

    def test_the_book_is_offered_in_the_wrappers_shape(self):
        b = FakeBook({UP: touch(UP, bid=0.48, ask=0.52, ask_size=300.0)})
        br = make_broker(book=b, client=FakeClient())
        ob = asyncio.run(br.order_book(UP, market_id=12345))
        assert ob["bid"] == pytest.approx(0.48)
        assert ob["ask"] == pytest.approx(0.52)
        assert ob["askSize"] == pytest.approx(300.0)
        assert asyncio.run(br.best_prices(UP, 1)) == pytest.approx((0.48, 0.52))

    def test_the_market_id_argument_is_accepted_and_ignored(self):
        # The wrapper needs it; this venue addresses a book by token id alone.
        # The parameter exists so an arm can be repointed unchanged.
        b = FakeBook({UP: touch(UP)})
        br = make_broker(book=b, client=FakeClient())
        assert asyncio.run(br.order_book(UP, 999)) is not None

    def test_a_stale_book_yields_no_order_book_rather_than_a_price(self):
        b = FakeBook({UP: touch(UP, age_ms=30_000)})
        br = make_broker(book=b, client=FakeClient(), max_book_age_s=2.0)
        assert asyncio.run(br.order_book(UP)) is None
        assert asyncio.run(br.best_prices(UP)) is None

    def test_a_dry_account_reports_nothing_redeemable(self):
        c = FakeClient(positions=[{"asset_id": UP, "size": 5.0,
                                   "price": 0.38}])
        br = make_broker(book=FakeBook({}), client=c)
        assert asyncio.run(br.claimable_token_ids()) == []
        out = asyncio.run(br.redeem())
        assert out["reason"] == "nothing_redeemable"

    def test_an_already_swept_position_is_a_SUCCESS_not_an_error(self):
        # The AutoRedeemer sweeps winners gaslessly ~10 s after they become
        # redeemable, so redeem_positions raises "No market found for
        # condition 0x..." -- which reads like a lookup bug and means
        # "nothing to do".
        #
        # The signature below is the REAL one, verbatim, so a regression back
        # to `redeem_positions(token_id=...)` raises TypeError here instead of
        # silently claiming nothing on the live wallet for hours (measured
        # 2026-09-16: 20 attempts, 0 successes).
        class Swept(FakeClient):
            def redeem_positions(self, *, condition_id=None, market_id=None,
                                 position_id=None, metadata=None):
                raise RuntimeError("No market found for condition 0xdead")

        c = Swept(positions=[{"asset_id": UP, "size": 5.0, "price": 0.38,
                              "redeemable": True, "condition_id": "0xcond"}])
        br = make_broker(book=FakeBook({}), client=c)
        out = asyncio.run(br.redeem([UP]))
        assert out["redeemed"] == [UP]

    def test_a_genuine_redeem_failure_is_not_reported_as_done(self):
        class Broken(FakeClient):
            def redeem_positions(self, *, condition_id=None, market_id=None,
                                 position_id=None, metadata=None):
                raise RuntimeError("insufficient gas")

        c = Broken(positions=[{"asset_id": UP, "size": 5.0, "price": 0.38,
                               "redeemable": True, "condition_id": "0xcond"}])
        br = make_broker(book=FakeBook({}), client=c)
        assert asyncio.run(br.redeem([UP]))["redeemed"] == []

    def test_redeem_is_addressed_by_condition_and_waited_on(self):
        """Both halves of the 2026-09-16 failure, pinned together.

        The call must name `condition_id` (a token id is a TypeError), and the
        returned transaction handle must be `wait()`ed -- dispatching without
        waiting reports a claim that may never land.
        """
        seen = []

        class Handle:
            def wait(self):
                seen.append("wait")

        class Ok(FakeClient):
            def redeem_positions(self, *, condition_id=None, market_id=None,
                                 position_id=None, metadata=None):
                seen.append(condition_id)
                return Handle()

        c = Ok(positions=[{"asset_id": UP, "size": 5.0, "price": 0.38,
                           "redeemable": True, "condition_id": "0xcond"},
                          {"asset_id": DOWN, "size": 5.0, "price": 0.38,
                           "redeemable": True, "condition_id": "0xcond"}])
        br = make_broker(book=FakeBook({}), client=c)
        out = asyncio.run(br.redeem())
        # ONE call for the pair: both tokens of a market share a condition, so
        # the two positions are redeemed by a single condition id and the
        # result names the one token that key was taken from.
        assert seen == ["0xcond", "wait"]
        assert out["redeemed"] == [UP]

    def test_a_position_with_no_condition_id_is_left_alone(self):
        """Redeeming by the wrong key is what broke this. Without a condition
        id there is nothing correct to send, so it is skipped rather than
        guessed at."""
        c = FakeClient(positions=[{"asset_id": UP, "size": 5.0,
                                   "price": 0.38, "redeemable": True}])
        br = make_broker(book=FakeBook({}), client=c)
        assert asyncio.run(br.redeem()) == {"redeemed": [],
                                            "reason": "nothing_redeemable"}

    def test_cancel_returns_only_the_ids_the_venue_accepted(self):
        class Partial(FakeClient):
            def cancel_order(self, *, order_id):
                if order_id == "bad":
                    raise RuntimeError("not found")
                return {"canceled": True}

        br = make_broker(book=FakeBook({}), client=Partial())
        assert asyncio.run(br.cancel_orders(["a", "bad", "b"])) == ["a", "b"]

    def test_last_failure_names_why_buy_refused(self):
        # The caller needs to tell a safe-to-retry rejection from an unknown
        # outcome; a bare None cannot express that.
        br = make_broker(book=FakeBook({}), client=FakeClient())
        asyncio.run(br.buy(slug=SLUG, side="Up", token_id=UP, price=0.5,
                           stake=2.0))
        assert br.last_failure == "no_fresh_book"

    def test_last_order_error_keeps_the_venues_own_words(self):
        c = FakeClient(post_raises=True)
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c)
        asyncio.run(br.buy(slug=SLUG, side="Up", token_id=UP, price=0.52,
                           stake=2.0))
        assert br.last_failure == "post_failed"
        assert "venue said no" in (br.last_order_error or "")


class TestTheMakerPathIsHonest:
    """`buy_limit` exists to be MEASURED, not because it is expected to win.

    A resting order fills when the book comes to it, and the realized win rate
    lands 3-9 pp below the limit paid in every configuration tried (both
    venues). These tests pin the mechanics, not a profit claim.
    """

    def test_a_maker_fill_pays_no_fee(self):
        # feeSchedule is takerOnly; the one live trade measured paid $0.00 and
        # the venue attributed it MAKER. So a maker fill must NOT pick up the
        # taker schedule -- this test caught exactly that.
        c = FakeClient(trades=[{"asset_id": UP, "size": 5.0, "price": 0.55,
                                "trader_side": "MAKER"}])
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c)
        f = asyncio.run(br.buy_limit(slug=SLUG, side="Up", token_id=UP,
                                     limit_px=0.55, stake=3.0, wait_s=0.2,
                                     poll_s=0.05))
        assert f is not None and f.fee_paid == 0.0
        assert f.filled_px == pytest.approx(0.55)

    def test_remember_rejects_the_OTHER_Round_type(self):
        """Two classes are called `Round`: markets.PmRound (the venue's view)
        and market_feed.Round (the arms' view). `plan()` calls `.token(side)`
        on whatever is cached, so the wrong one failed LATER, on a live round,
        with `'Round' object has no attribute 'token'`. The dry run found it."""
        from btcpredict.feeds.market_feed import Round as ArmRound

        br = make_broker(book=FakeBook({}), client=FakeClient())
        wrong = ArmRound(
            slug=SLUG, market_topic_id=0, market_id=None, condition_id=None,
            symbol="BTCUSDT", start_ms=0, end_ms=0, strike=1.0, end_price=None,
            price_feed_id=None, feed_provider="x", fee_rate_bps=0,
            liquidity=0.0, trade_volume=0.0, status="open",
            trading_status=None, live_status=None, outcomes=[])
        with pytest.raises(TypeError, match="markets.PmRound"):
            br.remember(wrong)

    def test_remember_accepts_the_venue_round(self):
        br = make_broker(book=FakeBook({}), client=FakeClient())
        br.remember(PmRound(
            slug="btc-updown-5m-999", start_ts=999, condition_id="0xc",
            up_token=UP, down_token=DOWN, tick=0.01, min_order=5.0,
            end_ms=999_000, neg_risk=False, liquidity=1.0))
        assert "btc-updown-5m-999" in br._rounds.by_slug

    def test_the_parameter_names_match_live_broker(self):
        # main.py guards the call with `hasattr(broker, "buy_limit")`, so a
        # method with the RIGHT NAME and WRONG parameters passes the guard and
        # then raises TypeError on the first live order.
        import inspect

        sig = inspect.signature(B.PmBroker.buy_limit)
        assert set(sig.parameters) >= {"slug", "side", "token_id", "limit_px",
                                       "stake", "wait_s", "poll_s",
                                       "fill_break"}

    def test_the_venue_fee_wins_when_it_reports_one(self):
        # 70 bps of notional, reported by the venue. The metadata's schedule is
        # a PREDICTION; this is the charge.
        c = FakeClient(trades=[{"asset_id": UP, "size": 5.0, "price": 0.50,
                                "fee_rate_bps": 70}])
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c)
        f = asyncio.run(br.buy(slug=SLUG, side="Up", token_id=UP,
                               price=0.52, stake=2.0))
        assert f.fee_paid == pytest.approx(70 / 1e4 * 0.50 * 5.0)

    def test_an_unattributed_fill_is_charged_the_TAKER_rate(self):
        # Missing attribution must not be read as "maker": that would
        # understate cost on the path that actually pays it.
        c = FakeClient(trades=[{"asset_id": UP, "size": 5.0, "price": 0.50,
                                "trader_side": None}])
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c)
        f = asyncio.run(br.buy(slug=SLUG, side="Up", token_id=UP,
                               price=0.52, stake=2.0))
        assert f.fee_paid == pytest.approx(B.taker_fee(0.50, 5.0))

    def test_a_resting_order_that_never_fills_returns_None(self):
        # Mirrors `limit_unfilled` on the wrapper. Returning an "unverified"
        # Fill here would book a position that does not exist.
        c = FakeClient()
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c)
        assert asyncio.run(br.buy_limit(slug=SLUG, side="Up", token_id=UP,
                                        limit_px=0.55, stake=3.0,
                                        wait_s=0.2, poll_s=0.05)) is None
        assert br.last_failure == "limit_unfilled"

    def test_an_unfilled_limit_is_CANCELLED(self):
        # Otherwise a resting order is left behind on the venue.
        c = FakeClient()
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c)
        asyncio.run(br.buy_limit(slug=SLUG, side="Up", token_id=UP,
                                 limit_px=0.55, stake=3.0, wait_s=0.2,
                                 poll_s=0.05))
        assert "cancel_order" in c.calls

    def test_a_post_only_rejection_is_not_a_fill(self):
        # The venue rejects post-only above the touch: "invalid post-only
        # order: order crosses book".
        class Cross(FakeClient):
            def post_order(self, signed):
                raise RuntimeError("invalid post-only order: order crosses book")

        br = make_broker(book=FakeBook({UP: touch(UP)}), client=Cross())
        assert asyncio.run(br.buy_limit(slug=SLUG, side="Up", token_id=UP,
                                        limit_px=0.55, stake=3.0,
                                        wait_s=0.2, poll_s=0.05)) is None
        assert br.last_failure == "post_rejected"


class TestEnabledFlag:
    def test_an_injected_client_counts_as_enabled(self):
        br = make_broker(book=FakeBook({}), client=FakeClient())
        assert br.enabled is True

    def test_missing_credentials_are_reported_not_guessed(self, monkeypatch):
        for k in ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        br = make_broker(book=FakeBook({}), client=None)
        assert br.enabled is False


class TestTheQuoteShape:
    """`main.py` calls `get_quote` DIRECTLY at two sites (not via getattr), so
    its absence is an AttributeError mid-round, not a graceful degradation."""

    def test_a_quote_carries_the_two_fields_the_callers_read(self):
        # main.py: `q.get("averagePrice")` and `q.get("feeRateBps")`.
        b = FakeBook({UP: touch(UP, bid=0.48, ask=0.52)})
        br = make_broker(book=b, client=FakeClient())
        q = asyncio.run(br.get_quote(UP, 2.0))
        assert q is not None
        assert q["averagePrice"] > 0
        assert q["feeRateBps"] > 0
        assert q["tokenId"] == UP

    def test_the_price_is_the_BOOK_SWEEP_not_the_displayed_touch(self):
        # The sweep is what a taker actually pays, and it needs no
        # complementing: the token passed IS the side being bought.
        b = FakeBook({UP: touch(UP, ask=0.50, ask_size=1.0,
                                levels=((0.50, 1.0), (0.90, 100.0)))})
        br = make_broker(book=b, client=FakeClient())
        q = asyncio.run(br.get_quote(UP, 20.0))
        assert q["averagePrice"] > 0.50

    def test_the_fee_curve_is_the_venues_NOT_a_flat_bps(self):
        """fee/share = 0.07*p*(1-p); against notional that is 0.07*(1-p).

        350 bps at 0.50 and 70 bps at 0.90 -- cheap exactly where the
        favourite lives. The wrapper's flat ~96 bps does not transfer.
        """
        mid = make_broker(book=FakeBook({UP: touch(UP, ask=0.50)}),
                          client=FakeClient())
        # 0.07 * (1 - 0.50) * 1e4
        assert asyncio.run(mid.get_quote(UP, 2.0))["feeRateBps"] \
            == pytest.approx(350.0)
        hi = make_broker(book=FakeBook({UP: touch(UP, ask=0.90)}),
                         client=FakeClient())
        assert asyncio.run(hi.get_quote(UP, 2.0))["feeRateBps"] \
            == pytest.approx(70.0, abs=1.0)

    def test_a_stale_book_yields_no_quote(self):
        b = FakeBook({UP: touch(UP, age_ms=30_000)})
        br = make_broker(book=b, client=FakeClient(), max_book_age_s=2.0)
        assert asyncio.run(br.get_quote(UP, 2.0)) is None

    def test_the_wrapper_only_arguments_are_accepted(self):
        # A caller written for BinanceBroker must run unchanged.
        b = FakeBook({UP: touch(UP)})
        br = make_broker(book=b, client=FakeClient())
        q = asyncio.run(br.get_quote(UP, 2.0, "BUY", order_type="LIMIT",
                                     price_limit=0.60))
        assert q is not None


class TestAuditRegressions:
    """One test per bug found by `tools/pm_sdk_audit.py` against SDK 0.10.0.

    Each of these failed silently in production terms rather than loudly: a
    missing method was swallowed into an empty list, a renamed field returned
    0, and a rejected order looked like an accepted one. A test is the only
    thing that keeps them fixed.
    """

    def test_open_orders_uses_list_open_orders(self):
        # The SDK has NO `list_orders`. Calling it raises AttributeError, which
        # the handler swallows -- so the failure mode is a permanently empty
        # order list, not a crash.
        c = FakeClient(open_orders=[{"id": "o7", "price": 0.44, "size": 100.0,
                                     "filled": 2.0}])
        br = make_broker(book=FakeBook({}), client=c)
        got = asyncio.run(br.active_orders())
        assert c.calls.count("list_open_orders") == 1
        assert got and got[0]["orderId"] == "o7"
        assert got[0]["filled"] == pytest.approx(2.0)

    def test_a_broken_open_order_reader_is_not_a_crash(self):
        class Bad(FakeClient):
            def list_open_orders(self, **kw):
                raise AttributeError("no such method")

        br = make_broker(book=FakeBook({}), client=Bad())
        assert asyncio.run(br.active_orders()) == []
        assert br.last_failure == "open_orders_read_failed"

    def test_a_rejected_order_is_None_not_a_fill(self):
        """`post_order` returns AcceptedOrder | RejectedOrder.

        Treating a rejection as accepted reports a Fill that never existed,
        and then sends the operator hunting for a phantom position.
        """
        c = FakeClient(order_ok=False, reject_code="400",
                       reject_message="invalid post-only order: order "
                                      "crosses book")
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c)
        f = asyncio.run(br.buy(slug=SLUG, side="Up", token_id=UP,
                               price=0.52, stake=2.0))
        assert f is None                      # NOT an unverified Fill
        assert br.last_failure == "rejected"
        assert "crosses book" in (br.last_order_error or "")

    def test_a_rejected_post_only_order_is_also_None(self):
        c = FakeClient(order_ok=False, reject_code="400",
                       reject_message="order crosses book")
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c)
        assert asyncio.run(br.buy_limit(slug=SLUG, side="Up", token_id=UP,
                                        limit_px=0.55, stake=3.0,
                                        wait_s=0.1)) is None
        assert br.last_failure == "rejected"

    def test_positions_read_current_size_not_size(self):
        # The real `Position` has NO `size`. Reading it returns 0 without
        # raising, which made every fill look unfilled.
        c = FakeClient(positions=[{"asset_id": UP, "size": 3.0,
                                   "price": 0.40}])
        br = make_broker(book=FakeBook({}), client=c)
        pos = asyncio.run(br.positions())
        assert pos and pos[0]["size"] == pytest.approx(3.0)

    def test_a_stale_trade_is_not_counted_as_this_fill(self):
        """A confirmation must be a DELTA, not a total.

        A position's size is cumulative: buying the same token twice in a
        round (a retry, a second entry) would otherwise report the running
        total and overstate the second stake.
        """
        old = {"asset_id": UP, "size": 5.0, "price": 0.30,
               "matched_at": (datetime.now(timezone.utc)
                              - timedelta(minutes=10)).isoformat()}
        new = {"asset_id": UP, "size": 5.0, "price": 0.55}
        c = FakeClient(trades=[old, new])
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c)
        f = asyncio.run(br.buy(slug=SLUG, side="Up", token_id=UP,
                               price=0.55, stake=3.0))
        assert f.shares == pytest.approx(5.0)          # not 10
        assert f.filled_px == pytest.approx(0.55)      # not vwap(0.30, 0.55)

    def test_an_unparsable_timestamp_does_not_discard_a_fill(self):
        # A wrong time is a parsing failure, not evidence the fill is not ours.
        c = FakeClient(trades=[{"asset_id": UP, "size": 5.0, "price": 0.50,
                                "matched_at": "not-a-date"}])
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c)
        f = asyncio.run(br.buy(slug=SLUG, side="Up", token_id=UP,
                               price=0.52, stake=2.0))
        assert f is not None and f.unverified is False

    def test_a_trade_for_another_token_is_ignored(self):
        c = FakeClient(trades=[{"asset_id": DOWN, "size": 99.0,
                                "price": 0.01}])
        br = make_broker(book=FakeBook({UP: touch(UP)}), client=c)
        f = asyncio.run(br.buy(slug=SLUG, side="Up", token_id=UP,
                               price=0.52, stake=2.0))
        assert f is not None and f.unverified is True
