"""The SHARE QUANTITY: book the position, never a rounding, never more.

CORRECTED 2026-09-23 from the WALLET, after two wrong answers in a row.
Order 26092300001909464075: $1.00 at 0.70 and the wallet moved
$2.0854 -> $2.50172653 across that one trade, so the position that settled and
redeemed was 1.41632653 shares.

    quote `amountOut`       1.4163   <- the position, to the 8th decimal
    quote `feeAmount`       0.0123   <- already netted OUT of `amountOut`
    venue `filledShareQty`  "1.42"   <- that number ROUNDED to 2dp, here UP

So the two earlier readings were both wrong:
  * `amountOut - feeAmount` (the original code) under-credits by the whole fee --
    that is the 3.2442222-vs-3.24 armed ask from 2026-09-22 22:43, refused -9000
    "You have exceeded your available shares", and the reaction exit never fired;
  * `filledShareQty` (the first fix) is a DISPLAY ROUNDING that can sit ABOVE the
    holding (1.42 > 1.41632653), which reproduces the same refused exit.
`amountOut` reproduces the wallet to the eighth decimal, so that is what `buy`
books. The venue's figure is kept as a cross-check line in the log only.

Two properties, so neither can regress quietly:
  * `buy` books the quote's `amountOut`;
  * `sell_fak` FLOORS the ask to the venue's step (hundredths) and, if the venue
    still refuses the size, steps the ask down rather than giving up -- quotes are
    read-only, so that costs time and can never place an order. Selling FEWER
    shares is always accepted, which is what makes both a percentage exit and the
    venue's own UI semantics safe.

A consequence worth keeping: because the fee is already missing from
`amountOut`, `payout - stake` IS the cash PnL -- deducting `fee_paid` again
would double count it.
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

def test_buy_books_the_quotes_amount_out_not_the_venues_display():
    """`amountOut` is the position; never `amountOut - fee`, never the display."""
    b = _broker()
    gross = QUOTE_NET + 0.0433
    _stub_quote(b, avg=0.616481, amount_out=gross, fee=0.0433)
    _stub_rest(b, executed="3.24")
    fill = asyncio.run(b.buy(slug="s", side="Up", token_id="t",
                             price=0.6, stake=2.0))
    assert fill is not None
    assert abs(fill.shares - gross) < 1e-9, fill.shares
    assert fill.shares != 3.24 and fill.shares != QUOTE_NET, fill.shares
    # the price is then the REAL one, not a re-derived float
    assert abs(fill.filled_px - 2.0 / gross) < 1e-9, fill.filled_px


def test_buy_never_books_a_rounding_the_venue_makes_in_our_favour():
    """MEASURED 2026-09-23: the display rounded UP past the real holding.

    order 26092300001909464075: $1.00 at 0.70 -> the wallet moved
    2.0854 -> 2.50172653, so the position was 1.41632653 shares while the
    venue's `filledShareQty` read "1.42". Booking 1.42 put 0.0037 shares of
    phantom size in the ledger, and an exit selling 1.42 (> 1.41632653 held)
    would have been refused -9000. Book the quote's amountOut: 1.4163.
    """
    b = _broker()
    _stub_quote(b, avg=0.706053, amount_out=1.41632653, fee=0.012245)
    _stub_rest(b, executed="1.42")
    fill = asyncio.run(b.buy(slug="s", side="Down", token_id="t",
                             price=0.7, stake=1.0))
    assert fill is not None
    assert abs(fill.shares - 1.41632653) < 1e-9, fill.shares
    assert fill.shares < 1.42, "booked the venue's display: asks to sell more " \
                               "than the venue holds"
    assert abs(fill.filled_px - 1.0 / 1.41632653) < 1e-9, fill.filled_px
    # and a SELL of it floors BELOW the holding, so the venue accepts
    b2 = _broker()
    _stub_quote(b2)
    _stub_rest(b2)
    asyncio.run(b2.sell_fak(slug="s", side="Down", token_id="t",
                            shares=fill.shares))
    assert b2.rec["quotes"][0]["amount"] == 1.41, b2.rec["quotes"]


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
    # amountOut, and NOT amountOut - feeAmount (which under-credits the fee)
    assert abs(fill.shares - (QUOTE_NET + 0.0433)) < 1e-9, fill.shares
    assert b.last_failure is None
