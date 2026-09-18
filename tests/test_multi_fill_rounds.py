"""One position per round was also a DATA STRUCTURE, not just a rule.

`open_fills` used to be keyed by slug. That is the same thing as one fill per
round, and it held silently for as long as nothing ever put two fills in one
round. The double-down does exactly that, and the collision was not caught by
any test because the invariant was never written down as code.

Measured 2026-09-12, first live doubled round: the base unit lost $2.23 and
booked its settlement, the add was still `open` and no settlement row was ever
written for it. The ledger understated the loss, and the restore path had the
same bug on restart.

These are structural assertions rather than behavioural ones because the failure
mode is structural: the code is correct as long as both booking sites agree on
the key. An AST check is the cheapest way to keep them in agreement.
"""
from __future__ import annotations

import ast
from pathlib import Path

MAIN = Path(__file__).resolve().parents[1] / "btcpredict" / "main.py"


def _src() -> str:
    return MAIN.read_text()


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.subscript_keys: list[str] = []
        self.loops: list[str] = []
        self.decl: str | None = None

    def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802
        if (isinstance(node.value, ast.Attribute)
                and node.value.attr == "open_fills"):
            self.subscript_keys.append(ast.unparse(node.slice))
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:  # noqa: N802
        tgt = ast.unparse(node.target)
        body = ast.unparse(node)
        if "open_fills.items()" in body:
            self.loops.append(tgt)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if (isinstance(node.target, ast.Attribute)
                and node.target.attr == "open_fills"):
            self.decl = ast.unparse(node)
        self.generic_visit(node)


def _visit() -> _Visitor:
    v = _Visitor()
    v.visit(ast.parse(_src()))
    return v


def test_open_fills_is_annotated_as_keyed_by_fill_id():
    v = _visit()
    assert v.decl is not None, "open_fills must be declared"
    assert "dict[int," in v.decl, (
        "open_fills must be keyed by fill id: keyed by slug, two fills in one "
        f"round collide. Got: {v.decl}")
    assert "slug -> (fill_id" not in v.decl, "the old slug-keyed shape is back"


def test_both_booking_sites_write_the_same_key():
    """`_book_fill` and the restore path must not drift apart."""
    src = _src()
    assert 'self.open_fills[fid] = (r.slug, fill)' in src, (
        "_book_fill must key by fid")
    assert 'self.open_fills[row["id"]] = (row["slug"], fill)' in src, (
        "the restore path must key by fid too")
    assert 'self.open_fills[r.slug]' not in src
    assert 'self.open_fills[row["slug"]]' not in src


def test_the_settle_loop_unpacks_the_new_order():
    v = _visit()
    assert v.loops, "settle_due must iterate open_fills.items()"
    for tgt in v.loops:
        # ast.unparse renders a tuple target WITH its parentheses.
        assert tgt.lstrip("(").startswith("fid"), (
            f"the loop must unpack (fid, (slug, fill)); got {tgt}")


def test_nothing_deletes_open_fills_by_slug_any_more():
    src = _src()
    assert "del self.open_fills[slug]" not in src
    assert "del self.open_fills[fid]" in src


def test_every_broker_mode_query_covers_all_of_its_modes():
    """`mode = broker.mode` excluded 'double' from the restore AND the guard seed.

    The second is the dangerous one: a restart that misses today's doubles
    understates the daily loss, and the guard then lets the bot trade past its
    own cap -- the failure the seed exists to prevent.
    """
    src = _src()
    assert 'f.mode = ?", (self.broker.mode,)' not in src, (
        "a bare broker-mode filter excludes the double")
    assert '"JOIN fills f ON f.id = s.fill_id WHERE f.mode = ? "' not in src, (
        "the guard seed must cover every mode its broker settles")
    assert "self._broker_modes()" in src

    # The string check this used to make broke the moment a fourth mode was
    # added, while a MISSING mode -- the actual bug -- would have slipped
    # through unchanged. Assert the invariant itself instead: every label
    # booked through the live broker must appear in the live mode tuple, or
    # its fill orphans on restart and the guard never sees its loss.
    import re
    from types import SimpleNamespace

    from btcpredict.main import PROBE_MODE, Bot

    bot = Bot.__new__(Bot)
    bot.broker = SimpleNamespace(mode="live")
    live_modes = set(Bot._broker_modes(bot))
    assert {"live", "double"} <= live_modes, (
        "the live broker settles at least 'live' and 'double'")

    # Lookbehind: a bare `mode="x"` argument only. Without it this also matched
    # the tail of `entry_mode="both"`, which is a STRATEGY mode, not a fill
    # label, and the test failed on a change that was correct.
    booked = set(re.findall(r'(?<![a-z_])mode="([a-z_]+)"', src))
    # `probe` is deliberately a separate measurement ledger: _book_fill's own
    # docstring says it books "separately without touching the live record".
    missing = booked - live_modes - {PROBE_MODE}
    assert not missing, (
        f"these modes are booked through the live broker but are not in "
        f"_broker_modes(), so their fills orphan on restart and their losses "
        f"never reach the daily cap: {sorted(missing)}")

    # THE SCAN ABOVE ONLY SEES `mode="literal"` ARGUMENTS, which is how the bug
    # this test exists for survived it: the Train arm's label is BUILT --
    # `arm_mode = "train" if train else "cross"`, then
    # `_book_fill(..., mode=arm_mode)` -- so 'train' was never in `booked` and
    # the tuple could omit it while this test passed. MEASURED COST: one
    # orphaned train fill held $0.98 of the exposure budget for 26 h and locked
    # the whole cross book out (0 orders in 2.5 h; see FINDINGS).
    # Assert the property the regex cannot: `pool_for_mode` books the RESERVED
    # slot for 'cross' AND 'train', and settle is the only thing that releases
    # it -- so both labels must be restorable, or the slot (and the exposure cap
    # that counts it) is held until the next hand-edit.
    assert {"cross", "train"} <= live_modes, (
        "every label pool_for_mode books into the RESERVED slot must appear "
        "here, or its fill orphans on restart and permanently occupies the "
        "exposure cap")


def test_the_guard_seed_and_the_restore_use_the_same_mode_set():
    """They must not drift: one builds the loss budget, the other the exposure."""
    src = _src()
    assert src.count("modes = self._broker_modes()") >= 2, (
        "both _seed_guard_state and _restore_open_positions must use it")
