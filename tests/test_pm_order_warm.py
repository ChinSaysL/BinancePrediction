"""Warm the order metadata BEFORE the order needs it.

MEASURED 2026-09-17, live: a FAK took 918-947 ms and the httpx log beside it
shows two GETs the SDK does ahead of its POST:

    GET /markets-by-token/<tok>    253 ms
    GET /clob-markets/<cond>       238 ms
    POST /order                    440 ms

Those are `SecureClient`'s order-metadata cache resolving the market for a
token. The cache is real -- `_conditions` never expires, `_markets` lives 600 s
-- but it is keyed by TOKEN/CONDITION and a token IS a round side, so every
order was a cold lookup. Against a book that moves 1-4c in that window, five of
six live FAKs came back "no orders found to match", one missing by a cent.

Warming means calling what the ORDER path calls:
`ctx.order_metadata.resolve_market(ctx, token_id=...)`, the same entry point
`place_market_order` uses (`_internal/actions/orders/market.py:173`) -- so the
order reads the cache entry we filled. (Loading a market also back-fills the
condition id for BOTH tokens of the round.)

A first version of this warmed `SecureClient.get_tick_size` / `.get_neg_risk`,
methods that exist on `py_clob_client`, NOT on the client we actually build.
Live it raised `AttributeError` and warmed nothing for 13 minutes -- so the
tests below pin the SHAPE of the call, not just that a warm was scheduled.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import types

import pytest

from btcpredict.main import _log_warm_done, _queue_warm_tokens
from btcpredict.venues.polymarket.broker import PmBroker, PmBrokerConfig

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = (ROOT / "btcpredict" / "main.py").read_text()
BROKER = (ROOT / "btcpredict" / "venues" / "polymarket" / "broker.py").read_text()


class FakeCache:
    """Mirrors `SyncOrderMetadataCache`: one call resolves a token."""

    def __init__(self, boom: bool = False) -> None:
        self.calls: list[str] = []
        self.boom = boom

    def resolve_market(self, ctx, *, token_id: str):
        if self.boom:
            raise RuntimeError("no market for that token")
        self.calls.append(token_id)
        return object()


class FakeCtx:
    def __init__(self, cache) -> None:
        self.order_metadata = cache


class FakeClient:
    """Only the shape `warm` reads: `_ctx_inner.order_metadata`."""

    def __init__(self, boom: bool = False, cache: bool = True) -> None:
        self.cache = FakeCache(boom=boom)
        self._ctx_inner = FakeCtx(self.cache) if cache else None


def make_broker(client, clock=None) -> PmBroker:
    return PmBroker(book=None, client=client, cfg=PmBrokerConfig(),
                    log=lambda _m: None, creds={}, clock=clock)


def test_warm_resolves_each_token_once():
    c = FakeClient()
    assert make_broker(c).warm(["a", "b"]) == 2
    assert c.cache.calls == ["a", "b"]


def test_the_second_warm_is_free():
    """The whole point: the poll keeps calling this, the venue is asked once."""
    c = FakeClient()
    br = make_broker(c)
    br.warm(["a"])
    assert br.warm(["a"]) == 0
    assert br.warm(["a", "b"]) == 1          # only the NEW token costs
    assert c.cache.calls == ["a", "b"]


def test_the_memo_expires_so_a_late_order_is_still_warm():
    """The SDK's market cache lives 600 s; a 15m round outlives it.

    A memo that never expired would mark a token warm forever and let the last
    order of a long round pay the lookups again -- the defect, re-armed.
    """
    now = [1000.0]
    c = FakeClient()
    br = make_broker(c, clock=lambda: now[0])
    br.warm(["a"])
    now[0] += br.WARM_TTL_S - 1
    assert br.warm(["a"]) == 0               # still warm
    now[0] += 2
    assert br.warm(["a"]) == 1               # re-warmed, not forgotten


def test_a_warm_failure_never_raises_and_is_not_retried_every_poll():
    """A broken lookup must leave the order path exactly as it was."""
    c = FakeClient(boom=True)
    br = make_broker(c)
    assert br.warm(["a"]) == 0               # no exception escapes
    assert br.warm(["a"]) == 0               # and it is not retried forever


def test_junk_tokens_are_ignored():
    c = FakeClient()
    assert make_broker(c).warm([None, "", "a"]) == 1
    assert c.cache.calls == ["a"]


def test_a_client_without_the_cache_is_survivable_and_says_so_once():
    """If the SDK reshapes, we must find out from the LOG, not by silence."""
    c = FakeClient(cache=False)
    lines: list[str] = []
    br = PmBroker(book=None, client=c, cfg=PmBrokerConfig(), log=lines.append,
                  creds={})
    assert br.warm(["a"]) == 0               # no exception
    assert br.warm(["a"]) == 0               # ...and not a log line per poll
    assert sum("pm warm unavailable" in l for l in lines) == 1


def test_the_client_is_built_lazily_and_a_build_failure_is_survivable(
        monkeypatch):
    c = FakeClient()
    br = make_broker(None)

    def boom():
        raise RuntimeError("no credentials")
    monkeypatch.setattr(br, "_ensure_client", boom)
    assert br.warm(["a"]) == 0               # must not raise into the poll

    monkeypatch.setattr(br, "_ensure_client", lambda: c)
    assert br.warm(["a"]) == 1


# -- the call must be the SDK's, not a guess ------------------------------

def test_warm_goes_through_the_order_metadata_cache():
    """Regression guard for the live AttributeError.

    The first version called `get_tick_size` / `get_neg_risk` on the client --
    `py_clob_client`'s API, not `SecureClient`'s. `warm` must reach the cache
    the ORDER reads.

    The cache call lives in `_warm_body` since 2026-09-17: `warm` itself is now
    a lock wrapper (see the order-path tests below), and a source scan that
    kept reading `warm` would be measuring the wrapper.
    """
    warm = next(n for n in ast.walk(ast.parse(BROKER))
                if isinstance(n, ast.FunctionDef) and n.name == "warm")
    body = ast.get_source_segment(BROKER, warm) or ""
    assert "self._client_lock" in body        # serialised against the order
    assert "self._warm_body" in body
    inner = next(n for n in ast.walk(ast.parse(BROKER))
                 if isinstance(n, ast.FunctionDef) and n.name == "_warm_body")
    ibody = ast.get_source_segment(BROKER, inner) or ""
    assert 'getattr(c, "_ctx_inner", None)' in ibody
    assert 'getattr(ctx, "order_metadata", None)' in ibody
    assert "resolve_market(ctx, token_id=" in ibody
    # ...and NOT the client methods that do not exist on SecureClient
    assert "c.get_tick_size" not in ibody
    assert "c.get_neg_risk" not in ibody


def test_main_warms_the_same_token_ids_the_order_will_use():
    """`r.token("Up")` does not exist on a PM round; the AttributeError was
    swallowed and the warm never ran live. Warm `up.token_id`/`down.token_id`,
    the expression the order path itself uses."""
    tree = ast.parse(MAIN)
    rec = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
               and n.name == "_record")
    body = ast.get_source_segment(MAIN, rec) or ""
    assert "up_r.token_id" in body and "down_r.token_id" in body
    # no round.token(...) CALL (the comment above may mention the old one)
    assert not [c for c in ast.walk(rec) if isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute) and c.func.attr == "token"]
    # a failure must be loud (once) rather than silently skipped
    assert "_warm_gripe" in body


def test_main_schedules_the_warm_off_the_critical_path():
    """It must run in a THREAD: the clob client is blocking, and a stall on the
    decision loop is what makes crossings invisible."""
    src = "".join(l for l in MAIN.splitlines(True)
                  if "to_thread" in l or "br.warm" in l)
    assert "asyncio.to_thread(br.warm" in src, src
    rec = next(n for n in ast.walk(ast.parse(MAIN))
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "_record")
    body = ast.get_source_segment(MAIN, rec) or ""
    assert "br.warm" in body
    assert "to_thread" in body
    assert "except Exception" in body
    # Never more than one warm in flight -- now enforced by the QUEUE, which is
    # where the starvation of the other eight families was removed (see the
    # `_queue_warm_tokens` tests at the bottom of this file).
    q = next(n for n in ast.walk(ast.parse(MAIN))
             if isinstance(n, ast.FunctionDef)
             and n.name == "_queue_warm_tokens")
    qbody = ast.get_source_segment(MAIN, q) or ""
    assert "not task.done()" in qbody
    assert "pend.update(toks" in qbody


def test_the_sdk_really_exposes_the_cache_we_warm():
    """The contract this depends on, asserted against the installed SDK.

    Skipped where `polymarket` is absent (it is the VPS that trades) -- but it
    runs on the box, which is where the wrong-class bug would have died.
    """
    pytest.importorskip("polymarket")
    from typing import get_type_hints

    from polymarket._internal.actions.orders.cache import (
        SyncOrderMetadataCache,
    )
    from polymarket._internal.context import SyncSecureClientContext

    hints = get_type_hints(SyncSecureClientContext)
    assert hints["order_metadata"] is SyncOrderMetadataCache
    assert hasattr(SyncOrderMetadataCache, "resolve_market")
    # the parameter NAME we pass: a rename is silent at runtime
    sig = inspect.signature(SyncOrderMetadataCache.resolve_market)
    assert "token_id" in sig.parameters


def test_the_market_cache_ttl_is_above_our_re_warm_ttl():
    """Our memo must expire while the SDK's entry is still valid, otherwise a
    token we call warm can be cold inside the SDK when the order is sent."""
    pytest.importorskip("polymarket")
    from polymarket._internal.actions.orders import cache as sdk_cache

    assert PmBroker.WARM_TTL_S < sdk_cache._METADATA_TTL_S


def test_placeholder_tokens_are_not_fetched():
    """MEASURED live: a round carries an all-zero token before its book
    resolves the side, and those 404 -- 90 in one minute after a restart. Each
    is a ~150 ms GET that can never help."""
    c = FakeClient()
    assert make_broker(c).warm(["0" * 77, "a"]) == 1
    assert c.cache.calls == ["a"]


# -- the log must never be the thing that breaks --------------------------

def test_a_logging_logger_does_not_break_the_warm():
    """Regression guard for a LIVE defect found while verifying this warm.

    `main.py` passes `log = logging.getLogger("btcpredict")`, and a Logger is
    NOT callable -- so `self._log("msg")` raised `TypeError: 'Logger' object is
    not callable` for every one of this module's error lines. Inside the warm
    task the TypeError vanished, which is why 90 failed warms in a minute left
    NOTHING in the log.
    """
    import logging

    seen: list[str] = []

    class Handler(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    lg = logging.getLogger("btcpredict.test.warm")
    lg.addHandler(Handler())
    lg.setLevel(logging.DEBUG)
    br = PmBroker(book=None, client=FakeClient(cache=False),
                  cfg=PmBrokerConfig(), log=lg, creds={})
    assert br.warm(["a"]) == 0               # must not raise
    assert any("pm warm unavailable" in m for m in seen), seen


def test_a_warm_failure_reaches_the_logger():
    """The failure path must be VISIBLE, not merely survivable."""
    import logging

    seen: list[str] = []

    class Handler(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    lg = logging.getLogger("btcpredict.test.warmfail")
    lg.addHandler(Handler())
    lg.setLevel(logging.DEBUG)
    br = PmBroker(book=None, client=FakeClient(boom=True),
                  cfg=PmBrokerConfig(), log=lg, creds={})
    assert br.warm(["a"]) == 0
    assert any("pm warm failed" in m for m in seen), seen


def test_main_reports_a_failed_warm_task():
    """Nothing awaits the warm task: an exception inside it must be reported
    by the task itself, or it is lost for the life of the process."""
    assert "_report_task_failure" in MAIN
    assert "add_done_callback(_report_task_failure)" in MAIN
    # and the reporter actually looks at the exception
    fn = next(n for n in ast.walk(ast.parse(MAIN))
              if isinstance(n, ast.FunctionDef)
              and n.name == "_report_task_failure")
    body = ast.get_source_segment(MAIN, fn) or ""
    assert "cancelled()" in body and "exception()" in body


def test_the_liveness_line_shows_the_warm_counter():
    """Success is silent in `warm`, so the warm's presence must be a NUMBER
    somewhere -- otherwise it is an inference, and the dead first version
    looked exactly like a working one."""
    assert "warm_calls" in MAIN
    assert '"not live: %s | warm %s"' in MAIN


# ---------------------------------------------------------------------------
# `warm_now`: the warm that does NOT trust its own memo.
#
# MEASURED 2026-09-17, counting the httpx lines between `take via FAK` and the
# FAK ack line, over 25 live order attempts:
#     20 of 25 paid BOTH metadata GETs -> ack median 947 ms (897-1401)
#      5 of 25 paid NEITHER            -> ack median 458 ms (415-498)
# The 489 ms difference IS the two GETs (253 + 238). The TTL-gated `warm` above
# is therefore NOT sufficient on its own: it gates on its own `_warm` dict and
# returns "already warm" without touching the SDK, and its counter climbs in
# steps of two and plateaus -- one round per tick off a single-slot task.
# ---------------------------------------------------------------------------

TOKEN = ("6709166685372110844071221033750586213017108312913575577287850497"
         "737155")


def test_warm_now_resolves_the_token_the_order_will_use():
    c = FakeClient()
    ms = make_broker(c).warm_now(TOKEN)
    assert c.cache.calls == [TOKEN]
    assert ms >= 0.0


def test_warm_now_does_not_trust_the_memo():
    """THE WHOLE POINT. `warm` skips a token it has seen inside WARM_TTL_S even
    when the SDK's entry is gone; `warm_now` must always ask the SDK."""
    c = FakeClient()
    b = make_broker(c)
    b._warm[TOKEN] = 1e12           # "warmed forever" in the memo
    assert b.warm([TOKEN]) == 0     # the gated one declines to act
    b.warm_now(TOKEN)
    assert c.cache.calls == [TOKEN]  # this one acted anyway


