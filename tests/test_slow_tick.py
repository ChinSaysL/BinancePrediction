"""The slow-tick instrument: a 10-second tick must NAME its phase.

WHY THIS FILE EXISTS. `poll cadence` has reported `median 34ms p90 50ms max
11964ms` -- one outlier tick in 120 -- in every window since the log began at
2026-09-16 20:15. Four explanations were eliminated by measurement before the
instrument went in (host CPU, a blocked event loop, `Store.counts()`, and the
~120 ms `venue lag` floor, which is delivery latency). What is left is one of
four phases in `Bot.run`, three of which touch the network, and guessing which
one is how a working config gets broken.

THE GUARD THAT MATTERS MOST is `test_idle_is_timed_out_of_the_tick`. `elapsed`
is measured BEFORE `await self._idle(...)`, so the number excludes the deliberate
wait on the feed or the clock. Move that measurement one line down and every
healthy tick starts reporting ~1 s (the poll interval) instead of ~43 ms, and
the instrument becomes a lie that reads like an outage.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from btcpredict.main import _SLOW_TICK_MS, _phase_breakdown

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ROOT / "btcpredict" / "main.py"


def _run_source() -> str:
    for n in ast.walk(ast.parse(MAIN.read_text())):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "run":
            return ast.unparse(n)
    pytest.fail("Bot.run is gone: this guard would be measuring nothing")


class TestTheBreakdown:
    def test_each_phase_is_the_gap_to_the_next_mark(self) -> None:
        # t0=0; tick done at 0.04; settle done at 9.04; tick ends at 9.05.
        s = _phase_breakdown(0.0, [("tick", 0.04), ("settle", 9.04)], 9.05)
        assert s == "tick 40ms | settle 9000ms | after 10ms"

    def test_the_parts_sum_to_the_tick(self) -> None:
        """Exclusive phases: no double-counting, no missing time."""
        t0, marks, end = 100.0, [("tick", 100.5), ("backfill", 100.6)], 101.0
        got = [int(p.split()[1].rstrip("ms")) for p in
               _phase_breakdown(t0, marks, end).split(" | ")]
        assert sum(got) == 1000

    def test_a_tick_that_completed_no_phase_says_so(self) -> None:
        """A hang inside `tick()` must not be blamed on a later phase."""
        s = _phase_breakdown(0.0, [], 9.5)
        assert "no phase completed" in s and "9.50s" in s
        assert "after" not in s

    def test_a_phase_that_returns_instantly_is_still_named(self) -> None:
        """The point is the SHAPE of the tick: 43 ms of work then 9 s of one."""
        s = _phase_breakdown(0.0, [("tick", 0.043), ("settle", 9.043),
                                   ("claims", 9.044)], 9.05)
        assert "settle 9000ms" in s and "claims 1ms" in s


class TestTheWiring:
    def test_run_logs_the_breakdown_with_the_threshold(self) -> None:
        src = _run_source()
        assert "_phase_breakdown(t0, ph, time.time())" in src
        assert "_SLOW_TICK_MS" in src

    def test_every_phase_is_marked(self) -> None:
        """A phase with no mark is invisible exactly when it is the slow one."""
        src = _run_source()
        for name in ("tick", "backfill", "housekeep"):
            # `ast.unparse` renders string literals with single quotes.
            assert "ph.append(('%s', time.time()))" % name in src, name

    def test_idle_is_timed_out_of_the_tick(self) -> None:
        """`elapsed` excludes the deliberate wait; see this file's docstring."""
        src = _run_source()
        measured = src.index("elapsed = time.time() - t0")
        idle = src.index("await self._idle(")
        assert measured < idle, (
            "`elapsed` is now measured after `_idle`, so every healthy tick "
            "will report the poll interval as a stall")

    def test_the_threshold_cannot_fire_on_a_clean_window(self) -> None:
        """A clean window's worst tick is ~384 ms; 2 s is ~5x that."""
        assert _SLOW_TICK_MS > 1_000.0


