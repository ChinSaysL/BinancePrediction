"""The Polymarket take arms must TAKE -- `pm_order_type=fak` routes them there.

WHY THE KNOB NEEDED A ROUTING GUARD AND NOT JUST A SETTING. `pm_order_type` only
reaches `buy_fak()` through `broker.buy()`, and the arms did not call `buy()`:
they called `buy_limit()`, which on Polymarket is `post_only=True`. So the knob
was settable and inert, and the arms kept posting an order that cannot cross.

Two live orders put the guard here (2026-09-17 03:44, the first two attempts after
the stake-cap fix reached the venue at all):

    limit post CROSS eth-updown-5m-1789616400 Up: 0.84 (ceiling 0.84, quote
    0.8227)          -> POST /order 200 OK
    cross fine ... 05=0.970 10=0.980 ...            -> the book had already run
    pm limit unfilled ... @ 0.84 after 6053 ms      -> expired, book now 0.9900

The order rested at 0.84 for six seconds while the venue sat at 0.97-0.99. A FAK
at the SAME ceiling would also have missed (nothing was on offer at 0.84) -- the
difference this guard makes is that the order is one the arms can actually send,
and that there is no leftover resting as bait. So these tests pin the ROUTING, not
a P&L claim.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from btcpredict.main import pm_take_via_fak

MAIN = pathlib.Path(__file__).resolve().parents[1] / "btcpredict" / "main.py"


class _PmBroker:
    """Polymarket: has both paths, and `buy()` routes to FAK under the knob."""

    mode = "live"

    async def buy_fak(self, **_kw):            # pragma: no cover - shape only
        return None


class _WrapperBroker:
    """The Binance wrapper: no `buy_fak`, and IOC is rejected with -1102."""

    mode = "live"

    async def buy_limit(self, **_kw):          # pragma: no cover - shape only
        return None


class TestWhenTheFakPathIsUsed:
    def test_a_polymarket_take_arm_under_the_knob(self) -> None:
        s = type("S", (), {"pm_order_type": "fak"})()
        assert pm_take_via_fak(s, _PmBroker(), 0.84) is True

    def test_the_knob_is_case_insensitive(self) -> None:
        s = type("S", (), {"pm_order_type": "FAK"})()
        assert pm_take_via_fak(s, _PmBroker(), 0.84) is True

    def test_the_default_knob_leaves_the_limit_path_alone(self) -> None:
        s = type("S", (), {"pm_order_type": "limit"})()
        assert pm_take_via_fak(s, _PmBroker(), 0.84) is False

    def test_a_missing_knob_reads_as_limit(self) -> None:
        """A settings object without the field must not silently switch paths."""
        s = type("S", (), {})()
        assert pm_take_via_fak(s, _PmBroker(), 0.84) is False

    def test_the_model_arm_keeps_its_resting_bid(self) -> None:
        """`limit_ceiling` is set only by the cross/train TAKE path.

        The model arm's limit is a genuine maker bid (its fair, posted behind the
        book), and `tests/test_maker_path.py` measures that as the thing that
        makes money there. Sending it as a FAK would pay the spread instead.
        """
        s = type("S", (), {"pm_order_type": "fak"})()
        assert pm_take_via_fak(s, _PmBroker(), None) is False

    def test_the_binance_wrapper_is_untouched(self) -> None:
        """MARKET=FOK/LIMIT=GTC only; `buy_fak` does not exist there."""
        s = type("S", (), {"pm_order_type": "fak"})()
        assert pm_take_via_fak(s, _WrapperBroker(), 0.84) is False


class TestTheOrderPathActuallyConsultsIt:
    """A pure helper nobody calls is a comment. Pin the call site."""

    @staticmethod
    def _place_order_node() -> ast.AsyncFunctionDef:
        tree = ast.parse(MAIN.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_place_order":
                return node
        pytest.fail("_place_order is gone: this guard is measuring nothing")

    def test_the_guard_runs_before_the_limit_branch(self) -> None:
        fn = self._place_order_node()
        assign = [n.lineno for n in ast.walk(fn)
                  if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "use_limit"
                          for t in n.targets)]
        guard = [n.lineno for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "id", "") == "pm_take_via_fak"]
        assert assign, "the `use_limit = ...` decision disappeared"
        assert guard, "_place_order no longer consults pm_take_via_fak"
        assert guard[0] > assign[0], (
            "the FAK routing must settle `use_limit` AFTER it is assigned, or the "
            "LIMIT path is chosen anyway")

    def test_the_guard_can_only_ever_clear_use_limit(self) -> None:
        """It must not become a second way to ENABLE the limit path."""
        fn = self._place_order_node()
        guards = [n for n in ast.walk(fn)
                  if isinstance(n, ast.If)
                  and any(isinstance(c, ast.Call)
                          and getattr(c.func, "id", "") == "pm_take_via_fak"
                          for c in ast.walk(n.test))]
        assert guards, "no `if pm_take_via_fak(...)` guard in _place_order"
        assigns = [s for g in guards for s in g.body
                   if isinstance(s, ast.Assign)]
        assert len(assigns) == 1, "the guard must make exactly one assignment"
        target = assigns[0].targets[0]
        assert isinstance(target, ast.Name) and target.id == "use_limit"
        value = assigns[0].value
        assert isinstance(value, ast.Constant) and value.value is False, (
            "the guard may only ever CLEAR use_limit; enabling the limit path is "
            "the setting's job, not this branch's")
