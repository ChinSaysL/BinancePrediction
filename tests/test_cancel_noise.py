"""Cancel logging: quiet on the expected refusal, loud on everything else.

MEASURED 2026-09-13 over the full live log: 199 batch-cancel -9000 refusals,
of which `cancel_orders` found a genuinely stuck order ZERO times and failed to
read the working-order list ZERO times. Booked stake and shares matched the
venue's record exactly on every sampled fill, and an order reading 98% at fill
time still read 98% hours later -- so the 1-2% tail is dead, not unbooked.

The risk in quieting a warning is quieting the one that matters, so these pin
BOTH directions.
"""
from __future__ import annotations

import logging

import pytest

from btcpredict.execution import live as live_mod


class FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        import json
        return json.dumps(self._body).encode()


class FakeConn:
    def __init__(self, status, body):
        self._r = FakeResp(status, body)

    def request(self, *a, **k):
        return None

    def getresponse(self):
        return self._r

    def close(self):
        return None


def _broker():
    b = live_mod.LiveBroker.__new__(live_mod.LiveBroker)
    b.api_key = "k"
    b.api_secret = b"s"
    b.recv_window = 5000
    b.wallet_address = "0xabc"
    b.wallet_id = "1"
    return b


def _run(monkeypatch, status, body, caplog):
    b = _broker()
    monkeypatch.setattr(live_mod.http.client, "HTTPSConnection",
                        lambda *a, **k: FakeConn(status, body))
    with caplog.at_level(logging.DEBUG, logger="btcpredict.execution.live"):
        out = b._cancel_blocking(["123"])
    return out, caplog.records


def test_the_expected_minus_9000_is_not_a_warning(monkeypatch, caplog):
    """The normal case: a 1-2% tail that already expired with its round."""
    out, recs = _run(monkeypatch, 400, {"code": -9000, "msg": "SYSTEM_ERROR"},
                     caplog)
    assert out == []
    levels = {r.levelno for r in recs if "batch-cancel" in r.getMessage()}
    assert logging.WARNING not in levels, (
        "199 warnings for a condition that never mattered trains you to "
        "ignore the log")
    assert logging.DEBUG in levels, "it must still be recoverable at DEBUG"


@pytest.mark.parametrize("body", [
    {"code": -1022, "msg": "Signature for this request is not valid."},
    {"code": -1102, "msg": "Mandatory parameter was not sent"},
    {"msg": "no code at all"},
])
def test_any_other_failure_stays_loud(monkeypatch, caplog, body):
    """The signature and parameter errors are real bugs -- and -1022 in
    particular is the one this venue returns when the bracket encoding breaks,
    which silently disabled cancelling once already."""
    out, recs = _run(monkeypatch, 400, body, caplog)
    assert out == []
    levels = {r.levelno for r in recs if "batch-cancel" in r.getMessage()}
    assert logging.WARNING in levels, "only -9000 is known-benign"


def test_a_successful_cancel_returns_the_ids(monkeypatch, caplog):
    out, _ = _run(monkeypatch, 200, {"canceled": ["123"]}, caplog)
    assert out == ["123"]
