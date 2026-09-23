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

    def test_a_sale_under_one_dollar_is_SENT(self):
        """Operator, 2026-09-23 23:10: there is NO sell minimum. The $1.00
        guard refused the 22:04 exit (5.71 sh @ 0.14 = $0.78) before it ever
        reached the venue. A small sale goes out and the venue decides."""
        b = _broker(quote={"quoteId": "q", "averagePrice": "0.14",
                           "amountOut": str(int(0.78 * 10**18))})
        _sell(b, shares=5.71)
        assert b.last_failure != "sell_below_venue_minimum"
        assert any(x[0] == "order" for x in b.sent), (
            "a sub-$1 sale must be sent to the venue")
        assert SELL_MIN_NOTIONAL_USDT == 0.0


class TestTheSizeLadderGoesAllTheWayDown:
    """21:09 died with 6.06 / 5.99 / 5.75 refused and nothing smaller asked."""

    def test_it_keeps_asking_smaller_until_a_quote_comes_back(self, monkeypatch):
        import btcpredict.execution.live as live
        monkeypatch.setattr(live, "SELL_STEP_PAUSE_S", 0.0)
        b = _broker()
        asks = []

        async def get_quote(token_id, amount, side="BUY", **kw):
            asks.append(amount)
            if amount > 1.0:
                return None                 # "lacks enough liquidity"
            return {"quoteId": "q", "averagePrice": "0.50",
                    "amountOut": str(int(amount * 0.5 * 10**18))}
        b.get_quote = get_quote
        q, ask = asyncio.run(b._sell_quote("s", "Down", "tok", 6.06))
        assert q is not None and ask <= 1.0, (ask, asks)
        assert asks == sorted(asks, reverse=True) and len(set(asks)) == len(asks)
        assert asks[0] == 6.06

    def test_the_last_rung_is_one_hundredth_of_a_share(self, monkeypatch):
        import btcpredict.execution.live as live
        monkeypatch.setattr(live, "SELL_STEP_PAUSE_S", 0.0)
        b = _broker()
        asks = []

        async def get_quote(token_id, amount, side="BUY", **kw):
            asks.append(amount)
            return None
        b.get_quote = get_quote
        q, _ = asyncio.run(b._sell_quote("s", "Down", "tok", 5.93))
        assert q is None
        assert asks[-1] == 0.01, asks
        assert all(a <= 5.93 for a in asks)


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
    def test_unreadable_status_is_PENDING_not_a_no_fill(self):
        """The model exit retries a failed sale every tick, so an unreadable
        status must NOT read as a no-fill (that would send a second sale while
        the first may have filled). PENDING books no shares and hands the
        position to `confirm_sale`."""
        b = _broker(quote={"quoteId": "q", "averagePrice": "0.70",
                           "amountOut": str(int(2.1 * 10**18))},
                    place={"orderId": "o1"}, record=None)
        f = _sell(b)
        assert f is not None and f.pending is True and f.shares == 0.0
        assert b.last_failure == "sell_pending"

    def test_a_transport_failure_on_place_is_PENDING(self):
        """The order may be live; a None would let the per-tick retry send a
        second sale over an unbooked first one."""
        b = _broker(quote={"quoteId": "q", "averagePrice": "0.70",
                           "amountOut": str(int(2.1 * 10**18))}, place=None)
        b._transport_error = True
        f = _sell(b)
        assert f is not None and f.pending is True and f.shares == 0.0
        assert b.last_failure == "sell_pending" and b.last_sell_size == 3.0

    def test_a_4xx_on_place_is_a_plain_refusal(self):
        b = _broker(quote={"quoteId": "q", "averagePrice": "0.70",
                           "amountOut": str(int(2.1 * 10**18))}, place=None)
        b._last_status = 400
        assert _sell(b) is None and b.last_failure == "rejected"

    def test_zero_fill(self):
        """A status that MEANS dead is the only thing allowed to be a no-fill."""
        b = _broker(quote={"quoteId": "q", "averagePrice": "0.70",
                           "amountOut": str(int(2.1 * 10**18))},
                    place={"orderId": "o1"},
                    record={"status": "FAILED", "fillPercentage": 0.0})
        assert _sell(b) is None
        assert b.last_failure == "fak_unfilled"

    def test_a_venue_refusal(self):
        b = _broker(quote={"quoteId": "q", "averagePrice": "0.70",
                           "amountOut": str(int(2.1 * 10**18))},
                    place=None)
        b._last_status = 400
        assert _sell(b) is None
        assert b.last_failure == "rejected"


