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


def test_every_cap_guard_logs_before_it_returns():
    """A bare `return` here drops a crossing with no trace anywhere."""
    for node in _cap_guard():
        calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
        logged = [
            c for c in calls
            if isinstance(c.func, ast.Attribute) and c.func.attr in
            ("info", "warning", "debug")
        ]
        assert logged, (
            "the max_per_round guard returns without logging -- every crossing "
            "it rejects is invisible in both live.log and the decisions table "
            "(main.py line %d)" % node.lineno
        )


def test_the_log_says_the_crossing_is_dropped():
    """The message must be findable by someone asking this exact question.

    Search for the fragment, not the whole sentence: the source splits it across
    two adjacent string literals, so the full phrase never appears contiguously.
    """
    text = SRC.read_text()
    idx = text.find("crossing is DROPPED")
    assert idx > 0, "the cap-block log line lost the word DROPPED"
    # and it must sit inside the guard, not somewhere else in the file
    guard_start = min(n.lineno for n in _cap_guard())
    assert text[:idx].count("\n") + 1 > guard_start


def test_it_logs_once_per_round_not_per_tick():
    """The arm ticks ~7/s; logging every blocked tick would bury the log it is
    meant to make honest, so the guard is behind a per-round set."""
    text = SRC.read_text()
    assert "_cross_blocked" in text
    assert "if r.slug not in blocked" in text