def test_warm_now_ignores_a_placeholder_token():
    """Those 404 live; they must never become a GET on the order path."""
    c = FakeClient()
    assert make_broker(c).warm_now("0" * 40) == 0.0
    assert c.cache.calls == []


def test_warm_now_never_raises():
    """It sits on the fire path: a raise here would cancel a live order."""
    assert make_broker(FakeClient(boom=True)).warm_now(TOKEN) == 0.0


def test_warm_now_survives_a_client_without_the_cache():
    assert make_broker(FakeClient(cache=False)).warm_now(TOKEN) == 0.0


def test_buy_fak_pays_the_warm_before_the_post():
    """Paid after the POST it would be the same 489 ms, counted as the order's.

    Anchor on the CALLS, not on the bare name: the comment above the call
    explains itself using the words `place_market_order`, so a name-only search
    would find the prose and read the order backwards.

    THE BODY MOVED on 2026-09-17 (`_buy_fak_body`), and the ORDER of these two
    calls is still the whole point -- so the assertion follows the body rather
    than the name.
    """
    fn = next(n for n in ast.walk(ast.parse(BROKER))
              if isinstance(n, ast.FunctionDef) and n.name == "_buy_fak_body")
    body = ast.get_source_segment(BROKER, fn) or ""
    assert "self.warm_now" in body
    assert "c.place_market_order" in body
    assert body.index("self.warm_now") < body.index("c.place_market_order")
    assert "except Exception" in body      # a warm failure cannot cancel it


