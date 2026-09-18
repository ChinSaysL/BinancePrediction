"""The train arm's ask gate: a static knob TIGHTENED by the dynamic EV ceiling.

The point of the change is that the gate is now arithmetic on a measured win
rate instead of a remembered constant. These tests pin the two properties that
make that safe to ship:

1. it can only ever REFUSE MORE (min of the two), so enabling it cannot open a
   gate that was already closed; and
2. it fails back to the static value rather than to 0.0, because 0 is this
   repo's convention for "gate OFF" -- returning it when the measurement says
   there is no edge would OPEN the gate exactly when the arm is unprofitable.
"""
from __future__ import annotations

import pytest

from btcpredict.config import settings
from btcpredict.venues.polymarket.broker import TAKER_FEE_RATE, entry_ceiling

STATIC = 0.890                 # cross_train_max_ask
P_WIN = 0.914                  # the measured PM sweep result
MARGIN = 0.03


class TestItTightens:
    def test_the_live_pair_tightens_the_static_gate(self):
        assert entry_ceiling(STATIC, P_WIN, MARGIN) == pytest.approx(0.876, abs=5e-4)
        assert entry_ceiling(STATIC, P_WIN, MARGIN) < STATIC

    def test_a_lower_measured_win_rate_tightens_it_further(self):
        lo = entry_ceiling(STATIC, 0.85, MARGIN)
        hi = entry_ceiling(STATIC, 0.95, MARGIN)
        assert lo < STATIC              # a poor win rate tightens the gate
        assert hi == STATIC             # a strong one cannot LOOSEN past it

    def test_a_bigger_margin_tightens_it_further(self):
        assert entry_ceiling(STATIC, P_WIN, 0.05) < entry_ceiling(STATIC, P_WIN, 0.01)

    def test_it_can_only_refuse_more_never_less(self):
        """The property that makes shipping this safe: for ANY input the result
        is <= the static gate, so no already-refused order becomes allowed."""
        for p in (0.05, 0.3, 0.5, 0.7, 0.8, 0.876, 0.914, 0.99):
            for m in (0.0, 0.01, 0.03, 0.1):
                got = entry_ceiling(STATIC, p, m)
                assert got <= STATIC + 1e-9, (p, m, got)
                assert got > 0, (p, m, got)

    def test_it_is_honest_about_the_fee(self):
        """Same p_win, zero fee -> a HIGHER ceiling. If the fee were ignored
        this would not move, and the gate would be too loose by the fee."""
        assert entry_ceiling(1.0, P_WIN, MARGIN) < entry_ceiling(
            1.0, P_WIN, MARGIN, fee_rate=0.0)


class TestItFailsToTheStaticValue:
    def test_an_unset_win_rate_leaves_the_gate_alone(self):
        """0 is how a knob is disabled; it must not become a ceiling."""
        assert entry_ceiling(STATIC, 0.0, MARGIN) == STATIC

    def test_a_non_probability_leaves_the_gate_alone(self):
        for bad in (-1.0, 1.0, 1.5, 2.0):
            assert entry_ceiling(STATIC, bad, MARGIN) == STATIC

    def test_an_unprofitable_pair_does_NOT_open_the_gate(self):
        """ev_ceiling -> 0.0 here. Returning 0.0 would read as 'gate OFF' and
        admit every price -- the opposite of what the measurement says."""
        got = entry_ceiling(STATIC, 0.02, 0.05)
        assert got == STATIC
        assert got != 0.0

    def test_the_shipped_defaults_come_from_the_measurement(self):
        assert settings.train_p_win == pytest.approx(0.914)
        assert settings.ev_ceiling_margin == pytest.approx(0.03)
        assert TAKER_FEE_RATE == pytest.approx(0.07)
        assert entry_ceiling(settings.cross_train_max_ask,
                             settings.train_p_win,
                             settings.ev_ceiling_margin) < 0.890


class TestItIsWiredIntoTheArm:
    def test_main_uses_entry_ceiling_for_the_train_gate(self):
        """Read from the SOURCE: the gate is one line in a very long function
        and a refactor that drops the ceiling would be invisible in behaviour
        until the win rate moved."""
        src = open("btcpredict/main.py", encoding="utf-8").read()
        i = src.index('tmax = entry_ceiling(')
        block = src[i:i + 400]
        assert 'self._ck("train_max_ask", r)' in block
        assert "settings.train_p_win" in block
        # and the ceiling must not have replaced the refusal
        assert "train ask above the gate" in src[i:i + 1200]
