"""The SDK returns **Paginators**. Iterating one yields PAGES, not records.

Every `list_*` call was treated as a plain list, and a Page is a perfectly
valid object whose attributes are all simply absent -- so each default hid the
failure and nothing raised. Measured cost on 2026-09-16:

* `_trades_since` -> always `None` ("the venue will not say"), so `_confirm`
  never confirmed and **six real fills were never booked**. The DB showed 861
  fills / max id 865 (all Binance-era) while the wallet had traded 5 Up @0.38,
  500 Down @0.01 and 4 x 5 Down @0.01.
* `active_orders` -> ONE ALL-BLANK ROW, so resting exposure was unreadable and
  `cancel_orders([""])` could cancel nothing.
* `_position_size` -> always 0.0, which reads as "no fill".
* `positions()` -> empty, so the claim sweep saw nothing.

The fakes below are shaped like the SDK's own (a Paginator of Pages, records
carried as attribute objects) so these tests fail if the unwrapping is removed.
"""
from __future__ import annotations

import asyncio
import ast
import re
from types import SimpleNamespace

from btcpredict.venues.polymarket.broker import (PmBroker, PmBrokerConfig,
                                                 _records)

TOK = "tok-down"

BROKER_SRC = "btcpredict/venues/polymarket/broker.py"


def strings_in(src: str) -> list[str]:
    """Every string literal in `src`, longest first (so nesting replaces
    cleanly). Used to strip documentation before a source-shape regex."""
    out = [n.value for n in ast.walk(ast.parse(src))
           if isinstance(n, ast.Constant) and isinstance(n.value, str)
           and n.value.strip()]
    return sorted(out, key=len, reverse=True)


class Page:
    """Mirrors the SDK's `Page(items=(), has_more=False, next_cursor=None)`."""

    def __init__(self, items=(), has_more=False):
        self.items = tuple(items)
        self.has_more = has_more
        self.next_cursor = None

    def __repr__(self):
        return f"Page(items={self.items!r}, has_more={self.has_more})"


class Paginator:
    """Mirrors the SDK's Paginator: iterable, and it yields PAGES."""

    def __init__(self, *pages):
        self.pages = list(pages)

    def first_page(self):
        return self.pages[0]

    def __iter__(self):
        return iter(self.pages)


def trade(asset_id=TOK, size=5.0, price=0.01, ts=1789594780.0,
          side="TAKER", fee_bps=None):
    return SimpleNamespace(asset_id=asset_id, size=size, price=price,
                           matched_at=ts, trader_side=side,
                           fee_rate_bps=fee_bps)


class FakeClient:
    def __init__(self, *, trades=None, orders=None, positions=None):
        self._trades, self._orders, self._positions = trades, orders, positions

    def list_account_trades(self, token_id=None):
        return self._trades

    def list_open_orders(self):
        return self._orders

    def list_positions(self):
        return self._positions


def make_broker(**kw):
    logs: list[str] = []
    fake = FakeClient(**kw)
    br = PmBroker(book=None, cfg=PmBrokerConfig(), log=logs.append, creds={})
    br._ensure_client = lambda: fake
    br.logs = logs
    return br


# ------------------------------------------------------- the unwrapping itself

class TestRecords:
    def test_a_paginator_of_pages_becomes_the_records(self):
        a, b = object(), object()
        assert _records(Paginator(Page([a, b]))) == [a, b]

    def test_multiple_pages_are_all_unwrapped(self):
        a, b = object(), object()
        assert _records(Paginator(Page([a], has_more=True), Page([b]))) == [a, b]

    def test_a_page_a_list_and_none_all_work(self):
        a = object()
        assert _records(Page([a])) == [a]
        assert _records([a]) == [a]
        assert _records(()) == []
        assert _records(None) == []

    def test_the_FAILURE_MODE_is_real_iterating_yields_pages(self):
        """Pins WHY this exists: the old code's element is a Page, whose
        `asset_id` is absent -- not an exception, just a silent default."""
        tr = trade()
        old = list(Paginator(Page([tr])))            # what the old code got
        assert len(old) == 1 and isinstance(old[0], Page)
        assert getattr(old[0], "asset_id", "") == ""  # matched nothing
        assert list(_records(Paginator(Page([tr])))) == [tr]


# --------------------------------------------------------- the money path

class TestTheFillsCanNowBeSeen:
    def test_trades_since_returns_the_fill_instead_of_None(self):
        """THE BUG. None means "the venue will not say", so `_confirm` gives
        up and no `fills` row is ever written -- for a fill that happened."""
        br = make_broker(trades=Paginator(Page([trade()])))
        got = br._trades_since(TOK, 0.0)
        assert got is not None, "a Paginator of real trades must not read as None"
        shares, px, fee = got
        assert shares == 5.0 and px == 0.01

    def test_a_trade_for_another_token_is_still_filtered_out(self):
        br = make_broker(trades=Paginator(Page([trade(asset_id="other")])))
        assert br._trades_since(TOK, 0.0) is None

    def test_the_venue_fee_is_charged_when_reported(self):
        br = make_broker(trades=Paginator(Page([trade(fee_bps=700)])))
        assert br._trades_since(TOK, 0.0)[2] == 700 / 1e4 * 0.01 * 5.0

    def test_position_size_is_read_not_defaulted_to_zero(self):
        br = make_broker(positions=Paginator(
            Page([SimpleNamespace(asset_id=TOK, current_size=500.0)])))
        assert br._position_size(TOK) == 500.0

    def test_active_orders_are_real_rows_not_one_blank_row(self):
        o = SimpleNamespace(id="0xabc", asset_id=TOK, price=0.01,
                            original_size=5.0, size_matched=0.0,
                            status="live", order_type="GTC")
        br = make_broker(orders=Paginator(Page([o])))
        rows = asyncio.run(br.active_orders())
        assert len(rows) == 1
        assert rows[0]["orderId"] == "0xabc"      # not ""
        assert rows[0]["size"] == 5.0             # not 0.0
        assert rows[0]["price"] == 0.01

    def test_no_open_orders_is_an_empty_list_not_a_blank_row(self):
        br = make_broker(orders=Paginator(Page([])))
        assert asyncio.run(br.active_orders()) == []


# ------------------------------------------------------ it cannot come back

class TestNoCallSiteIteratesAPaginatorRaw:
    def test_no_raw_paginator_iteration_survives(self):
        """A source check on purpose: the failure is SILENT, so a behavioural
        test on one call site cannot protect the next one somebody writes.

        String literals are stripped first -- the module DOCUMENTS the old
        shape, and a regex over raw source would flag its own explanation.
        """
        src = open(BROKER_SRC, encoding="utf-8").read()
        code = src
        for s in strings_in(src):
            code = code.replace(s, "")
        bad = re.findall(r"for \w+ in \w+ or \(\)|default=(?:res|page)\b", code)
        assert bad == [], (
            "these iterate/return a raw Paginator, so every record resolves to "
            f"a default and the failure is invisible: {bad}")

    def test_every_list_endpoint_is_unwrapped(self):
        src = open(BROKER_SRC, encoding="utf-8").read()
        for call, after in (
                ("c.list_account_trades(", "_records(page)"),
                ("c.list_positions()", "_records(page)"),
                ("c.list_open_orders()", "_records(res)"),
        ):
            i = src.index(call)
            assert after in src[i:i + 1400], (
                f"{call} is not unwrapped with {after} -- see _records")
