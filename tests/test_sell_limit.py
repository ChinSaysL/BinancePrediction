"""`sell_limit`: the sell path that is NOT bound by the venue's MARKET minimum.

WHY THIS FILE EXISTS. `sell_fak` is a MARKET order and the venue's MARKET
minimum is ~1.5 USDT of notional, so at $1-3 stakes a position could not be sold
at all -- measured 2026-09-22 13:42:01, the reaction exit was refused on a
2.6831-share position whose 25% slice came to $0.3556, and `react` exits booked
stood at 0 all-time. LIMIT carries NO such minimum (the venue's schema and
config.py's `order_type` note both say so), which makes this method the thing
that lets any position be sold.

TWO MISTAKES THAT WOULD BE SILENT RATHER THAN LOUD, so they are pinned here:

  * THE UNITS. For a SELL, `amountIn` is SHARES IN WEI, not USDT. Sending USDT
    asks the venue to sell ~`price` times too few shares and it ACCEPTS it -- a
    quiet under-sell, not an error. Pinned by asserting the amount handed to
    `get_quote` is the SHARE COUNT and not the USDT proceeds.
  * THE PROCEEDS. On a sell, `stake` is cash coming IN. Handing it to
    `settle()` would pay the position out against the round's winner as well,
    crediting it twice.
"""
from __future__ import annotations

import asyncio

import pytest

from btcpredict.execution.live import LiveBroker

SHARES = 0.6708          # 25% of a 2.6831-share position
PROCEEDS = 0.3556        # the slice the venue's MARKET minimum refused


class _Rec:
    def __init__(self):
        self.quotes: list[dict] = []
        self.orders: list[dict] = []
        self.cancels: list[list[str]] = []


def make_broker(*, quote_avg=0.53, fill_pct=1.0, post_pct=None,
                status="FILLED", sold=SHARES, proceeds=PROCEEDS, fee=0.001,
                place_ok=True, unreadable=False):
    """A `LiveBroker` with only the surface `sell_limit` touches."""
    b = LiveBroker.__new__(LiveBroker)
    b.api_key, b.api_secret = "k", "s"
    b.wallet_address, b.wallet_id = "0xw", 7
    b.account_type, b.slippage_bps = "SPOT", 300
    b._transport_error, b._last_status = False, 200
    b.last_failure = None
    b.last_order_error = None
    b.last_sell_size = 0.0
    rec = _Rec()
    b.rec = rec
    reads = {"n": 0}

    async def get_quote(token_id, amount, side="BUY", *, order_type="MARKET",
                        price_limit=None):
        rec.quotes.append(dict(token_id=token_id, amount=amount, side=side,
                               order_type=order_type, price_limit=price_limit))
        return {"quoteId": "q1", "averagePrice": quote_avg}

    async def _request(method, path, params):
        rec.orders.append(dict(method=method, path=path, params=params))
        if not place_ok:
            b._last_status = 400
            return None
        return {"orderId": "oid1"}

    async def order_record(order_id, tries=1, delay=0.8):
        reads["n"] += 1
        if unreadable:
            return None
        # read 1 is the wait loop; read 2 is the post-cancel re-read
        pct = post_pct if (post_pct is not None and reads["n"] > 1) else fill_pct
        return {"fillPercentage": pct, "status": status,
                "filledShareQty": sold, "filledUsdtAmount": proceeds,
                "marketProviderFee": fee}

    async def cancel_orders(ids):
        rec.cancels.append(list(ids))
        return list(ids)

    async def order_book(*a, **k):
        return None

    b.get_quote, b._request = get_quote, _request
    b.order_record, b.cancel_orders = order_record, cancel_orders
    b.order_book = order_book
    return b


def sell(b, **kw):
    kw.setdefault("slug", "btc-updown-5m-1790084400")
    kw.setdefault("side", "Down")
    kw.setdefault("token_id", "tok")
    kw.setdefault("wait_s", 0.0)
    return asyncio.run(b.sell_limit(**kw))


