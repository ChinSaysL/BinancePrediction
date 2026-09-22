"""The SHARE QUANTITY: book the venue's, ask for the venue's, never more.

MEASURED live 2026-09-22 22:43, and it cost a trade. `buy()` booked the QUOTE's
net quantity (3.2442222 = `amountOut - feeAmount`) while the venue's own order
record reports what it credited as `filledShareQty`, a 2-DECIMAL number -- "3.24"
for that fill. The reaction exit then asked the venue to sell 3.2442222 of a
3.24 holding:

    HTTP 400 {'code': -9000, 'msg': 'You have exceeded your available shares.'}
    no SELL quote for btc-updown-5m-1790116800 Up (3.2442 shares)
    sell did not fill (no_sell_quote) -- retrying next tick

No sale, and the position rode to the bell. The 20:33 exit that DID work was luck,
not correctness: it booked 9.6000000 against a credit of 9.6200000, i.e. BELOW.

Two properties, so neither can regress quietly:
  * `buy` prefers the venue's `filledShareQty` over the quote's arithmetic;
  * `sell_fak` FLOORS the ask to the venue's step (hundredths) and, if the venue
    still refuses the size, steps the ask down rather than giving up -- quotes are
    read-only, so that costs time and can never place an order. Selling FEWER
    shares is always accepted, which is what makes both a percentage exit and the
    venue's own UI semantics safe.
"""
from __future__ import annotations

import asyncio

from btcpredict.execution.live import LiveBroker
from btcpredict.execution.live import PLACE_ORDER

# The refused fill, verbatim from the log/db.
QUOTE_NET = 3.2442222222222226      # what the bot used to book
VENUE_EXECUTED = "3.24"             # what the venue credited
ASK = 3.2442222222222226

WEI = 10 ** 18


def _broker(**over):
    b = LiveBroker.__new__(LiveBroker)
    b.api_key, b.api_secret = "k", "s"
    b.wallet_address, b.wallet_id = "0xw", 7
    b.account_type, b.slippage_bps = "SPOT", 300
    b._transport_error, b._last_status = False, 200
    b.last_failure = None
    b.last_order_error = None
    b.last_sell_size = 0.0
    b.rec = {"quotes": [], "orders": []}
    for k, v in over.items():
        setattr(b, k, v)
    return b


def _stub_quote(b, *, avg=0.6, quote_ok_after=0, amount_out=None, fee=0.0):
    """`get_quote`: records every ask, refuses the first `quote_ok_after` ones."""
    state = {"n": 0}

    async def get_quote(token_id, amount, side="BUY", *, order_type="MARKET",
                        price_limit=None):
        b.rec["quotes"].append({"amount": amount, "side": side,
                                "order_type": order_type})
        if state["n"] < quote_ok_after:
            state["n"] += 1
            b._last_status = 400
            return None
        return {"quoteId": "q1", "averagePrice": avg,
                "amountOut": str(int((amount_out or amount) * WEI)),
                "feeAmount": str(int(fee * WEI)), "expireAt": 2 ** 62}
    b.get_quote = get_quote
    return state


def _stub_rest(b, *, executed=VENUE_EXECUTED, fill_pct="1"):
    async def _request(method, path, params):
        b.rec["orders"].append({"path": path, "params": params})
        return {"orderId": "o1"}

    async def order_record(order_id, tries=3, delay=0.4):
        return {"fillPercentage": fill_pct, "filledShareQty": executed,
                "filledUsdtAmount": "2", "marketProviderFee": "0.01",
                "status": "FILLED"}

    async def _order_status(order_id, tries=8, delay=1.5):
        return "FILLED"

    b._request = _request
    b.order_record = order_record
    b._order_status = _order_status


# --- the SELL side ---------------------------------------------------------

def test_sell_fak_floors_the_ask_to_the_venues_step():
    """3.2442222 must go out as 3.24: asking for a hair more kills the exit."""
    b = _broker()
    _stub_quote(b)
    _stub_rest(b)
    fill = asyncio.run(b.sell_fak(slug="s", side="Up", token_id="t",
                                  shares=ASK))
    assert fill is not None
    assert b.rec["quotes"][0]["amount"] == 3.24, b.rec["quotes"]
    # and the sale itself went out (not a silent no-op)
    assert any(o["path"] == PLACE_ORDER for o in b.rec["orders"])


def test_sell_fak_steps_the_ask_down_when_the_venue_refuses_it():
    """A refused quote must not end the exit: retry SMALLER, then sell."""
    b = _broker()
    _stub_quote(b, quote_ok_after=1)     # refuse the first ask (a -9000 case)
    _stub_rest(b)
    fill = asyncio.run(b.sell_fak(slug="s", side="Up", token_id="t",
                                  shares=ASK))
    assert fill is not None, "the exit gave up instead of selling less"
    asks = [q["amount"] for q in b.rec["quotes"]]
    assert asks[0] == 3.24 and asks[1] < 3.24, asks
    assert any(o["path"] == PLACE_ORDER for o in b.rec["orders"])


def test_sell_fak_never_rounds_up():
    for raw in (0.6749, 1.6299999, 3.241, 9.607):
        b = _broker()
        _stub_quote(b)
        _stub_rest(b)
        asyncio.run(b.sell_fak(slug="s", side="Up", token_id="t", shares=raw))
        asked = b.rec["quotes"][0]["amount"]
        assert asked <= raw, (raw, asked)
        assert abs(asked * 100 - round(asked * 100)) < 1e-9, asked


# --- the BUY side ---------------------------------------------------------

def test_buy_books_the_venues_executed_quantity_not_the_quotes():
    """The root cause: 3.2442222 booked vs 3.24 credited -> sell refused."""
    b = _broker()
    _stub_quote(b, avg=0.616481, amount_out=QUOTE_NET + 0.0433, fee=0.0433)
    _stub_rest(b, executed="3.24")
    fill = asyncio.run(b.buy(slug="s", side="Up", token_id="t",
                             price=0.6, stake=2.0))
    assert fill is not None
    assert fill.shares == 3.24, fill.shares
    # the price is then the REAL one, not a re-derived float
    assert abs(fill.filled_px - 2.0 / 3.24) < 1e-9, fill.filled_px


def test_buy_falls_back_to_the_quote_when_the_record_is_unreadable():
    b = _broker()
    _stub_quote(b, avg=0.616481, amount_out=QUOTE_NET + 0.0433, fee=0.0433)

    async def _request(method, path, params):
        return {"orderId": "o1"}

    async def order_record(order_id, tries=3, delay=0.4):
        return None

    async def _order_status(order_id, tries=8, delay=1.5):
        return "FILLED"

    b._request, b.order_record, b._order_status = _request, order_record, _order_status
    fill = asyncio.run(b.buy(slug="s", side="Up", token_id="t",
                             price=0.6, stake=2.0))
    assert fill is not None
    assert fill.shares == QUOTE_NET, fill.shares   # the old behaviour, loudly logged
    assert b.last_failure is None
