"""The fill may not exceed a MULTIPLE of the ask the arm actually saw.

WHY THIS EXISTS. Live 2026-09-17, `sol-updown-5m-1789649400`: the trigger ask was
**0.100**, the ceiling `ask + buf` was **0.28**, and the order **filled at
0.200** -- 2.0x the only price the arm ever observed -- on a round that resolved
against us. Two candles later `doge-updown-5m-1789649700` paid 0.46 against a
0.61 ask (0.75x), i.e. the cap costs nothing on the trades that behave.

The mechanism is that an ABSOLUTE buffer is regressive in the ask:

    ask 0.10 -> +0.18 (inv*(1-ask)) -> 2.8x
    ask 0.60 -> +0.20 (absolute)    -> 1.3x

so the arm bid furthest from the price it saw exactly where the price it saw was
least reliable.

IT IS A RISK BOUND, NOT AN EDGE, and the tests say so. On the 8-day spring corpus
(`tools/pm_range_gate_corpus.py --sweep cap`, 122 triggers) the EV is flat from
1.25 to off, k=1.0 is best per trade but drops 37% of triggers and makes LESS
total money, and only k=1.5 is nearly free (8 triggers, 0.0077 EV/share). So what
is pinned here is the BEHAVIOUR -- it can only tighten, and 0 disables it -- not
a claimed improvement.
"""
from __future__ import annotations

import ast
import math
import pathlib

import pytest

from btcpredict.config import Settings

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = (ROOT / "btcpredict" / "main.py").read_text()


def _flat(fn: str) -> str:
    seg = next((ast.get_source_segment(MAIN, n) for n in ast.walk(ast.parse(MAIN))
                if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                and n.name == fn), None)
    assert seg is not None, f"{fn} is gone: this guard would measure nothing"
    return " ".join(seg.split())


def _ceil(ask: float, buf: float, cap: float, mult: float) -> float:
    """The live ceiling arithmetic, transcribed from `_cross_all`."""
    c = min(cap, round(ask + buf, 2))
    if mult and mult > 0:
        c = min(c, math.floor(ask * mult * 100) / 100)
    return c


class TestTheDefault:
    def test_it_ships_OFF(self) -> None:
        """OFF because the total PnL is worse, not the average: +19.15 ->
        +17.17 over 6 days of triggers, to buy a bound that would not have
        prevented the loss it was built for (a loser loses its stake either
        way). Re-enable by setting 1.5 in .env."""
        assert Settings.model_fields["cross_max_fill_multiple"].default == 0.0

    def test_enabling_it_would_have_capped_the_sol_fill(self) -> None:
        """ask 0.100, buf 0.18 (inv 0.2), cap 0.65 -> 0.28 uncapped, 0.15 at
        1.5x. The order filled at 0.200, so at 1.5 the fill cannot happen there."""
        assert _ceil(0.10, 0.18, 0.65, 0.0) == 0.28
        assert _ceil(0.10, 0.18, 0.65, 1.5) == 0.15
        assert 0.200 > _ceil(0.10, 0.18, 0.65, 1.5)

    def test_the_doge_fill_is_untouched(self) -> None:
        """ask 0.61, filled 0.46 -- a cap that blocks this would be a tax."""
        assert _ceil(0.61, 0.20, 0.65, 1.5) == 0.65
        assert 0.46 <= _ceil(0.61, 0.20, 0.65, 1.5)


class TestItCanOnlyTighten:
    @pytest.mark.parametrize("ask", [0.01, 0.05, 0.10, 0.25, 0.40, 0.55, 0.64])
    def test_never_loosens_the_absolute_ceiling(self, ask: float) -> None:
        buf = max(0.01, min(0.25, 0.20 * (1.0 - ask)))
        assert _ceil(ask, buf, 0.65, 1.5) <= _ceil(ask, buf, 0.65, 0.0)

    @pytest.mark.parametrize("mult", [0.0, -1.0, None])
    def test_zero_and_junk_disable_it(self, mult) -> None:
        """0 is this repo's convention for OFF; a junk value must not close the
        gate to nothing (the `>= 0` failure mode that a `if mult:` test invites)."""
        assert _ceil(0.30, 0.20, 0.65, mult) == _ceil(0.30, 0.20, 0.65, 0.0)


class TestItIsWiredIntoTheCrossCeiling:
    def test_the_cross_path_reads_the_knob(self) -> None:
        body = _flat("_cross_tick")
        assert 'self._ck("max_fill_multiple", r)' in body
        assert "math.floor(ask * mult * 100) / 100" in body

    def test_it_is_guarded_against_zero(self) -> None:
        body = _flat("_cross_tick")
        assert "if mult and mult > 0:" in body, \
            "an unguarded multiply would collapse the ceiling to 0 and block " \
            "every cross the moment the knob is disabled"
