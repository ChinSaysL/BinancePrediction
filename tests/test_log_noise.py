"""The log is an operations record, not a request trace.

WHY THIS FILE EXISTS. Measured over 3 hours of the live log, the largest sources
of lines were things that had ALREADY been said better somewhere else:

    220  pm order metadata: pre-resolved N token(s)   <- a SUCCESS line; the
                                                         `warm N` counter on the
                                                         liveness line carries it
    171  httpx GET /markets-by-token/N                 <- transport for that warm
    135  families: N/N cached, N live | ...            <- "no change", every 60s
     80  poll cadence over 120 ticks: median 40ms     <- healthy, every 2 min
     64  claim sweep -> {'redeemed': [], ...}          <- "nothing happened"
    ~300 the rest of httpx (positions, balance-allowance, clob-markets, ...)

Five shapes, roughly half the file, none of them a trading event -- and between
them they drown the ones that are (`CROSS`, `pm FAK FILL`, `cross exec`,
`settled`, and every WARNING).

RE-MEASURED 2026-09-22 over 5.6 days / 83,209 lines, which found a bigger one
that no census of 3 hours could see. The file was dominated not by chatty
successes but by TWO REPEATING CONDITIONS:

  14,801  17.8%  ERROR `live POST .../get-quote -> HTTP 400: -9000`
   ~15,000 18%   the startup banner, re-emitted on ~1,542 process starts
   3,075   3.7%  `settled <slug> -> Up`      (six per 5-minute roll)
   2,464   3.0%  `book ws connected: N (M live)`
   5,817   7.0%  `stats: rounds=... | no trades`

That is why the fix is not only levels. A condition that repeats is collapsed by
`_RepeatFilter` into one line per 5 minutes carrying its own count, and the
`families:` alarm above was firing on 100% of ticks. The lesson the 3-hour
census could not teach: THE LARGEST LINE ITEM IN AN OPS LOG IS USUALLY THE SAME
SENTENCE MANY TIMES, and its cost is not disk -- it is that `grep -i error`
returns 14,801 hits and the one error that is new is invisible.

The risk in removing noise is removing the ALARM with it, so each is pinned in
BOTH directions: silent when healthy, loud when it is not. `_quiet_libraries`
goes further and gives httpx back wholesale at `log_level=DEBUG`, because a
diagnostic mode that silently drops the transport is not a diagnostic mode.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import pathlib

import pytest

import btcpredict.main as m

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ROOT / "btcpredict" / "main.py"


class _Collect(logging.Handler):
    """Collect records WITHOUT touching the logger's own level.

    `caplog` and the `_capture` helper in test_pm_order_warm.py both call
    `setLevel(DEBUG)`, which is exactly what these tests are trying to prove has
    been turned OFF -- a helper that re-enables it would pass for the wrong
    reason.
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:    # noqa: E704
        self.records.append(record)


def _attach(name: str) -> tuple[_Collect, logging.Logger]:
    lg = logging.getLogger(name)
    h = _Collect()
    lg.addHandler(h)
    return h, lg


@pytest.fixture()
def httpx_logger():
    """Restore httpx's level: `_quiet_libraries` mutates global logger state."""
    lg = logging.getLogger("httpx")
    old = lg.level
    yield lg
    lg.setLevel(old)


