"""Two brokers, one caller: the SIGNATURES must match, not just the names.

`main.py` does not ask a broker what it can take -- it dispatches on
`broker.mode` and `hasattr`. So a broker that mirrors the MODE and the METHOD
NAME but not the PARAMETERS passes every guard and then raises TypeError on the
first live order.

MEASURED 2026-09-17 04:44:17, exactly that:

    cross order failed for sol-updown-5m-1789620000 Down:
    TypeError: PmBroker.buy() got an unexpected keyword argument 'quote'

`main.py` sends `quote=` whenever `broker.mode == "live"` (LiveBroker uses it to
skip a round trip and to bound a MARKET order by the displayed price). PmBroker
sets `mode = "live"` deliberately, to be interchangeable -- and had no `quote`.
It had never fired because the cross arm used `buy_limit` (post-only) until the
FAK take path existed; the first order that reached `buy()` crashed.

`buy_limit`'s docstring already states the rule ("The parameter names are the
contract, not a suggestion"), and it is the one method that was written twice
with the same parameters. This pins it for the whole interface.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from btcpredict.execution.live import LiveBroker
from btcpredict.venues.polymarket.broker import PmBroker

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Every `await self.broker.<name>(...)` in main.py, and the kwargs it sends.
#:
#: THE SPLAT IS WHY THIS BUG SURVIVED REVIEW. `quote` never appears as a literal
#: `quote=` beside the call -- it is built one statement earlier as
#: `kw = {"quote": quote} if self.broker.mode == "live" else {}`, extended with
#: `kw["max_px"] = cap`, and splatted as `**kw`. So the scan resolves, for every
#: broker call that splats a name, the CONSTANT KEYS that name can carry: dict
#: literals assigned to it plus `name["key"] = ...` subscripts.
def _splatted_keys(tree: ast.Module, name: str) -> set[str]:
    keys: set[str] = set()
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            for sub in ast.walk(value):
                if isinstance(sub, ast.Dict):
                    keys |= {k.value for k in sub.keys
                             if isinstance(k, ast.Constant)
                             and isinstance(k.value, str)}
        for t in targets:
            if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                    and t.value.id == name
                    and isinstance(t.slice, ast.Constant)
                    and isinstance(t.slice.value, str)):
                keys.add(t.slice.value)
    return keys


def _call_kwargs() -> dict[str, set[str]]:
    src = (ROOT / "btcpredict" / "main.py").read_text()
    tree = ast.parse(src)
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute)
                and f.value.attr == "broker"):
            continue
        names = {k.arg for k in node.keywords if k.arg}
        for k in node.keywords:
            if k.arg is None and isinstance(k.value, ast.Name):
                names |= _splatted_keys(tree, k.value.id)
        out.setdefault(f.attr, set()).update(names)
    return out


def _params(func) -> list[str]:
    return [p for p in inspect.signature(func).parameters if p != "self"]


def _kwargs_only(func) -> set[str]:
    sig = inspect.signature(func)
    return {n for n, p in sig.parameters.items()
            if p.kind is p.KEYWORD_ONLY}


CALLS = _call_kwargs()


def test_the_call_sites_were_found():
    """If main.py stops calling through `self.broker`, this file tests nothing."""
    assert {"buy", "buy_limit"} <= set(CALLS), CALLS
    assert "quote" in CALLS["buy"], \
        "the live-mode quote= kwarg is what broke; it must be in the scan"


@pytest.mark.parametrize("name", sorted(CALLS))
def test_pm_broker_takes_every_kwarg_main_sends(name: str):
    """THE REGRESSION. `quote` was in CALLS['buy'] and not in PmBroker.buy."""
    func = getattr(PmBroker, name, None)
    if func is None:
        pytest.skip(f"PmBroker has no {name} (main.py guards with hasattr)")
    missing = CALLS[name] - _kwargs_only(func)
    assert not missing, (
        f"PmBroker.{name} would raise TypeError: unexpected keyword argument "
        f"{sorted(missing)} -- main.py sends {sorted(CALLS[name])}")


@pytest.mark.parametrize("name", ["buy", "buy_limit"])
def test_pm_and_live_agree_on_the_parameter_names(name: str):
    """Mirroring a mode without mirroring the signature is how the bug landed."""
    live = _params(getattr(LiveBroker, name))
    pm = _params(getattr(PmBroker, name))
    # LiveBroker may carry extras PmBroker has no use for (a warm `quote`);
    # what it may NOT do is omit something the caller passes.
    assert set(live) - {"self"} <= set(pm), (
        f"LiveBroker.{name} takes {live}, PmBroker.{name} takes {pm}")


def test_quote_is_still_in_the_live_call_site():
    """The bug was only reachable because main.py passes quote= on live mode.

    If that ever stops being true this test should be deleted rather than
    'fixed' -- but while it is true, PmBroker must keep accepting it.
    """
    assert "quote" in CALLS["buy"]
    assert "quote" in _params(PmBroker.buy)
    assert "quote" in _params(PmBroker.buy_fak)
