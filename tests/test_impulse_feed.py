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
    """`_primary_spot` is set where the traded round is bound.

    It now captures the LOCAL `feed`, not `self.spot`. That is strictly
    stronger: `self.spot` was shared state rewritten once per market, so
    capturing it depended on loop order; the local cannot be another family's.
    """
    src = _method_source("_record")
    assert "self._primary_spot = feed" in src, (
        "_record must capture the primary feed next to `self._round = r`"
    )


def test_record_never_reads_shared_spot_state_for_a_recorded_row() -> None:
    """The quotes row must describe the family being recorded, not whichever
    feed `self.spot` happens to hold. This caught a real bug when the arm was
    widened to six families: `spot_ts_ms` was still reading `self.spot.ts_ms`,
    which for a non-primary family is the PRIMARY's timestamp."""
    src = _method_source("_record")
    row = src[src.index('self.store.insert("quotes"'):]
    row = row[:row.index("})")]
    assert "self.spot" not in row, (
        "the quotes row reads shared spot state; it must use the local `feed`"
    )


def test_the_primary_market_is_the_one_captured() -> None:
    """The impulse arm's single-valued feed state is assigned ONLY for the
    primary, so no other family -- traded or merely recorded -- can become it.

    Tightened when the cross arm was widened to six families: the guard used to
    be `not trade`, which now admits eth/bnb/15m.

    Re-expressed 2026-09-19 when the MODEL arm widened to every family. The
    guard stopped being an early `if not primary: return` -- the method now
    continues past it to price the other coins -- so this pins the invariant
    that actually matters: every assignment to the impulse arm's cross-call
    state sits inside the `if primary:` block.
    """
    src = _method_source("_record")
    lines = src.splitlines()

    def block_after(i: int) -> str:
        indent = len(lines[i]) - len(lines[i].lstrip())
        out = []
        for ln in lines[i + 1:]:
            if ln.strip() and (len(ln) - len(ln.lstrip())) <= indent:
                break
            out.append(ln)
        return "\n".join(out)

    # `_record` has more than one `if primary:`; take the one that actually
    # carries the impulse arm's feed state.
    bodies = [block_after(i) for i, ln in enumerate(lines)
              if ln.strip() == "if primary:"]
    owning = [b for b in bodies if "self._primary_spot = feed" in b]
    assert len(owning) == 1, (
        "expected exactly one `if primary:` block to assign _primary_spot, "
        f"found {len(owning)}"
    )
    body = owning[0]

    SINGLE_VALUED = ("self._primary_spot", "self._round", "self._asks",
                     "self._sigma")
    for name in SINGLE_VALUED:
        assign = f"{name} = "
        assert assign in body, (
            f"{name} must be assigned inside `if primary:` -- outside it, an "
            f"untraded family (ETH/BNB/15m) becomes the impulse arm's state"
        )
        assert src.count(assign) == body.count(assign), (
            f"{name} is also assigned OUTSIDE the `if primary:` block; that is "
            f"how ETH's spot came to be compared against BTC's strike"
        )
    assert "await self._double_tick()" in body, (
        "the double arm must stay primary-only: it reads the same state"
    )


def test_the_model_arm_is_not_confined_to_the_primary() -> None:
    """The model arm must run for every family, one position per coin per round.

    The operator asked for this on 2026-09-19. Pinned because the obvious way to
    restore the old impulse guard -- an early `if not primary: return` -- would
    silently narrow the model arm back to BTC-5m with no test failing.
    """
    src = _method_source("_record")
    assert "if not primary:\n            return" not in src, (
        "an early primary-only return confines the model arm to BTC-5m again"
    )
    # The cap is per-SLUG, i.e. per family-round, not a global counter: that
    # is what makes "one trade per round for each coin" true.
    assert "self.guard.check(r.slug)" in src, (
        "the entry must gate on r.slug; a global counter would let one coin "
        "consume every slot"
    )

