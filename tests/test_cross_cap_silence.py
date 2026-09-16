"""The per-round cap must not drop crossings SILENTLY.

`_cross_tick` returns early when a round has already used its `max_per_round`
slot. That return used to happen before anything was logged or written to
`decisions`, so every crossing the cap rejected was absent from the record --
which makes a log-derived fire count a count of UNBLOCKED fires, and the cap's
cost unmeasurable. `3b92a87` records the same silence hiding an entire cross
window on 2026-09-15.

With `cross_max_per_round = 1` this is the common case, not an edge case: every
re-crossing in a round is dropped, and the 12:19 round on 09-16 spent 124 polls
on the crossed side of the strike.

The guard is inline in `_cross_tick` and needs a full round to execute, so this
pins it at the AST level -- the same approach `test_decision_path.py` uses for
its undefined-name net.
"""

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "btcpredict" / "main.py"


def _cap_guard():
    """Every `if` statement in main.py whose test mentions the per-round cap."""
    tree = ast.parse(SRC.read_text())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        names = {n.attr for n in ast.walk(node.test) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        src = ast.dump(node.test)
        if "_cross_fired" in src and "max_per_round" in src:
            out.append(node)
    return out


def test_the_per_round_cap_guard_exists():
    assert _cap_guard(), "the _cross_fired vs max_per_round guard is gone"


def _refuse_calls(node):
    return [c for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr == "_cross_refuse"]


def test_the_cap_guard_records_a_decision_row_not_just_a_log():
    """`_cross_refuse` is the helper that LOGS and INSERTS into `decisions`.
    A bare return, or a bare log.info, leaves the cap's cost unmeasurable --
    a log line cannot be joined against settled P&L."""
    for node in _cap_guard():
        assert _refuse_calls(node), (
            "the max_per_round guard drops the crossing without recording it "
            "(main.py line %d)" % node.lineno
        )


def test_the_recorded_row_carries_what_the_counterfactual_needs():
    """spot + strike give the gap; tau + sigma_1m give z. Without them the row
    cannot be priced, which is the entire point of recording it."""
    for node in _cap_guard():
        for call in _refuse_calls(node):
            kws = {k.arg for k in call.keywords}
            assert {"tau", "spot"} <= kws, (
                "the cap-block decision row must pass tau and spot so z is "
                "recoverable (main.py line %d)" % node.lineno
            )


def test_it_only_records_when_a_crossing_was_actually_detected():
    """A tick with no crossing would have returned at the trigger anyway;
    recording it would invent rows for rounds that never crossed.

    `crossed_side` is a module-level function, so it appears as a Name call --
    not an Attribute call like the `_cross_refuse` and `_ck` methods.
    """
    for node in _cap_guard():
        called = set()
        for c in ast.walk(node):
            if not isinstance(c, ast.Call):
                continue
            if isinstance(c.func, ast.Name):
                called.add(c.func.id)
            elif isinstance(c.func, ast.Attribute):
                called.add(c.func.attr)
        assert "crossed_side" in called, (
            "_cross_refuse is called without resolving the side first, so every "
            "blocked tick records a row whether or not a crossing existed "
            "(main.py line %d)" % node.lineno
        )


def test_the_message_says_the_crossing_is_dropped():
    """Findable by someone asking this exact question."""
    text = SRC.read_text()
    idx = text.find("crossing DROPPED")
    assert idx > 0, "the cap-block reason lost the word DROPPED"
    guard_start = min(n.lineno for n in _cap_guard())
    assert text[:idx].count("\n") + 1 > guard_start
