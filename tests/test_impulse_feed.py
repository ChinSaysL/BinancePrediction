"""The impulse fast path must read the TRADED asset's feed, not `self.spot`.

`_record` runs once per market in `MARKETS` and reassigns `self.spot` on each
call, so after the loop it holds the LAST symbol's feed. `_impulse_tick` runs on
the websocket clock, i.e. outside that call, so `self.spot` there was whatever
the poll left behind.

Measured live 2026-09-12, the day the ETH 15m market first appeared in the
venue's list (it is the last entry in MARKETS):

    13:02:01 Down spot=77309.025 strike=77340.005  OK
    13:33:27 Up   spot=77304.005 strike=77318.765  OK
    15:37:31 Up   spot= 2534.995 strike=77301.385  WRONG FEED (ETH vs BTC)
    15:47:31 Down spot= 2534.120 strike=77403.305  WRONG FEED
    15:56:56 Up   spot= 2536.400 strike=77386.605  WRONG FEED
    16:57:50 Down spot= 2530.835 strike=77344.005  WRONG FEED

Every impulse trade after the switch was triggered by the wrong asset. It also
produced the @0.01 limit that stalled the arm: fair_value(spot=ETH,strike=BTC)
is a -9672 bps move, so P(Up)~0 and take_limit_px computed max(0.01, 0-0.04).

The bug is invisible in behaviour -- no exception, the loop keeps running, the
arm just gets quiet -- so it is pinned structurally here as well as by the feed
selection itself.
"""
from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "btcpredict" / "main.py"


def _method_source(name: str) -> str:
    tree = ast.parse(SRC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(SRC.read_text(), node) or ""
    raise AssertionError(f"{name} not found in {SRC}")


def test_impulse_tick_does_not_read_the_shared_spot_attribute() -> None:
    """`self.spot` is per-market state; the fast path must not use it."""
    src = _method_source("_impulse_tick")
    # The bug was exactly `getattr(self, "spot", None)`. Comments in this method
    # legitimately mention self.spot, so match the code pattern, not the prose.
    assert 'getattr(self, "spot"' not in src, (
        "_impulse_tick reads self.spot, which _record rewrites once per market "
        "in MARKETS -- that is the wrong-asset trigger bug"
    )
    assert "_primary_spot" in src, (
        "_impulse_tick must take its feed from the primary-symbol attribute "
        "captured in _record"
    )


def test_record_captures_the_primary_feed() -> None:
    """`_primary_spot` is set where the traded round is bound."""
    src = _method_source("_record")
    assert "self._primary_spot = self.spot" in src, (
        "_record must capture the primary feed next to `self._round = r`"
    )


def test_the_primary_market_is_the_one_captured() -> None:
    """_primary_spot is assigned AFTER the trade check, so a recorded-only
    market can never become the feed the impulse arm trades on."""
    src = _method_source("_record")
    guard = src.index("if self.mode == \"record\" or not trade")
    capture = src.index("self._primary_spot = self.spot")
    assert capture > guard, (
        "_primary_spot must be captured after the `not trade` return, or an "
        "untraded family (ETH/BNB/15m) becomes the impulse arm's price feed"
    )