class TestTheTransportIsSilenced:
    def test_a_request_line_no_longer_reaches_the_log(self, httpx_logger,
                                                      monkeypatch) -> None:
        monkeypatch.setattr(m.settings, "log_level", "INFO")
        m._quiet_libraries()
        assert httpx_logger.getEffectiveLevel() == logging.WARNING
        h, lg = _attach("httpx")
        try:
            lg.info('HTTP Request: GET https://clob.polymarket.com/order '
                    '"HTTP/2 200 OK"')
        finally:
            lg.removeHandler(h)
        assert h.records == [], [r.getMessage() for r in h.records]

    def test_a_transport_FAILURE_still_gets_through(self, httpx_logger,
                                                    monkeypatch) -> None:
        """The point is not "hide httpx", it is "stop logging success loudly"."""
        monkeypatch.setattr(m.settings, "log_level", "INFO")
        m._quiet_libraries()
        h, lg = _attach("httpx")
        try:
            lg.info('HTTP Request: POST https://clob.polymarket.com/order '
                    '"HTTP/2 400 Bad Request"')
            lg.warning("connection reset by peer")
        finally:
            lg.removeHandler(h)
        assert [r.getMessage() for r in h.records] == ["connection reset by peer"], \
            [r.getMessage() for r in h.records]

    def test_debug_mode_is_left_complete(self, httpx_logger, monkeypatch) -> None:
        """Nothing is suppressed at DEBUG: the diagnostic mode must not lie."""
        httpx_logger.setLevel(logging.NOTSET)
        monkeypatch.setattr(m.settings, "log_level", "DEBUG")
        m._quiet_libraries()
        assert httpx_logger.level == logging.NOTSET

    def test_amain_installs_it_after_basicConfig(self) -> None:
        """Attaching the level before the handler setup would be undone by it."""
        src = MAIN.read_text()
        seg = next(ast.get_source_segment(src, n)
                   for n in ast.walk(ast.parse(src))
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "amain")
        assert seg is not None
        assert "logging.basicConfig(" in seg
        assert "_quiet_libraries()" in seg
        assert seg.index("logging.basicConfig(") < seg.index("_quiet_libraries()")


class TestTheOnesThatSaidNothingHappened:
    def _sweep(self, result) -> list[logging.LogRecord]:
        class _Broker:
            mode = "live"

            async def redeem(self):                       # noqa: E704
                return result

        bot = m.Bot.__new__(m.Bot)
        bot.broker = _Broker()
        bot._last_claim_sweep = 0.0
        h, lg = _attach("btcpredict")
        old, lg.level = lg.level, logging.DEBUG
        try:
            asyncio.run(bot.sweep_claims())
        finally:
            lg.removeHandler(h)
            lg.level = old
        return h.records

    def test_an_empty_sweep_is_not_an_event(self) -> None:
        """`{'redeemed': []}` is a TRUTHY dict, which is why `if claimed:`
        printed it every 180s forever."""
        recs = self._sweep({"redeemed": [], "reason": "nothing_redeemable"})
        assert [r for r in recs if r.levelno >= logging.INFO] == [], \
            [r.getMessage() for r in recs]

    def test_a_real_redemption_is_still_reported(self) -> None:
        recs = self._sweep({"redeemed": [{"txHash": "0xabc"}]})
        info = [r for r in recs if r.levelno == logging.INFO]
        assert len(info) == 1 and "claim sweep" in info[0].getMessage(), \
            [r.getMessage() for r in recs]


def _flat(fn: str) -> str:
    """A method of `Bot` with whitespace collapsed, so a reflow cannot break a
    pin. The liveness line is in `tick`, the cadence line in `run`."""
    src = MAIN.read_text()
    seg = next((ast.get_source_segment(src, n) for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.AsyncFunctionDef) and n.name == fn), None)
    assert seg is not None, f"Bot.{fn} is gone: this guard would measure nothing"
    return " ".join(seg.split())


class TestTheAlarmsSurvive:
    def test_the_liveness_line_shouts_on_a_transition_not_every_minute(self) -> None:
        """It exists for the ABSENCE case -- a family with no quote, no book and
        no decision can be dead for hours while the log says only "no trades".

        IT USED TO FIRE ON EVERY TICK THAT WAS BAD, AND `active_markets()` LISTS
        SIX FAMILIES WHILE THIS DEPLOYMENT TRADES TWO, so `not live` was
        PERMANENTLY non-empty: the alarm was on 100% of the time. Measured
        2026-09-22 over 5.6 days of live.log: 960 identical WARNINGs, and a
        warning that is always on is not a warning -- it buried the 17.8% of the
        file that was one repeating ERROR.

        THE ALARM SURVIVES, because the condition it guards -- a family that is
        SUPPOSED to trade having gone dark -- arrives as a CHANGE. This pin
        holds both halves: the warning must still be reachable, and it must be
        gated on the state changing or on the 30-minute reminder.
        """
        body = _flat("tick")
        assert ("(miss or dead) and (flag != prev or now_s - last_warn "
                ">= 1800.0)") in body, body[-1400:]
        assert "log.warning(pat, *fargs)" in body, body[-1400:]
        assert "else: log.debug(pat, *fargs)" in body, body[-1400:]

    def test_the_cadence_line_shouts_only_when_a_tick_was_bad(self) -> None:
        """A widened websocket book is measured to take the cadence 1.00s ->
        4.15s, so the number has to still be able to reach the log."""
        body = _flat("run")
        assert "if q[-1] >= _SLOW_TICK_MS: log.warning(*cad) else: log.debug(*cad)" \
            in body, body[-1200:]


