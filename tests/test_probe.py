"""The probe arm takes decisions the selectivity gate DECLINED.

That makes its exclusion list a safety control, not a convenience. Probing a
decision declined for exposure would double the position in one round; probing
one declined after a risk halt would keep buying through the halt. Both are the
exact behaviours the gate exists to prevent, so they are pinned here.
"""
from __future__ import annotations

import pytest

from btcpredict.main import PROBE_MODE, PROBE_NEVER, probeable


@pytest.mark.parametrize("reason", [
    # existing exposure
    "venue already has a position for this round",
    "already positioned in this round",
    # risk halt / budget
    "daily loss cap halt",
    "RISK HALT: consecutive-loss streak",
    "bankroll below minimum stake",
    # data we could not read
    "spot stale (12.3s)",
    "venue position list unavailable",
    "venue order status unreadable",
    "order unconfirmed",
    "no position held at the venue",
])
def test_never_probes_exposure_risk_or_bad_data(reason: str) -> None:
    assert probeable(reason) is False


@pytest.mark.parametrize("reason", [
    "quote 0.04 below min_quote [0.50,0.95]",
    "tau 277s outside window",
    "mid-candle (12s past open; trades only within 3s)",
    "candle 3 not in (0, 4)",
    "side Up filtered (trading Down only)",
    "signal held 1/2 polls",
    "order cooldown after a failure",
    "below hurdle 0.0900",
])
def test_probes_selectivity_blocks(reason: str) -> None:
    assert probeable(reason) is True


def test_blank_reason_is_not_probed() -> None:
    """No reason means no decision was declined -- nothing to measure."""
    assert probeable("") is False
    assert probeable(None) is False


def test_mode_is_distinct_from_the_live_record() -> None:
    """Probe fills must never land in the live P&L they are meant to inform."""
    assert PROBE_MODE == "probe"
    assert PROBE_MODE != "live"


def test_exclusion_list_covers_the_gates_real_reasons() -> None:
    """Every non-selectivity gate string must match PROBE_NEVER.

    Source: the reason strings the gate actually writes (checked against
    `decisions.reason` on the live DB). If a new gate is added without adding
    its string here, the probe arm would start trading around a real limit.
    """
    real_blocks = [
        "venue already has a position for this round",
        "already positioned in this round",
        "venue position list unavailable",
        "spot stale (1.2s)",
    ]
    for r in real_blocks:
        assert not probeable(r), f"{r!r} must never be probed"
    assert "venue" in PROBE_NEVER
    assert "already positioned" in PROBE_NEVER
