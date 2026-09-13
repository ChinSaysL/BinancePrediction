"""A refused cancel is a question, not an answer.

Measured live 2026-09-12: the venue answers `-9000 SYSTEM_ERROR` to cancels it
will not take -- 15 of 15 cancels in one session, 104 times in the log since
09-11 (including `batch-redeem`), while the orders had already filled 98-99%.

The old code returned `[]` (nothing confirmed cancelled) and logged an ERROR.
That is unactionable in both directions:

  * treating it as "still working" cries wolf on every single order, and the one
    real orphan -- where a ~2% remnant keeps resting and fills later (23 of 451
    rounds show a second fill) -- is lost in the noise;
  * treating it as "cancelled" leaves that orphan unmanaged and understates
    booked exposure.

So the venue's own working-order list decides, and an unreadable list is
reported as UNKNOWN rather than as either answer.
"""
from __future__ import annotations

import asyncio

import pytest

from btcpredict.execution.live import LiveBroker


def broker(*, cancel_returns, working_sequences):
    """A LiveBroker with only the two collaborating calls wired up."""
    b = LiveBroker.__new__(LiveBroker)
    b.wallet_address = "0x0"
    calls: list[list[str]] = []
    seq = list(working_sequences)

    def _cancel(ids):
        calls.append(list(ids))
        return cancel_returns(ids)

    async def _request(method, path, params):
        return seq.pop(0) if seq else {"orders": []}

    b._cancel_blocking = _cancel          # type: ignore[method-assign]
    b._request = _request                 # type: ignore[method-assign]
    return b, calls


def test_a_confirmed_cancel_needs_no_reconciliation():
    """Nothing to investigate: never ask the venue a question you don't have."""
    b, calls = broker(cancel_returns=lambda ids: list(ids),
                      working_sequences=[{"orders": [{"orderId": "1"}]}])
    assert asyncio.run(b.cancel_orders(["1"])) == ["1"]
    assert calls == [["1"]]


def test_a_refused_cancel_for_a_finished_order_is_not_retried_blind():
    """-9000 with nothing actually working is noise, not an orphan."""
    b, calls = broker(cancel_returns=lambda ids: [],          # venue refuses
                      working_sequences=[{"orders": []}])     # nothing working
    assert asyncio.run(b.cancel_orders(["1", "2"])) == []
    assert calls == [["1", "2"]], "a blind retry would spam the venue"


def test_a_refused_cancel_that_is_genuinely_working_gets_one_retry():
    b, calls = broker(cancel_returns=lambda ids: [],
                      working_sequences=[{"orders": [{"orderId": "1"}]},
                                         {"orders": [{"orderId": "1"}]}])
    assert asyncio.run(b.cancel_orders(["1"])) == []
    assert calls == [["1"], ["1"]], "it must be retried exactly once"


def test_a_remainder_that_survives_the_retry_is_not_reported_as_cancelled():
    """The worst outcome is a silent orphan, so it must not be returned as done."""
    b, calls = broker(cancel_returns=lambda ids: [],
                      working_sequences=[{"orders": [{"orderId": "1"}]},
                                         {"orders": [{"orderId": "1"}]}])
    done = asyncio.run(b.cancel_orders(["1"]))
    assert "1" not in done
    assert len(calls) == 2, "two attempts, then stop -- do not loop on the venue"


def test_an_unreadable_working_list_is_reported_as_unknown():
    """`_request` returns None on failure. That is not 'nothing is working'."""
    b, calls = broker(cancel_returns=lambda ids: [],
                      working_sequences=[None])
    assert asyncio.run(b.cancel_orders(["1"])) == []
    assert calls == [["1"]], "unknown state must not trigger a blind retry"


def test_only_the_unconfirmed_ids_are_reconciled():
    """A confirmed id must never be re-sent; only the refused one is retried."""
    b, calls = broker(cancel_returns=lambda ids: [ids[0]],
                      working_sequences=[{"orders": [{"orderId": "2"}]}])
    done = asyncio.run(b.cancel_orders(["1", "2"]))
    assert calls == [["1", "2"], ["2"]], "never re-cancel what was confirmed"
    assert set(done) == {"1", "2"}, "the retried id is cancelled on the retry"


@pytest.mark.parametrize("ids", [[], ()])
def test_no_ids_is_a_no_op(ids):
    b, calls = broker(cancel_returns=lambda x: [], working_sequences=[])
    assert asyncio.run(b.cancel_orders(list(ids))) == []
    assert calls == []