def test_buy_fak_runs_off_the_event_loop():
    """The loop must stay free while the POST is in flight.

    MEASURED live 2026-09-17 20:49: the FAK ack landed at 24,984 and the venue
    websocket drained 399 frames 9 ms later -- the consumer is an asyncio task
    on the loop this method used to block, so it could not drain during the
    order, and the next decision was priced off the book that arrived after it.
    The source scan below pins the mechanism; `test_the_loop_keeps_running...`
    in test_pm_fak.py proves it by measuring.
    """
    outer = next(n for n in ast.walk(ast.parse(BROKER))
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "buy_fak")
    obody = ast.get_source_segment(BROKER, outer) or ""
    assert "asyncio.to_thread" in obody
    assert "self._buy_fak_sync" in obody
    # ...and the blocking half touches the SDK only under the shared lock.
    inner = next(n for n in ast.walk(ast.parse(BROKER))
                 if isinstance(n, ast.FunctionDef) and n.name == "_buy_fak_sync")
    ibody = ast.get_source_segment(BROKER, inner) or ""
    assert "self._client_lock" in ibody
    # the warm is paid INSIDE the locked worker, not on a second thread that
    # would queue behind the very lock this one holds
    assert "to_thread" not in ibody


# ---------------------------------------------------------------------------
# The ORDER-METADATA CACHE had to be full, not merely present.
#
# `warm` itself was correct; the DISPATCH to it was not. MEASURED live
# 2026-09-17: the `warm` counter moves +2 per 5-minute rollover against 18
# tokens a round -- (18, 18, 18, 18, 20, 22, 22, 22, 22, 24, 26) across the
# 10:45-10:55 window. Two tokens a round is not a rate, it is ONE family's
# round, and the family it is belongs to `MARKETS[0]`.
#
# Counting the httpx lines between `take via FAK` and the FAK ack over 16 live
# order attempts:
#     13 paid BOTH metadata GETs -> decision->ack median  963 ms
#      3 paid NEITHER            -> decision->ack median  442 ms
# The 520 ms difference is `GET /markets-by-token` + `GET /clob-markets`, and
# at 963 ms the venue's price is gone: the shadow probe read the REAL venue
# book 0.06-0.28 cents above our own at every fire, so the FAK answers
# `no orders found to match` and the fills that do land are the ones where the
# book did not move -- adverse selection, which is the whole fill record.
# ---------------------------------------------------------------------------