# -- the units trap ---------------------------------------------------------

def test_the_amount_sent_for_a_sell_is_SHARES_not_usdt():
    """Sending USDT would ask to sell ~price x too few shares, and the venue
    would accept it -- a quiet under-sell with no error anywhere.

    The ask is FLOORED to the venue's own step on the way out (hundredths --
    an order record reports the executed quantity as "3.24", and a real
    holding is therefore always a multiple of 0.01). So the `SHARES` constant
    below is not sent verbatim any more: the invariant pinned here is the
    UNITS, and that the ask is never LARGER than the holding."""
    b = make_broker()
    sell(b, shares=SHARES, limit_px=0.53)
    q = b.rec.quotes[0]
    assert q["side"] == "SELL"
    assert q["amount"] <= SHARES, "amountIn must never exceed the holding"
    assert q["amount"] == pytest.approx(SHARES, abs=0.01), "amountIn must be the SHARES"
    assert abs(q["amount"] - PROCEEDS) > 0.1, "it must NOT be the USDT proceeds"
    assert q["order_type"] == "LIMIT"
    assert q["price_limit"] == pytest.approx(0.53)


# -- the order body ---------------------------------------------------------

def test_it_posts_a_GTC_limit_priced_to_the_venues_2dp_tick():
    b = make_broker()
    sell(b, shares=1.0, limit_px=0.53)
    p = b.rec.orders[0]["params"]
    assert p["orderType"] == "LIMIT"
    assert p["timeInForce"] == "GTC", "FOK belongs to MARKET"
    assert p["priceLimit"] == "0.53"
    assert p["quoteId"] == "q1"


def test_it_prices_off_the_venue_quote_when_the_caller_gives_none():
    """`_cross_exit_execute` is explicit that the ARM must not price an exit, so
    the price comes from the venue's own SELL quote, floored to its tick."""
    b = make_broker(quote_avg=0.537)
    sell(b, shares=1.0, limit_px=None)
    assert [q["order_type"] for q in b.rec.quotes] == ["MARKET", "LIMIT"]
    assert b.rec.quotes[0]["side"] == "SELL"
    assert b.rec.quotes[0]["amount"] == pytest.approx(1.0)
    assert b.rec.quotes[1]["price_limit"] == pytest.approx(0.53)
    assert b.rec.orders[0]["params"]["priceLimit"] == "0.53"


def test_an_unusable_venue_quote_is_refused_before_any_order():
    b = make_broker(quote_avg=0.0)
    assert sell(b, shares=1.0, limit_px=None) is None
    assert b.last_failure == "sell_price_missing"
    assert b.rec.orders == [], "nothing may be sent"


# -- the whole point: it is placeable under the MARKET minimum --------------

def test_a_position_worth_35_cents_is_placeable_here():
    """The $1.50 floor is a MARKET rule. This slice is the exact one the live
    log refused on 2026-09-22 13:42:01."""
    b = make_broker(proceeds=PROCEEDS, sold=SHARES)
    f = sell(b, shares=SHARES, limit_px=0.53)
    assert f is not None, "a MARKET order would have refused this"
    assert b.last_failure is None
    assert len(b.rec.orders) == 1


# -- the fill, and what it books --------------------------------------------

def test_the_booked_stake_is_PROCEEDS_not_an_outlay():
    b = make_broker(sold=SHARES, proceeds=PROCEEDS, fee=0.001)
    f = sell(b, shares=SHARES, limit_px=0.53)
    assert f is not None
    assert f.stake == pytest.approx(PROCEEDS), "stake is cash IN on a sell"
    assert f.shares == pytest.approx(SHARES)
    assert f.filled_px == pytest.approx(PROCEEDS / SHARES, rel=1e-6)
    assert f.requested_px == pytest.approx(0.53)
    assert f.token_id == "tok"
    assert f.fee_paid == pytest.approx(0.001)
    assert b.last_sell_size == pytest.approx(SHARES)


