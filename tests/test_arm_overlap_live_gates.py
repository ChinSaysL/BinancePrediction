"""Two live gates that `arm_overlap.arm_model` did not reproduce.

Both made the replay MORE permissive than the bot, which matters because every
knob in config.py is tuned on this replay:

  * the streak resets at every 1-minute candle open, unconditionally -- so a
    signal straddling a minute boundary starts again from 1 live. Without this
    every persistence above 1 was measured too generously, and the higher the
    persistence the larger the gift.
  * `up_regime_gate` withdraws the Up quote when the 1h return is too high,
    BEFORE the side pick, so a Down candidate on the same quote survives.

Found 2026-09-20 while chasing a fill-rate gap that turned out not to exist;
the gates are real regardless.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import arm_overlap as ao  # noqa: E402


# --- one_hour_return --------------------------------------------------------

def test_one_hour_return_uses_completed_bars_only():
    """⛔ The newest bar live can see is the minute BEFORE this tick. Reading
    the current minute would look ahead up to 59s on the gate's own input."""
    closes = {m: 100.0 for m in range(0, 200)}
    closes[150] = 999.0                      # the still-open current minute
    ts = 151 * 60_000 + 30_000               # a tick inside minute 151
    # newest completed = 150 ... which is the spiked one, so a reader that took
    # minute 151 would get a different answer. Pin the documented behaviour:
    assert ao.one_hour_return(closes, ts) == pytest.approx(math.log(999.0 / 100.0))


def test_one_hour_return_reaches_back_sixty_minutes():
    closes = {m: 100.0 * (1.01 ** m) for m in range(0, 200)}
    ts = 161 * 60_000
    got = ao.one_hour_return(closes, ts)
    assert got == pytest.approx(math.log(closes[160] / closes[100]))


def test_one_hour_return_none_without_an_hour_of_history():
    closes = {m: 100.0 for m in range(0, 30)}
    assert ao.one_hour_return(closes, 29 * 60_000) is None
    assert ao.one_hour_return({}, 10 * 60_000) is None


def test_one_hour_return_tolerates_a_gap():
    """Recording stalls; the nearest completed bar at or before is used."""
    closes = {m: 100.0 for m in range(0, 100)}
    closes[170] = 110.0
    ts = 175 * 60_000                       # 171..174 missing
    assert ao.one_hour_return(closes, ts) == pytest.approx(math.log(110.0 / 100.0))


def test_minute_closes_takes_the_last_spot_in_each_minute():
    rs = [{"rows": [(60_000, None, 10.0, None, None, None, None),
                    (90_000, None, 11.0, None, None, None, None),
                    (120_000, None, 12.0, None, None, None, None)]}]
    assert ao.minute_closes(rs) == {1: 11.0, 2: 12.0}


def test_minute_closes_ignores_missing_spot():
    rs = [{"rows": [(60_000, None, 10.0, None, None, None, None),
                    (90_000, None, None, None, None, None, None),
                    (95_000, None, 0.0, None, None, None, None)]}]
    assert ao.minute_closes(rs) == {1: 10.0}


# --- the candle reset -------------------------------------------------------

def _round(ticks):
    """ticks: list of (ts_ms, tau, spot, up_ask, dn_ask)."""
    return {"slug": "btc-updown-5m-1", "start_ms": 0, "end_ms": 0,
            "strike": 100.0, "winner": "Up", "day": 0, "end_price": 100.0,
            "rows": [(ts, tau, spot, None, None, ua, da)
                     for ts, tau, spot, ua, da in ticks]}


def _priced_ticks(base_ms, n, *, step_ms=1000):
    """Ticks deep enough in the money that `evaluate` acts on every one."""
    return [(base_ms + i * step_ms, 120.0, 100.6, 0.55, 0.50) for i in range(n)]