class _Task:
    """Only the shape the queue reads: `done()`."""

    def __init__(self, done: bool = False) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


def _owner(task=None):
    o = types.SimpleNamespace()
    if task is not None:
        o._warm_task = task
    return o


def test_a_family_that_requeues_every_sweep_cannot_starve_the_others():
    """THE LIVE DEFECT, AS A TEST.

    `_record` runs for every family in `MARKETS` order and `MARKETS[0]` is the
    primary, so the primary re-dispatched on every sweep while the one shared
    task was in flight -- and every other family was skipped for the life of
    the process. Eight of nine families never had their order metadata warmed,
    which is why the fired token was cold: 13 of 16 live FAKs paid the two GETs.
    """
    o = _owner(_Task(done=False))
    for _ in range(10):                      # the primary, on every sweep
        assert _queue_warm_tokens(o, ["btcU", "btcD"]) == []
    for i in range(8):                       # the other families, same window
        assert _queue_warm_tokens(o, [f"u{i}", f"d{i}"]) == []
    o._warm_task._done = True                # the in-flight warm lands
    batch = _queue_warm_tokens(o, [])
    assert len(batch) == 18, batch           # ALL NINE ROUNDS, not the primary
    assert "btcU" in batch and "d7" in batch


def test_the_first_warm_dispatches_immediately():
    assert _queue_warm_tokens(_owner(), ["a", "b"]) == ["a", "b"]


