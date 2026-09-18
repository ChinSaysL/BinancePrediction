"""The book subscription must cover every family the arms evaluate.

MEASURED FAILURE (2026-09-17): the socket was told to follow the rounds in
`_mkt`, and `_mkt` is filled BY `_record`, which needs the book this socket
provides. A family outside `_mkt` could therefore never be subscribed, never be
priced, and never enter `_mkt` -- `pm book ws connected (12 assets)` = 6
families, with sol/xrp/doge `not live` for hours and every trigger on them
refused as `no ask on the book`. An absence, so nothing logged an error.

`_round_cache` exists before any book does, so it is the only source that breaks
the loop. These tests pin the source, not the stringification.
"""

from __future__ import annotations

import ast
import pathlib

from btcpredict.main import ws_follow_rounds

MAIN = pathlib.Path(__file__).resolve().parents[1] / "btcpredict" / "main.py"
SRC = MAIN.read_text()


class _R:
    def __init__(self, slug: str) -> None:
        self.slug = slug

    def __repr__(self) -> str:                       # keeps failures readable
        return f"<R {self.slug}>"


def _mkt(*pfxs: str) -> dict:
    return {p: {"round": _R(p), "feed": None} for p in pfxs}


def _cache(*pfxs: str) -> dict:
    return {p: _R(p) for p in pfxs}


# -- the loop that was closed on itself ------------------------------------

def test_follow_all_uses_the_cache_not_the_priced_set():
    """The priced set is a SUBSET. Following it is the bug."""
    cache = _cache("btc-updown-5m", "eth-updown-5m", "sol-updown-5m",
                   "xrp-updown-5m", "doge-updown-5m")
    mkt = _mkt("btc-updown-5m", "eth-updown-5m")
    rs = ws_follow_rounds(cache, mkt, "btc-updown-5m", True)
    assert len(rs) == 5, "every cached family must be subscribed"
    assert {r.slug for r in rs} == set(cache)


def test_an_unpriced_family_is_subscribed():
    """sol/xrp/doge: cached, never priced, therefore never followed -- the bug."""
    cache = _cache("btc-updown-5m", "sol-updown-5m")
    mkt = _mkt("btc-updown-5m")
    rs = ws_follow_rounds(cache, mkt, "btc-updown-5m", True)
    assert "sol-updown-5m" in {r.slug for r in rs}


def test_no_duplicate_rounds():
    cache = _cache("btc-updown-5m", "eth-updown-5m")
    rs = ws_follow_rounds(cache, _mkt("btc-updown-5m"), "btc-updown-5m", True)
    assert [r.slug for r in rs] == [r.slug for r in rs][:len(rs)]
    assert len({id(r) for r in rs}) == len(rs)


# -- the older behaviour, kept for the switch ------------------------------

def test_follow_all_off_is_the_primary_only():
    cache = _cache("btc-updown-5m", "sol-updown-5m")
    rs = ws_follow_rounds(cache, _mkt("btc-updown-5m", "sol-updown-5m"),
                          "btc-updown-5m", False)
    assert [r.slug for r in rs] == ["btc-updown-5m"]


def test_follow_all_off_falls_back_to_the_primary_cache():
    rs = ws_follow_rounds(_cache("btc-updown-5m"), {}, "btc-updown-5m", False)
    assert [r.slug for r in rs] == ["btc-updown-5m"]


# -- degenerate inputs: `follow([])` means "leave the subscription alone" ---

def test_empty_cache_and_empty_mkt_yields_nothing():
    assert ws_follow_rounds({}, {}, "btc-updown-5m", True) == []


def test_none_rounds_are_dropped():
    cache = {"btc-updown-5m": _R("btc-updown-5m"), "eth-updown-5m": None}
    rs = ws_follow_rounds(cache, {}, "btc-updown-5m", True)
    assert [r.slug for r in rs] == ["btc-updown-5m"]


def test_empty_cache_still_follows_the_priced_primary():
    """A venue booting before the first sweep must not go bookless."""
    rs = ws_follow_rounds({}, _mkt("btc-updown-5m", "eth-updown-5m"),
                          "btc-updown-5m", False)
    assert [r.slug for r in rs] == ["btc-updown-5m"]


def test_a_cache_key_without_a_round_is_not_a_crash():
    assert ws_follow_rounds({"x": None}, {"y": {"round": None}},
                            "btc-updown-5m", True) == []


# -- source guards ---------------------------------------------------------

def _module() -> ast.Module:
    return ast.parse(SRC)


def _call_site() -> ast.Call:
    for node in ast.walk(_module()):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ws_follow_rounds"):
            return node
    raise AssertionError("ws_follow_rounds is never called")


def test_the_call_site_passes_the_round_cache_first():
    """Argument order is the contract: the cache, then `_mkt`."""
    a0, a1, a2 = _call_site().args[:3]
    assert isinstance(a0, ast.Name) and a0.id == "cache"
    assert "_mkt" in ast.dump(a1), "second argument is the priced set"
    assert isinstance(a2, ast.Name) and a2.id == "primary"


def test_the_old_mkt_only_list_is_gone():
    """`rs = [c["round"] for ... in ..._mkt` was the closed loop."""
    assert '_mkt", None) or {}).items()\n                  if c.get("round")' not in SRC
    # and the only remaining builder of the follow list is the helper
    assert SRC.count("wsb.follow(") == 1


def test_the_helper_reads_the_cache_before_mkt():
    """Reading order in the body: the cache branch must come first."""
    body = ast.get_source_segment(
        SRC, next(n for n in ast.walk(_module())
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "ws_follow_rounds"))
    assert body.index("round_cache") < body.index("mkt")