def _seq(records, **kw):
    """A broker whose `order_record` walks `records` (last one repeats)."""
    b = _broker(**kw)
    seen = {"n": 0}

    async def order_record(order_id, tries=3, delay=0.4):
        rec = records[min(seen["n"], len(records) - 1)]
        seen["n"] += 1
        return rec

    b.order_record = order_record
    b.record_calls = seen
    return b


class TestAFillWeCannotSeeYetIsNotANoFill:
    """⛔ THE BUG THIS FILE EXISTS FOR (live, 2026-09-23).

    `order_record` retries only until the order APPEARS, so the record read
    right after the ack is a still-OPENING order with `fillPercentage 0.0`.
    The old code called that `fak_unfilled`: the position stayed on the ledger
    and `settle_due` then booked the ROUND's own result in its place.

    The venue's own `/order/history` for this wallet: **5 SELLs ever, 4 FILLED
    / 1 FAILED**, and the bot booked NONE of the fills --
        13:52  sold 5.09 @0.52  = $2.60  (venue realizedPnl -$0.90) | ledger -$3.50
        13:36  sold 4.95 @0.546 = $2.66  (venue -$0.84)             | ledger +$1.46
        11:29  sold 5.40 @0.93  = $5.01  (venue +$1.52)             | ledger +$1.91
    Fills land 1.2-2.2 s after `createTime`; our no-fill verdicts were printed
    141 ms - 1.4 s BEFORE the venue's own terminal times.
    """

    _FILLED = {"status": "FILLED", "fillPercentage": 1.0,
               "filledShareQty": "5.09", "filledUsdtAmount": "2.60",
               "marketProviderFee": "0.048864"}

    def _fast(self, monkeypatch):
        from btcpredict.execution import live
        monkeypatch.setattr(live, "SELL_CONFIRM_WAIT_S", 0.2)
        monkeypatch.setattr(live, "SELL_CONFIRM_POLL_S", 0.01)

    def test_opening_then_filled_books_the_venue_numbers(self, monkeypatch):
        """🔴 THE ONE. 141 ms after this state was read, the venue had sold."""
        self._fast(monkeypatch)
        b = _seq([{"status": "OPENING", "fillPercentage": 0.0},
                  self._FILLED],
                 quote={"quoteId": "q", "averagePrice": "0.52",
                        "amountOut": str(int(2.6 * 10**18))},
                 place={"orderId": "o1"})
        f = _sell(b, shares=5.09)
        assert f is not None, "a FILLED sale must not be reported as a no-fill"
        assert not f.pending
        assert f.shares == pytest.approx(5.09)
        # The venue's own live record for 13:52: filledShareQty 5.09,
        # filledUsdtAmount $2.60 (what landed), marketProviderFee 0.048864.
        assert f.stake == pytest.approx(2.648864), "GROSS: net + fee"
        assert f.shares * f.filled_px - f.fee_paid == pytest.approx(2.60), (
            "the booked proceeds must be the $2.60 that landed"
        )
        assert b.last_failure != "fak_unfilled"
        assert b.last_sell_size == pytest.approx(5.09)

    def test_still_working_at_the_deadline_is_PENDING_not_a_no_fill(self,
                                                                   monkeypatch):
        """The venue has not published the outcome. Say so, and keep the fill
        provisional so the caller confirms it late instead of retiring the
        position into the settle path."""
        self._fast(monkeypatch)
        b = _seq([{"status": "OPENING", "fillPercentage": 0.0}],
                 quote={"quoteId": "q", "averagePrice": "0.70",
                        "amountOut": str(int(2.1 * 10**18))},
                 place={"orderId": "o1"})
        f = _sell(b, shares=3.0)
        assert f is not None, "still-working is not a verdict"
        assert f.pending, "the caller must keep the position watched"
        assert f.stake == 0.0 and f.shares == 0.0, "nothing booked yet"
        assert b.last_failure == "sell_pending"

    def test_a_filled_status_with_an_unreadable_qty_is_pending(self, monkeypatch):
        """`status FILLED` while the amounts are unreadable must NOT be called
        a no-fill: the venue just told us something sold."""
        self._fast(monkeypatch)
        b = _seq([{"status": "FILLED", "fillPercentage": 0.0}],
                 quote={"quoteId": "q", "averagePrice": "0.70",
                        "amountOut": str(int(2.1 * 10**18))},
                 place={"orderId": "o1"})
        f = _sell(b, shares=3.0)
        assert f is not None and f.pending
        assert b.last_failure != "fak_unfilled"

    def test_a_record_with_no_status_at_all_is_not_a_no_fill(self, monkeypatch):
        self._fast(monkeypatch)
        b = _seq([{"fillPercentage": 0.0}],
                 quote={"quoteId": "q", "averagePrice": "0.70",
                        "amountOut": str(int(2.1 * 10**18))},
                 place={"orderId": "o1"})
        f = _sell(b, shares=3.0)
        assert f is not None and f.pending

    def test_only_a_DEAD_status_may_retire_a_position(self, monkeypatch):
        """FAILED is the venue saying it matched nothing -- the one case that
        may end the exit `None`, and the one the venue's history also shows."""
        self._fast(monkeypatch)
        b = _seq([{"status": "OPENING", "fillPercentage": 0.0},
                  {"status": "FAILED", "fillPercentage": 0.0}],
                 quote={"quoteId": "q", "averagePrice": "0.70",
                        "amountOut": str(int(2.1 * 10**18))},
                 place={"orderId": "o1"})
        assert _sell(b, shares=3.0) is None
        assert b.last_failure == "fak_unfilled"

    def test_a_sale_with_no_readable_proceeds_is_still_priced(self, monkeypatch):
        """Position gone, cash unreadable: booking $0 leaves a hole nothing
        revisits, so price it at the fill."""
        self._fast(monkeypatch)
        b = _seq([{"status": "FILLED", "fillPercentage": 1.0,
                   "filledShareQty": "3.0"}],
                 quote={"quoteId": "q", "averagePrice": "0.70",
                        "amountOut": str(int(2.1 * 10**18))},
                 place={"orderId": "o1"})
        f = _sell(b, shares=3.0)
        assert f is not None and f.stake == pytest.approx(2.1)

    def test_the_tick_loop_is_not_stalled(self, monkeypatch):
        """The wait is bounded: the fill is late, not the process."""
        self._fast(monkeypatch)
        b = _seq([{"status": "OPENING", "fillPercentage": 0.0}],
                 quote={"quoteId": "q", "averagePrice": "0.70",
                        "amountOut": str(int(2.1 * 10**18))},
                 place={"orderId": "o1"})
        import time as _t
        t0 = _t.monotonic()
        _sell(b, shares=3.0)
        assert _t.monotonic() - t0 < 2.0
        assert b.record_calls["n"] >= 2, "it must have polled, not read once"