def test_streak_resets_on_a_candle_boundary(monkeypatch):
    """Three acting polls that STRADDLE a minute boundary must not satisfy a
    persistence of 3: live pops the streak at the boundary."""
    monkeypatch.setattr(ao.settings, "min_edge", 0.0)
    monkeypatch.setattr(ao.settings, "min_quote", 0.05)
    monkeypatch.setattr(ao.settings, "max_quote", 0.99)
    monkeypatch.setattr(ao.settings, "model_max_round_range_bps", 0.0)
    monkeypatch.setattr(ao.settings, "up_regime_gate", 0.0)
    # two acting polls late in minute 0, then one in minute 1. Live resets at
    # the boundary, so the third poll counts as 1 and never reaches 3.
    r = _round(_priced_ticks(40_000, 1) + _priced_ticks(50_000, 1)
               + _priced_ticks(61_000, 1))
    got = ao.arm_model(r, 0.0004, 3, exec_mode="market", latency_s=0.0,
                       apply_range_gate=False)
    assert got is None, "the boundary must have restarted the count"


def test_three_polls_inside_one_candle_do_fire(monkeypatch):
    monkeypatch.setattr(ao.settings, "min_edge", 0.0)
    monkeypatch.setattr(ao.settings, "min_quote", 0.05)
    monkeypatch.setattr(ao.settings, "max_quote", 0.99)
    monkeypatch.setattr(ao.settings, "model_max_round_range_bps", 0.0)
    monkeypatch.setattr(ao.settings, "up_regime_gate", 0.0)
    r = _round(_priced_ticks(61_000, 4))          # all inside minute 1
    got = ao.arm_model(r, 0.0004, 3, exec_mode="market", latency_s=0.0,
                       apply_range_gate=False)
    assert got is not None and got["side"] == "Up"


# --- up_regime_gate ---------------------------------------------------------

def _gate_round():
    return _round(_priced_ticks(61_000, 4))


def _closes_with_1h_return(ret):
    """Sixty-one minutes of history ending at minute 0, with the given log
    return, so a tick in minute 1 sees exactly `ret`."""
    c = {m: 100.0 for m in range(-70, -59)}
    for m in range(-59, 1):
        c[m] = 100.0 * math.exp(ret)
    return c


def test_up_is_withdrawn_when_the_hour_has_run(monkeypatch):
    monkeypatch.setattr(ao.settings, "min_edge", 0.0)
    monkeypatch.setattr(ao.settings, "min_quote", 0.05)
    monkeypatch.setattr(ao.settings, "max_quote", 0.99)
    monkeypatch.setattr(ao.settings, "model_max_round_range_bps", 0.0)
    got = ao.arm_model(_gate_round(), 0.0004, 1, exec_mode="market",
                       latency_s=0.0, apply_range_gate=False,
                       closes=_closes_with_1h_return(0.01), up_gate=0.005)
    assert got is None or got["side"] == "Down", "Up must have been withdrawn"


def test_up_survives_a_quiet_hour(monkeypatch):
    monkeypatch.setattr(ao.settings, "min_edge", 0.0)
    monkeypatch.setattr(ao.settings, "min_quote", 0.05)
    monkeypatch.setattr(ao.settings, "max_quote", 0.99)
    monkeypatch.setattr(ao.settings, "model_max_round_range_bps", 0.0)
    got = ao.arm_model(_gate_round(), 0.0004, 1, exec_mode="market",
                       latency_s=0.0, apply_range_gate=False,
                       closes=_closes_with_1h_return(0.001), up_gate=0.005)
    assert got is not None and got["side"] == "Up"


def test_gate_is_skipped_without_closes(monkeypatch):
    """Old callers that pass no history keep the previous behaviour rather than
    silently getting a gate evaluated against nothing."""
    monkeypatch.setattr(ao.settings, "min_edge", 0.0)
    monkeypatch.setattr(ao.settings, "min_quote", 0.05)
    monkeypatch.setattr(ao.settings, "max_quote", 0.99)
    monkeypatch.setattr(ao.settings, "model_max_round_range_bps", 0.0)
    got = ao.arm_model(_gate_round(), 0.0004, 1, exec_mode="market",
                       latency_s=0.0, apply_range_gate=False, up_gate=0.005)
    assert got is not None and got["side"] == "Up"
