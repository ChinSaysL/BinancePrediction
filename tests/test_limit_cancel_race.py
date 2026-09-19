"""A limit order that fills WHILE we cancel it must still be booked.

`buy_limit` polls the order, sees 0% filled, cancels, and returns None. The
venue can fill the order in the gap between that last poll and the cancel
landing. Returning None there books no fill for a position the venue has
actually opened: an untracked holding that never settles, never redeems and
never appears in the ledger or the daily P&L.

Latent when found on 2026-09-19 -- `CANCEL NOT CONFIRMED` had never fired in
10 unfilled cancels -- but the old code could not have detected the case it
mattered most in, because a venue that fills first and then accepts the cancel
of a zero remainder looks exactly like a clean cancel.
"""
import asyncio

import pytest

from btcpredict.execution.live import LiveBroker


def _broker(records, cancel_result=None):
    """A LiveBroker with the network replaced.

    `records` is the sequence order_record() returns, so a test can make the
    poll say 0% and the post-cancel read say something else.
    """
    b = LiveBroker.__new__(LiveBroker)
    # `enabled` is a read-only property over these four
    b.api_key = "k"
    b.api_secret = "s"
    b.wallet_address = "0xwallet"
    b.wallet_id = "wid"
    b.account_type = "SPOT"
    b.slippage_bps = 50
    b.last_failure = None
    b.last_order_error = None
    b._transport_error = False
    b._last_status = 200
    b.cancelled = []
    seq = list(records)

    async def get_quote(*a, **k):
        return {"quoteId": "q1"}

    async def _request(method, path, params=None):
        return {"orderId": "oid-1"}

    async def order_record(order_id, tries=3, delay=0.4):
        return seq.pop(0) if seq else (records[-1] if records else None)

    async def cancel_orders(ids):
        b.cancelled.append(list(ids))
        return cancel_result if cancel_result is not None else list(ids)

    b.get_quote = get_quote
    b._request = _request
    b.order_record = order_record
    b.cancel_orders = cancel_orders
    return b


def _call(b):
    return asyncio.run(b.buy_limit(
        slug="eth-updown-15m-1", side="Down", token_id="tok",
        limit_px=0.60, stake=2.0, wait_s=0.0, poll_s=0.0))


class TestTheCancelRace:
    def test_a_genuinely_unfilled_order_still_returns_none(self):
        """The ordinary path must not change: no fill means the round is over."""
        b = _broker([{"fillPercentage": 0.0, "status": "OPENING"},
                     {"fillPercentage": 0.0, "status": "CANCELLED"}])
        assert _call(b) is None
        assert b.last_failure == "limit_unfilled"
        assert b.cancelled == [["oid-1"]]

    def test_a_fill_that_lands_during_the_cancel_is_booked(self):
        """The bug: the venue filled it, so a position exists. Book it."""
        b = _broker([
            {"fillPercentage": 0.0, "status": "OPENING"},          # the poll
            {"fillPercentage": 0.99, "status": "FILLED",           # after cancel
             "filledUsdtAmount": "1.97", "filledShareQty": "3.28",
             "marketProviderFee": "0.0307"},
        ])
        fill = _call(b)
        assert fill is not None, "a filled order must never return None"
        assert fill.shares == pytest.approx(3.28)
        assert fill.stake == pytest.approx(1.97)
        assert fill.fee_paid == pytest.approx(0.0307)
        # price comes from the venue's record, never from what we asked
        assert fill.filled_px == pytest.approx(1.97 / 3.28, rel=1e-6)
        assert fill.order_id == "oid-1"

    def test_it_books_the_fill_even_when_the_cancel_reports_success(self):
        """A venue that fills first, then accepts the cancel of a zero
        remainder, is indistinguishable from a clean cancel by the cancel's
        own return value. Only the order record is authoritative."""
        b = _broker([
            {"fillPercentage": 0.0, "status": "OPENING"},
            {"fillPercentage": 1.0, "status": "FILLED",
             "filledUsdtAmount": "2.00", "filledShareQty": "3.33",
             "marketProviderFee": "0.0"},
        ], cancel_result=["oid-1"])          # cancel says it worked
        fill = _call(b)
        assert fill is not None, (
            "trusting the cancel's return value is what loses the position"
        )
        assert fill.shares == pytest.approx(3.33)