def test_a_finished_task_frees_the_queue():
    assert _queue_warm_tokens(_owner(_Task(done=True)), ["a"]) == ["a"]


def test_the_batch_is_emptied_so_a_token_is_not_warmed_twice():
    o = _owner()
    assert _queue_warm_tokens(o, ["a"]) == ["a"]
    assert _queue_warm_tokens(o, []) == []   # nothing carried over


def test_the_queue_survives_junk_and_an_empty_sweep():
    """`toks` is `[]` whenever a family has no round or reads a placeholder."""
    o = _owner()
    assert _queue_warm_tokens(o, []) == []
    assert _queue_warm_tokens(o, None) == []
    assert _queue_warm_tokens(o, ["a"]) == ["a"]


def test_main_dispatches_the_whole_batch_not_one_family():
    """The queue is only a fix if `_record` sends what it returns."""
    rec = next(n for n in ast.walk(ast.parse(MAIN))
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "_record")
    body = ast.get_source_segment(MAIN, rec) or ""
    assert "_queue_warm_tokens(self, toks)" in body
    assert "asyncio.to_thread(br.warm, batch)" in body
    # the starved shape must not come back
    assert "asyncio.to_thread(br.warm, toks)" not in body
    assert "if toks and (task is None or task.done()):" not in body