class TestConfirmSaleClosesAPendingSale:
    """`_confirm_pending_sale` does `fn is None -> stop asking`, so a broker
    WITHOUT `confirm_sale` retires the watch with the position still on the
    ledger. On Binance there was no such method at all."""

    def _hist(self, orders):
        return _broker(quote={"quoteId": "q", "averagePrice": "0.70",
                              "amountOut": str(int(2.1 * 10**18))},
                       place={"orders": orders})

    def _call(self, b, **kw):
        kw.setdefault("token_id", "tok")
        kw.setdefault("shares", 5.09)
        kw.setdefault("since_ts", 1_790_172_000.0)
        kw.setdefault("fallback_px", 0.50)
        return asyncio.run(b.confirm_sale(**kw))

    def test_it_finds_the_sale_we_placed(self):
        b = self._hist([{"side": "SELL", "status": "FILLED",
                         "createTime": 1_790_172_031_627,
                         "terminalTime": 1_790_172_032_915,
                         "filledShareQty": "5.09",
                         "filledUsdtAmount": "2.60",
                         "marketProviderFee": "0.048864"}])
        got = self._call(b)
        assert got is not None
        sold, px, fee = got
        assert sold == pytest.approx(5.09)
        assert px == pytest.approx(2.60 / 5.09, abs=1e-6)
        assert fee == pytest.approx(0.048864)

    def test_a_no_fill_is_not_a_sale(self):
        b = self._hist([{"side": "SELL", "status": "FAILED",
                         "createTime": 1_790_172_031_627,
                         "filledShareQty": "0", "filledUsdtAmount": "0"}])
        assert self._call(b) is None

    def test_a_buy_is_not_a_sale(self):
        b = self._hist([{"side": "BUY", "status": "FILLED",
                         "createTime": 1_790_172_031_627,
                         "filledShareQty": "5.09",
                         "filledUsdtAmount": "2.60"}])
        assert self._call(b) is None

    def test_an_older_sale_is_not_ours(self):
        """The history also carries the OPERATOR'S own trades."""
        b = self._hist([{"side": "SELL", "status": "FILLED",
                         "createTime": 1_790_100_000_000,
                         "filledShareQty": "5.09",
                         "filledUsdtAmount": "2.60"}])
        assert self._call(b) is None

    def test_a_bigger_sale_than_we_sent_is_not_ours(self):
        """LIVE 10:34: a manual UI trade sold 8.21 shares; a loose match would
        book it against the bot's 5.09-share position."""
        b = self._hist([{"side": "SELL", "status": "FILLED",
                         "createTime": 1_790_172_031_627,
                         "filledShareQty": "8.21",
                         "filledUsdtAmount": "1.30"}])
        assert self._call(b) is None

    def test_a_stepped_down_fill_still_matches(self):
        """`_sell_quote` may step the size down 1-10%; that is still our sale."""
        b = self._hist([{"side": "SELL", "status": "FILLED",
                         "createTime": 1_790_172_031_627,
                         "filledShareQty": "5.00",
                         "filledUsdtAmount": "2.55"}])
        assert self._call(b) is not None


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
        exists.

        GROSS of the fee, because `_cross_exit_execute` books
        `proceeds = sold*px - fee_paid`: the venue's `filledUsdtAmount` is what
        LANDED (2.10 on 3 sh @0.70 with a $0.02 fee), so booking THAT as the
        price would charge the fee twice (the same convention
        `PmBroker.sell_fak` uses)."""
        f = _sell(self._ok())
        assert f is not None
        assert f.stake == pytest.approx(2.12), "gross: net 2.10 + fee 0.02"
        assert f.shares == pytest.approx(3.0)
        assert f.filled_px == pytest.approx(2.12 / 3.0)
        assert f.fee_paid == pytest.approx(0.02)

    def test_the_fee_is_charged_once(self):
        """The one arithmetic that must hold end to end: what the exit books
        as proceeds is what the venue actually paid."""
        f = _sell(self._ok())
        booked = f.shares * f.filled_px - f.fee_paid
        assert booked == pytest.approx(2.10), (
            "sold*px - fee must be the venue's net cash, not the net minus "
            "the fee again"
        )

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