class TestTheBackfillIsOffTheTick:
    """The instrument's first fire named `backfill` as 8084 ms of an 8.39 s tick.

    `backfill_settlements` walks up to 5 rounds and each is a SEQUENTIAL REST
    round trip, so awaiting it inside the tick cost the loop 8.4 s of ticks --
    and `_idle`, which is what evaluates the cross arm, then got
    `max(0.05, 1.0 - 8.39)` = 50 ms instead of ~960 ms. The feed keeps ticking
    throughout, so nothing is lost on the socket; what is lost is the chance to
    ACT. Note the 8 s is the WORST case and not the mean: gamma is edge-cached
    at 8.7 ms, and the same call has since come back inside 2 s.
    """

    def test_run_dispatches_it_instead_of_awaiting_it(self) -> None:
        src = _run_source()
        assert "await self.backfill_settlements()" not in src, (
            "the await is back: every 15th tick will starve the cross arm for "
            "however long the venue takes to answer five times")
        assert "asyncio.create_task(self._backfill_async())" in src

    def test_it_is_still_bounded_to_one_in_flight(self) -> None:
        """Overlapping backfills would be a worse neighbour than the stall."""
        src = _run_source()
        assert "bf is None or bf.done()" in src
        assert "n % 15 == 0" in src

    def test_it_is_cancelled_on_shutdown(self) -> None:
        """Not awaited on the way in, so it must be cancelled on the way out."""
        src = _run_source()
        assert "_t.cancel()" in src
        assert "(bf, hk)" in src, "the backfill task dropped out of the sweep"

    def test_an_exception_does_not_kill_the_task_silently(self) -> None:
        """`live_round()` never returns a settled round, so this call is the
        ONLY path that attaches an outcome. A task that dies quietly leaves
        every later round unlabelled and unscoreable."""
        import asyncio
        import logging

        from btcpredict.main import Bot

        class Fake(Bot):
            def __init__(self):
                pass

            async def backfill_settlements(self, limit: int = 5) -> int:
                raise RuntimeError("venue said no")

        caught: list[str] = []

        class H(logging.Handler):
            def emit(self, rec):
                caught.append(rec.getMessage())

        h = H()
        lg = logging.getLogger("btcpredict")
        lg.addHandler(h)
        try:
            asyncio.run(Fake()._backfill_async())   # must not raise
        finally:
            lg.removeHandler(h)
        assert any("settlement backfill failed" in m for m in caught), caught


def _func_source(name: str) -> str:
    """RAW source of a method, not `ast.unparse` -- the await-free guard below
    is about what is WRITTEN, and unparse would happily normalise it away."""
    tree = ast.parse(MAIN.read_text())
    for n in ast.walk(tree):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            lines = MAIN.read_text().splitlines()
            return "\n".join(lines[n.lineno - 1:n.end_lineno])
    pytest.fail("%s is gone: this guard would be measuring nothing" % name)


