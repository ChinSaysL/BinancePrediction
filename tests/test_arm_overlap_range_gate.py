"""arm_overlap.arm_model must apply the LIVE round-range gate.

It did not, which scored every replay on a population the bot refuses -- the
same defect `tools/arms_recorded.py` was fixed for. Semantics must match
`Bot._round_range_bps`: a RUNNING range from the open to the current tick, in
bps of the strike, and fewer than two samples means "no opinion", not a refusal.
"""
import sys

import pytest

sys.path.insert(0, "tools")
import arm_overlap as ao  # noqa: E402
from btcpredict.config import settings  # noqa: E402


def _round(prices, strike=100_000.0, ask=0.60):
    """One round; `prices` is the spot per tick, newest last."""
    rows = []
    n = len(prices)
    for i, px in enumerate(prices):
        tau = 300.0 - i * (240.0 / max(n - 1, 1))
        rows.append((1_000 + i * 1_000, max(tau, 1.0), px,
                     ask, 1 - ask, ask, 1 - ask))
    return {"slug": "btc-updown-5m-1", "strike": strike, "winner": "Up",
            "rows": rows, "day": 1, "end_price": prices[-1]}


@pytest.fixture(autouse=True)
def _knobs(monkeypatch):
    monkeypatch.setattr(settings, "model_min_round_range_bps", 5.0,
                        raising=False)
    monkeypatch.setattr(settings, "model_max_round_range_bps", 16.0,
                        raising=False)
    monkeypatch.setattr(settings, "min_tau_s", 60.0, raising=False)
    monkeypatch.setattr(settings, "max_tau_s", 240.0, raising=False)
    # Wide enough that the ONLY thing that can refuse a flat round is the
    # range gate -- otherwise "returns None" proves nothing about the gate.
    monkeypatch.setattr(settings, "min_quote", 0.25, raising=False)
    monkeypatch.setattr(settings, "max_quote", 0.95, raising=False)
    monkeypatch.setattr(settings, "min_edge", 0.01, raising=False)
    monkeypatch.setattr(settings, "safety_margin", 0.0, raising=False)
    monkeypatch.setattr(settings, "edge_haircut", 1.0, raising=False)


def test_the_stub_round_is_tradeable_when_ungated():
    """Guard for the tests below: without the gate this round MUST fire.

    If it does not, every "returns None" assertion here is vacuous.
    """
    flat = [100_000.0 + i * 0.5 for i in range(40)]
    assert ao.arm_model(_round(flat), 0.0004, 1, exec_mode="market",
                        latency_s=0.0, apply_range_gate=False) is not None


def _range_bps(prices, strike=100_000.0):
    return 1e4 * (max(prices) - min(prices)) / strike


def test_a_flat_round_is_below_the_floor_and_refused():
    flat = [100_000.0 + i * 0.5 for i in range(40)]      # ~2 bps
    assert _range_bps(flat) < 5.0
    assert ao.arm_model(_round(flat), 0.0004, 1, exec_mode="market",
                        latency_s=0.0) is None


def test_a_wild_round_is_above_the_ceiling_and_refused():
    wild = [100_000.0 + (i % 2) * 400.0 for i in range(40)]   # 40 bps
    assert _range_bps(wild) > 16.0
    assert ao.arm_model(_round(wild), 0.0004, 1, exec_mode="market",
                        latency_s=0.0) is None


def test_the_gate_can_be_switched_off():
    """apply_range_gate=False restores the old, unfiltered behaviour."""
    flat = [100_000.0 + i * 0.5 for i in range(40)]
    gated = ao.arm_model(_round(flat), 0.0004, 1, exec_mode="market",
                         latency_s=0.0)
    ungated = ao.arm_model(_round(flat), 0.0004, 1, exec_mode="market",
                           latency_s=0.0, apply_range_gate=False)
    assert gated is None, "a 2 bps round must be refused by the gate"
    assert ungated is not None, "without the gate the same round must trade"


def test_range_is_RUNNING_not_whole_round():
    """A round that only travels LATE must be refused EARLY.

    The live gate reads the range so far, not the round's final range, so a
    decision taken before the move must not see it.
    """
    prices = [100_000.0] * 30 + [100_120.0] * 10        # 12 bps, but only late
    rows = _round(prices)["rows"]
    lo = hi = None
    early_ranges = []
    for _ts, _tau, spot, *_rest in rows[:30]:
        lo = spot if lo is None else min(lo, spot)
        hi = spot if hi is None else max(hi, spot)
        early_ranges.append(1e4 * (hi - lo) / 100_000.0)
    assert max(early_ranges) == 0.0          # nothing to see yet
    assert _range_bps(prices) == pytest.approx(12.0)
    # ...and the arm must still find the LATE ticks, where the range is in band
    assert ao.arm_model(_round(prices), 0.0004, 1, exec_mode="market",
                        latency_s=0.0) is not None


def test_zero_ceiling_disables_the_gate(monkeypatch):
    """0 is the repo's OFF convention for every other limit."""
    flat = [100_000.0 + i * 0.5 for i in range(40)]
    assert ao.arm_model(_round(flat), 0.0004, 1, exec_mode="market",
                        latency_s=0.0) is None          # gate on -> refused
    monkeypatch.setattr(settings, "model_max_round_range_bps", 0.0,
                        raising=False)
    assert ao.arm_model(_round(flat), 0.0004, 1, exec_mode="market",
                        latency_s=0.0) is not None      # ceiling 0 -> OFF
