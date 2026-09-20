"""`LiveBroker.sell_fak` -- the first sell path on the Binance venue.

`main.py` has called `self.broker.sell_fak` since the cross exit shipped, but
this class never defined it, so `_arm_exit_watch`'s
`hasattr(self.broker, "sell_fak")` was False on Binance and the watcher
returned silently -- no error, no log line. Every exit rule was inert there
for that reason alone, whatever its config said.

A phantom SALE is worse than a phantom buy: it books proceeds that never
arrived AND releases a position the venue still holds, so the round then
settles against a position the ledger thinks is gone. The failure paths are
therefore the point of this file.
"""
import asyncio

import pytest

from btcpredict.execution.live import LiveBroker, SELL_MIN_NOTIONAL_USDT


def _broker(*, quote=None, place=None, record=None):
    b = LiveBroker.__new__(LiveBroker)
    b.api_key = "k"; b.api_secret = "s"
    b.wallet_address = "0xw"; b.wallet_id = "wid"
    b.account_type = "SPOT"; b.slippage_bps = 300
    b.last_failure = None; b.last_order_error = None
    b._transport_error = False; b._last_status = 200
    b.sent = []

    async def get_quote(token_id, amount, side="BUY", **kw):
        b.sent.append(("quote", side, amount))
        return quote

    async def _request(method, path, params=None):
        b.sent.append(("order", params.get("slippageBps")))
        return place

    async def order_record(order_id, tries=3, delay=0.4):
        return record

    b.get_quote = get_quote
    b._request = _request
    b.order_record = order_record
    return b


def _sell(b, **kw):
    kw.setdefault("slug", "btc-updown-5m-1")
    kw.setdefault("side", "Down")
    kw.setdefault("token_id", "tok")
    kw.setdefault("shares", 3.0)
    return asyncio.run(b.sell_fak(**kw))


class TestItRefusesBeforeSendingAnything:
    def test_no_shares(self):
        b = _broker()
        assert _sell(b, shares=0.0) is None
        assert b.last_failure == "sell_no_shares"
        assert b.sent == [], "must not even ask for a quote"

    def test_no_token(self):
        b = _broker()
        assert _sell(b, token_id="") is None
        assert b.last_failure == "sell_no_shares"
        assert b.sent == []

    def test_no_quote(self):
        b = _broker(quote=None)
        assert _sell(b) is None
        assert b.last_failure == "no_sell_quote"
        assert not any(x[0] == "order" for x in b.sent), "no order without a quote"

    def test_below_the_venue_minimum(self):
        """The binding constraint for a $1-$2 account: the venue refuses
        MARKET orders under ~1.5 USDT of notional, so most of this bot's
        positions cannot be sold at all. Reported by name, and NOT sent."""
        b = _broker(quote={"quoteId": "q", "averagePrice": "0.30",
                           "amountOut": str(int(0.9 * 10**18))})
        assert _sell(b, shares=3.0) is None
        assert b.last_failure == "sell_below_venue_minimum"
        assert not any(x[0] == "order" for x in b.sent), (
            "a sale under the minimum must not be sent to the venue"
        )


class TestItSellsSharesNotDollars:
    def test_amount_in_is_shares(self):
        """For a SELL the venue takes `amountIn` in SHARES. Sending USDT asks
        to sell ~price times too few."""
        b = _broker(quote={"quoteId": "q", "averagePrice": "0.70",
                           "amountOut": str(int(2.1 * 10**18))},
                    place={"orderId": "o1"},
                    record={"fillPercentage": 1.0,
                            "filledShareQty": str(int(3 * 10**18)),
                            "filledUsdtAmount": str(int(2.1 * 10**18)),
                            "marketProviderFee": "0"})
        _sell(b, shares=3.0)
        kind, side, amount = b.sent[0]
        assert kind == "quote" and side == "SELL"
        assert amount == pytest.approx(3.0), "amountIn must be SHARES"


class TestTheUnsafePathsNeverBookAFill:
    def test_unreadable_status_is_not_retried(self):
        """Re-sending on weak evidence sells the position twice, and the
        second sale has nothing behind it."""
        b = _broker(quote={"quoteId": "q", "averagePrice": "0.70",
                           "amountOut": str(int(2.1 * 10**18))},
                    place={"orderId": "o1"}, record=None)
        assert _sell(b) is None
        assert b.last_failure == "sell_unconfirmed"

    def test_zero_fill(self):
        b = _broker(quote={"quoteId": "q", "averagePrice": "0.70",
                           "amountOut": str(int(2.1 * 10**18))},
                    place={"orderId": "o1"},
                    record={"fillPercentage": 0.0})
        assert _sell(b) is None
        assert b.last_failure == "fak_unfilled"

    def test_a_venue_refusal(self):
        b = _broker(quote={"quoteId": "q", "averagePrice": "0.70",
                           "amountOut": str(int(2.1 * 10**18))},
                    place=None)
        b._last_status = 400
        assert _sell(b) is None
        assert b.last_failure == "rejected"


class TestASuccessfulSale:
    def _ok(self):
        return _broker(quote={"quoteId": "q", "averagePrice": "0.70",
                              "amountOut": str(int(2.1 * 10**18))},
                       place={"orderId": "o1"},
                       record={"fillPercentage": 1.0,
                               "filledShareQty": str(int(3 * 10**18)),
                               "filledUsdtAmount": str(int(2.1 * 10**18)),
                               "marketProviderFee": str(int(0.02 * 10**18))})

    def test_stake_is_PROCEEDS_not_an_outlay(self):
        """`stake` is cash coming IN. Handing this Fill to settle() would pay
        the position out against the round's winner -- but it no longer
        exists."""
        f = _sell(self._ok())
        assert f is not None
        assert f.stake == pytest.approx(2.1), "stake must be the proceeds"
        assert f.shares == pytest.approx(3.0)
        assert f.filled_px == pytest.approx(0.70)
        assert f.fee_paid == pytest.approx(0.02)

    def test_last_sell_size_is_published(self):
        b = self._ok(); _sell(b)
        assert b.last_sell_size == pytest.approx(3.0)

    def test_a_floor_tightens_the_slippage(self):
        """`min_px` is the worst acceptable price per share; the venue enforces
        it through slippageBps, the mirror of the buy path's ceiling."""
        b = self._ok(); _sell(b, min_px=0.693)     # 1% under a 0.70 quote
        bps = [x[1] for x in b.sent if x[0] == "order"][0]
        assert 80 <= bps <= 120, bps

    def test_no_floor_uses_the_caller_default(self):
        b = self._ok(); _sell(b, slip=0.03)
        bps = [x[1] for x in b.sent if x[0] == "order"][0]
        assert bps == pytest.approx(300, abs=1)


def test_the_signature_matches_the_other_venue():
    """`main.py` calls one method on whichever broker it holds. A drifted
    signature is a runtime TypeError at the first exit, not a test failure."""
    import inspect
    from btcpredict.venues.polymarket.broker import PmBroker
    a = inspect.signature(LiveBroker.sell_fak).parameters
    b = inspect.signature(PmBroker.sell_fak).parameters
    assert set(a) == set(b), (set(a) ^ set(b))
