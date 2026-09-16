"""The fill mode must name the ARM, and everything keyed on it must agree.

`pool_for_mode` had no test. That is why this slipped: the Train arm's orders
run through the same `_cross_execute` as Cross, the log line derived the arm
from `sig.reason` (correct), and the booked fill mode was the literal "cross"
(wrong). Nothing failed, because a fill labelled 'cross' behaves exactly like a
fill labelled 'cross' -- it just made the two arms' P&L look like one arm's.

Measured 2026-09-16 on the live ledger: the pooled cohort was 46 train fills
(win 78.3%, paid 0.8406, EV/sh -0.0612) and 38 cross fills, i.e. two arms with
opposite signs. The tests below pin the three places that must move together:
the label, the pool it books, and the exposure cap that reads it.
"""
from __future__ import annotations

import ast
import pathlib

from btcpredict.main import pool_for_mode

MAIN = pathlib.Path(__file__).resolve().parents[1] / "btcpredict" / "main.py"


def _tree() -> ast.Module:
    return ast.parse(MAIN.read_text())


def _find_func(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in main.py")


def test_train_books_the_reserved_pool_like_cross():
    """Train claims the same slot as Cross, so it must release the same budget.

    Splitting the label without splitting THIS would leave the reserved slot
    claimed by one arm and released by another -- silent forever.
    """
    assert pool_for_mode("cross") == (True, None)
    assert pool_for_mode("train") == (True, None)


def test_other_modes_are_unchanged():
    assert pool_for_mode("impulse") == (False, "impulse")
    assert pool_for_mode("maker") == (False, "impulse")
    for other in ("live", "stale_maker", "double", "paper", None):
        assert pool_for_mode(other) == (False, None)


def test_exposure_cap_counts_both_arms():
    """A label change without this line makes the cap blind to train positions.

    That is the "silently stops capping" failure `_open_cross_stake` warns
    about in its own docstring; it is worse than a wrong label because it lets
    real exposure through.
    """
    src = ast.get_source_segment(MAIN.read_text(), _find_func(_tree(), "_open_cross_stake"))
    assert "IN ('cross', 'train')" in src, (
        "the exposure cap must sum BOTH cross and train fills")


def test_fill_mode_is_not_a_hardcoded_literal():
    """The booked mode must come from the train flag, not the string 'cross'."""
    src = MAIN.read_text()
    fn = _find_func(_tree(), "_cross_execute")

    booked = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "_book_fill":
            for kw in node.keywords:
                if kw.arg == "mode":
                    booked = kw.value
    assert booked is not None, "no _book_fill(mode=...) call found"

    assert isinstance(booked, ast.Name), (
        f"fill mode is not derived from a variable: {ast.dump(booked)}")
    assert booked.id == "arm_mode"

    # ...and arm_mode is driven by the train flag, in the same function.
    assigned = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "arm_mode" for t in n.targets)]
    assert assigned, "arm_mode is never assigned in _cross_execute"
    ternary = ast.dump(assigned[0].value)
    assert isinstance(assigned[0].value, ast.IfExp) and "train" in ternary, ternary


def test_cross_execute_receives_the_train_flag():
    """The flag must be passed in, not re-derived from the reason string.

    Re-deriving is how the log and the ledger drifted apart in the first place.
    """
    fn = _find_func(_tree(), "_cross_execute")
    names = [a.arg for a in fn.args.kwonlyargs]
    assert "train" in names, f"_cross_execute keyword args: {names}"