class TestHousekeepingIsOffTheTick:
    """The instrument convicted the other three phases too.

    Across 121 slow ticks: `claims` 472.3s (p90 8939ms, max 31822ms), `settle`
    171.0s (max 22859ms), `bankroll` 11.0s. 643 seconds inside bookkeeping that
    decides no trade -- and the cost is not only the cross arm's sampling
    window. `_cross_exit_check` runs from the tick as well, so every one of
    those seconds was also a second in which a losing position could not be
    cut. The worst single tick, 31.8 s, is 13% of a 300 s round.
    """

    def test_run_dispatches_them_instead_of_awaiting_them(self) -> None:
        src = _run_source()
        for call in ("self.settle_due()", "self.sweep_claims()",
                     "self.refresh_bankroll_if_stale()"):
            assert "await " + call not in src, (
                "%s is awaited in the tick again: the loop stops placing AND "
                "stops exiting for as long as the venue takes" % call)
        assert "asyncio.create_task(self._housekeeping_async())" in src

    def test_it_is_single_flight(self) -> None:
        """Two `settle_due`s on the same `open_fills` would DOUBLE-BOOK a
        settlement -- the ledger, the guard pool and the P&L all move in it.
        At a 1 s poll against the measured 22.9 s worst case, dropping this
        guard would put twenty-two of them in flight at once."""
        src = _run_source()
        assert "hk is None or hk.done()" in src

    def test_record_mode_still_does_no_housekeeping(self) -> None:
        src = _run_source()
        assert "self.mode != 'record' and (hk is None or hk.done())" in src

    def test_the_order_is_load_bearing_and_preserved(self) -> None:
        """`sweep_claims` is the RETRY CLOCK for the redeem `settle_due` fires,
        so it must run after it or it duplicates a claim already in flight
        ("already have a pending claim", 3x on 2026-09-14). The bankroll read
        is last because the settlement is what moved the number."""
        src = _func_source("_housekeeping_async")
        i_settle = src.index("await self.settle_due()")
        i_claims = src.index("await self.sweep_claims()")
        i_bank = src.index("await self.refresh_bankroll_if_stale()")
        assert i_settle < i_claims < i_bank

    def test_a_settlement_is_booked_with_no_await_inside_it(self) -> None:
        """⭐ THIS IS WHAT MAKES DISPATCHING A MONEY PATH SAFE.

        `_housekeeping_async` is cancelled on shutdown, so a cancel can land at
        any await in `settle_due`. The booking itself -- DB insert, guard
        release, P&L, ledger, `del open_fills` -- must therefore be one
        CONTIGUOUS SYNCHRONOUS run, so a cancel either books all of it or none
        of it. Put an await in that block and a shutdown at the wrong moment
        can release a slot without crediting the ledger, or credit it twice on
        the retry.
        """
        src = _func_source("settle_due")
        start = src.index('self.store.insert("settlements"')
        end = src.rindex("del self.open_fills[fid]")
        assert "await" not in src[start:end], (
            "an await appeared inside the settlement booking block: a cancel "
            "there can now half-book a settlement")

    def test_cancellation_is_re_raised_not_swallowed(self) -> None:
        """A cancel that the blanket `except Exception` ate would hang the
        shutdown and log a settlement failure that never happened."""
        src = _func_source("_housekeeping_async")
        i_cancel = src.index("except asyncio.CancelledError:")
        i_broad = src.index("except Exception as e:")
        assert i_cancel < i_broad, "CancelledError must be caught FIRST"
        assert "raise" in src[i_cancel:i_broad]

    def test_an_exception_does_not_kill_the_task_silently(self) -> None:
        """Worse here than for the backfill: a task that dies quietly stops
        settling positions, so the guard pool never frees and the ledger never
        moves -- the bot would go silent rather than wrong, which is harder to
        notice."""
        import asyncio
        import logging

        from btcpredict.main import Bot

        class Fake(Bot):
            def __init__(self):
                self.swept = False

            async def settle_due(self) -> None:
                raise RuntimeError("venue said no")

            async def sweep_claims(self) -> None:
                self.swept = True

            async def refresh_bankroll_if_stale(self) -> None:
                pass

        caught: list[str] = []

        class H(logging.Handler):
            def emit(self, rec):
                caught.append(rec.getMessage())

        h = H()
        lg = logging.getLogger("btcpredict")
        lg.addHandler(h)
        bot = Fake()
        try:
            asyncio.run(bot._housekeeping_async())   # must not raise
        finally:
            lg.removeHandler(h)
        assert any("housekeeping failed" in m for m in caught), caught

    def test_a_slow_pass_is_reported_against_the_same_threshold(self) -> None:
        """The two numbers have to be comparable in the log, and the message
        has to say OFF the tick -- that is the whole difference."""
        src = _func_source("_housekeeping_async")
        assert "_SLOW_TICK_MS" in src
        assert "OFF the tick" in src