class TestTheRefusalMustNameWhatRefused:
    """The liquidity refusals logged on EVERY tick, and named the wrong thing.

    MEASURED 2026-09-18: 731 lines in one afternoon reading

        cross eth-updown-5m-... Up not taken: book holds only $0.00 (wanted
        $0.00)

    at ~20/second while a trigger was live. The cause was the SECOND number on
    the line: `stake_for_fraction` returns 0 below
    `min_stake / FLOOR_MAX_BANKROLL_FRACTION` ($4.00 for a $1 floor), so on a
    $2.19 bankroll the SIZING refused and the book was never asked -- a zero
    stake makes `sweep()` return (0.0, 0.0) whatever is resting. Two diagnoses
    were written off that line before anyone read the stake.
    """

    def test_the_stake_is_checked_before_the_book(self) -> None:
        body = _flat("_cross_tick")
        assert body.index("stake under the venue minimum") < \
            body.index("book holds under the venue minimum"), \
            "a zero stake sweeps to zero on ANY book: the book check must " \
            "not run first, or the line blames the wrong input"

    def test_no_liquidity_refusal_logs_on_every_tick(self) -> None:
        """`_cross_refuse` dedupes per (round, reason); a bare log.info does
        not, and the tick clock re-detects one crossing for `look_s`."""
        body = _flat("_cross_tick")
        for gone in ('log.info("cross %s %s not taken: book holds only',
                     'log.info("cross %s %s not taken: sweep price'):
            assert gone not in body, gone

    def test_the_numbers_ride_as_detail_which_prints_once(self) -> None:
        """The reason is the dedupe KEY, so the numbers cannot go in it -- and
        they are the only place the sizing arithmetic is visible in the log."""
        import inspect
        p = inspect.signature(m.Bot._cross_refuse).parameters
        assert "detail" in p and p["detail"].default == ""
        body = " ".join(inspect.getsource(m.Bot._cross_refuse).split())
        assert 'f"{reason} ({detail})" if detail else reason' in body, body[:400]

    def test_the_fraction_floor_is_where_the_arm_went_silent(self) -> None:
        """The arithmetic of the incident, pinned: a $1 floor with the floor
        capped at 0.25 of the bankroll means the arm cannot size at all under
        $4 -- whatever the gates or the book say."""
        from btcpredict.edge.signal import stake_for_fraction
        kw = dict(fraction=0.0224, min_stake=1.0, cap=1.0,
                  floor_to_minimum=True)
        assert stake_for_fraction(2.1983, **kw) == 0.0, "the live bankroll"
        assert stake_for_fraction(4.2606, **kw) == 1.0, "the ledger that day"
        assert stake_for_fraction(3.99, **kw) == 0.0
        assert stake_for_fraction(4.01, **kw) == 1.0

    def test_the_arms_flat_stake_is_the_CROSS_prefixed_knob(self) -> None:
        """`_ck("stake_usdt")` reads `cross_stake_usdt`, NOT the global
        `stake_usdt` (7.0 live, cut to $1 by the cap). Reading the global would
        pin the arm to a flat dollar size and hide the fraction rule that
        actually decides it -- the confusion that cost two wrong diagnoses."""
        import types

        from btcpredict.config import Settings, settings as s
        # The DEFAULT is None; the live `.env` sets 1.0. What this test is
        # about is which NAME `_ck` reads, so the default is the honest anchor.
        assert Settings.model_fields["cross_stake_usdt"].default is None
        b = m.Bot.__new__(m.Bot)
        b._round = types.SimpleNamespace(slug="btc-updown-5m-1")
        assert b._ck("stake_usdt") == s.cross_stake_usdt
        assert s.stake_usdt != s.cross_stake_usdt, "the two names are the trap"