# -- the log must state the coverage, not imply it -------------------------

class _DoneTask:
    def __init__(self, result=None, exc=None, cancelled=False) -> None:
        self._result, self._exc, self._cancelled = result, exc, cancelled

    def cancelled(self): return self._cancelled          # noqa: E704
    def exception(self): return self._exc                # noqa: E704
    def result(self): return self._result                # noqa: E704


def _capture():
    import logging

    seen: list[str] = []

    class Handler(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    lg = logging.getLogger("btcpredict")
    h = Handler()
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    return seen, h, lg


def test_the_log_states_how_many_tokens_were_pre_resolved():
    """`warm` is silent on success and the counter climbs in ones and twos, so
    without this line "coverage" is an inference -- and the dead first version
    of this warm looked exactly like a working one."""
    seen, h, lg = _capture()
    try:
        _log_warm_done(_DoneTask(result=18))
    finally:
        lg.removeHandler(h)
    assert any("pre-resolved 18 token(s)" in m for m in seen), seen


def test_the_warm_done_line_is_silent_when_nothing_was_resolved():
    """0 is the steady state and the poll calls this every sweep."""
    seen, h, lg = _capture()
    try:
        _log_warm_done(_DoneTask(result=0))
        _log_warm_done(_DoneTask(result=None))
    finally:
        lg.removeHandler(h)
    assert seen == [], seen


def test_the_warm_done_line_is_info_once_and_debug_after():
    """It is a SUCCESS line that repeats at every 5-minute roll (9 families x 2
    sides), so it must not stay at INFO: MEASURED at 220 of the ~1,600 lines in a
    3-hour live.log window, every one of the 220 saying "working". INFO once --
    that line is the only proof the queue fix ever ran, which is what was
    invisible before -- and DEBUG after. The running total stays visible as
    `warm N` on the once-a-minute liveness line."""
    import logging

    import btcpredict.main as m

    seen: list[tuple[int, str]] = []

    class Handler(logging.Handler):
        def emit(self, record):                          # noqa: E704
            seen.append((record.levelno, record.getMessage()))

    lg = logging.getLogger("btcpredict")
    h = Handler()
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    old, m._WARM_LOGGED = m._WARM_LOGGED, False
    try:
        _log_warm_done(_DoneTask(result=18))
        _log_warm_done(_DoneTask(result=18))
    finally:
        lg.removeHandler(h)
        m._WARM_LOGGED = old
    assert [lv for lv, _ in seen] == [logging.INFO, logging.DEBUG], seen
    assert all("pre-resolved 18 token(s)" in msg for _, msg in seen), seen


def test_the_warm_done_line_never_raises():
    """It is a done-callback: an exception here is lost exactly as the warm's
    own was before `_report_task_failure` existed."""

    class NoResult:
        def cancelled(self): return False                # noqa: E704
        def exception(self): return None                 # noqa: E704
        def result(self): raise RuntimeError("no result")  # noqa: E704

    _log_warm_done(_DoneTask(result=1, exc=RuntimeError("boom")))
    _log_warm_done(_DoneTask(cancelled=True))
    _log_warm_done(NoResult())


def test_main_wires_the_warm_done_report():
    rec = next(n for n in ast.walk(ast.parse(MAIN))
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "_record")
    body = ast.get_source_segment(MAIN, rec) or ""
    assert "add_done_callback(_log_warm_done)" in body

