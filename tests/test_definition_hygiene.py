"""No module may define the same top-level name twice.

Found on 2026-09-16: `btcpredict/pricing/digital.py` defined `moneyness_sd`
TWICE. The second definition silently shadowed the first, so the first one's
docstring -- which promised "returns None when it cannot be computed, and
callers must treat that as no evidence, or a missing sigma silently becomes a
passing gate" -- described a contract that was never in force. Python binds the
last definition and says nothing. Nothing in the suite could notice.

An unused duplicate is only the benign version of this. The same shape hides:
a second copy of a gate function where the old one had a guard the new one
dropped; a helper redefined later in a long module "temporarily"; a decorator
stack applied to one copy but not the other. The file still imports, the tests
still pass, and the name at the call site resolves to something the author was
not looking at.

So: parse every module and fail on a repeated top-level name. Deliberate
conditional definition (the `if TYPE_CHECKING` idiom, platform forks) is rare
enough here to deserve a comment and an explicit allowlist entry rather than
silence.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("btcpredict", "tools")

#: module -> names allowed to repeat, with the reason. Keep this EMPTY unless a
#: conditional definition is genuinely the clearest way to write it.
ALLOWED = {}


def _modules() -> list[Path]:
    out: list[Path] = []
    for pkg in PACKAGES:
        out.extend(sorted((ROOT / pkg).rglob("*.py")))
    return out


def _top_level_definitions(tree: ast.Module):
    """(name, lineno) for every def/class/assignment bound at module level."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name, node.lineno
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    yield tgt.id, node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            yield node.target.id, node.lineno


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_module_does_not_shadow_its_own_top_level_names(path: Path):
    tree = ast.parse(path.read_text(), filename=str(path))
    seen: dict[str, int] = {}
    clashes: list[str] = []
    for name, lineno in _top_level_definitions(tree):
        if name in seen:
            allowed = ALLOWED.get(path.relative_to(ROOT).as_posix(), ())
            if name not in allowed:
                clashes.append(f"{name} (line {seen[name]} then line {lineno})")
        seen[name] = lineno
    assert not clashes, (
        f"{path.relative_to(ROOT)} binds the same top-level name more than "
        f"once, so the earlier definition is dead and its docstring lies: "
        f"{clashes}"
    )