def test_a_miss_returns_None_cancels_and_keeps_the_position():
    """None means a MISSED EXIT, not a sale -- the caller still owns it."""
    b = make_broker(fill_pct=0.0, status="CANCELLED")
    assert sell(b, shares=SHARES, limit_px=0.53) is None
    assert b.last_failure == "limit_unfilled"
    assert b.rec.cancels == [["oid1"]]


def test_a_fill_that_raced_the_cancel_is_actually_booked():
    """⛔ The venue can fill between the last poll and the cancel landing.
    Returning None there would tell the caller it still owns a position the
    venue has already paid for -- and the round would then settle against
    shares that no longer exist."""
    b = make_broker(fill_pct=0.0, post_pct=1.0, status="CANCELLED")
    f = sell(b, shares=SHARES, limit_px=0.53)
    assert f is not None, "the post-cancel re-read must win"
    assert f.shares == pytest.approx(SHARES)


def test_a_refused_placement_is_reported_as_rejected():
    b = make_broker(place_ok=False)
    assert sell(b, shares=SHARES, limit_px=0.53) is None
    assert b.last_failure == "rejected"


def test_an_unreadable_status_is_NOT_a_confirmed_miss():
    """⛔ `limit_unfilled` asserts the order did not fill. When the venue never
    answered we do not know that -- and the caller's miss branch RETRIES next
    tick, so a retry after a real fill is a naked short. `sell_fak` answers this
    with `sell_unconfirmed` and this must too."""
    b = make_broker(unreadable=True)
    assert sell(b, shares=SHARES, limit_px=0.53) is None
    assert b.last_failure == "sell_unconfirmed"
    assert b.last_failure != "limit_unfilled"
    assert b.rec.cancels == [["oid1"]], "an untracked GTC order must be pulled"


def test_a_remainder_that_fills_with_the_cancel_is_booked():
    """The same race as the zero-fill path, reached from the PARTIAL path: the
    poll says 50%, the cancel lands, and the remainder filled in the gap.
    Booking the polled 50% under-states the proceeds and tells the caller it
    still holds shares the venue has already paid for."""
    b = make_broker(fill_pct=0.5, post_pct=1.0, status="OPEN")
    f = sell(b, shares=SHARES, limit_px=0.53)
    assert f is not None
    assert b.rec.cancels == [["oid1"]], "the remainder is still cancelled"
    assert f.shares == pytest.approx(SHARES), "the venue's number, not 50% of it"
    assert f.stake == pytest.approx(PROCEEDS)
    assert b.last_sell_size == pytest.approx(SHARES)


def test_last_sell_size_stays_zero_when_nothing_sold():
    """It means "what we actually SOLD" (`main.py` reads it to size a pending
    sale's confirmation), so a refusal must not leave a size behind."""
    for b, kw in ((make_broker(place_ok=False), {}),
                  (make_broker(unreadable=True), {}),
                  (make_broker(fill_pct=0.0, status="CANCELLED"), {})):
        sell(b, shares=SHARES, limit_px=0.53, **kw)
        assert b.last_sell_size == 0.0


def test_no_shares_is_refused_before_any_venue_call():
    b = make_broker()
    assert sell(b, shares=0.0, limit_px=0.53) is None
    assert b.last_failure == "sell_no_shares"
    assert b.rec.quotes == [] and b.rec.orders == []


def test_a_position_with_no_token_is_refused_before_any_venue_call():
    b = make_broker()
    assert asyncio.run(b.sell_limit(slug="s", side="Up", token_id="",
                                    shares=SHARES, limit_px=0.53, wait_s=0.0)) is None
    assert b.last_failure == "sell_no_shares"
    assert b.rec.quotes == [] and b.rec.orders == []
