"""The decision path has to actually run.

Regression for `main.py:_record` using `math` without importing it. Two things
made it invisible:

  * the line sits behind `if sig.act and not gate_reason`, i.e. on exactly the
    ticks that are about to place an order, so the failure looked like a log
    symptom ("tick failed: NameError") rather than "the bot cannot trade" --
    and it aborted BEFORE the quote, the decision row and the order were
    written, so there was nothing in the tables to notice either;
  * every unit test touched tilt_factor / stake_for_fraction directly. Nothing
    executed the code that FEEDS them.

So this file drives the real `_record` with an actionable signal and asserts a
decision row comes out.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import tempfile
import time
from types import SimpleNamespace

import pytest

from btcpredict.edge.signal import Signal
from btcpredict.feeds.market_feed import Outcome, Round
from btcpredict.main import Bot
from btcpredict.models import PriceHistory
from btcpredict.pricing.vol import VolEstimator
from btcpredict.risk.guards import RiskGuard
from btcpredict.store.db import Store

SPOT = 77000.0
BANKROLL = 80.0


class FakeSpot:
    """Stands in for SpotFeed: the tick loop only needs these four things."""

    price = SPOT
    ts_ms = 0
    age_s = 0.0

    def is_fresh(self, max_age_s: float) -> bool:
        return True


def make_round(*, tau_s: float = 120.0) -> Round:
    """A round closing `tau_s` from now, on the REAL clock: _record reads its
    own now(), so a fixed fake end_ms lands outside the tau window."""
    now_ms = int(time.time() * 1000)
    return Round(
        slug="btc-updown-5m-1789000000", market_topic_id=1, market_id=2,
        condition_id="c", symbol="BTCUSDT",
        start_ms=now_ms - 180_000, end_ms=now_ms + int(tau_s * 1000),
        strike=SPOT, end_price=None, price_feed_id=None,
        feed_provider=None, fee_rate_bps=180, liquidity=5000.0,
        trade_volume=1000.0, status="OPEN", trading_status="OPEN",
        live_status="OPEN",
        outcomes=[Outcome(name="Up", price=0.40, chance=0.40, token_id="1",
                          market_outcome_id=11, winner=None),
                  Outcome(name="Down", price=0.60, chance=0.60, token_id="2",
                          market_outcome_id=12, winner=None)],
    )


def make_bot(store) -> Bot:
    bot = Bot.__new__(Bot)                      # skip network setup
    bot.mode = "live"
    bot.store = store
    bot.book = None                             # -> prices come from the round
    bot.broker = SimpleNamespace(mode="paper")  # skips the venue-position gate
    bot.open_fills = {}
    bot.bankroll = BANKROLL
    bot.symbols = ["BTCUSDT"]
    # _record looks these up per symbol, so both must exist as mappings.
    bot.spots = {"BTCUSDT": FakeSpot()}
    bot.histories = {"BTCUSDT": PriceHistory()}
    for i in range(5):
        bot.histories["BTCUSDT"].add_tick(SPOT + i)
    vol = VolEstimator(lam=0.97, floor=0.00015, lookback=240)
    vol.add(0.0)
    bot.vols = {"BTCUSDT": vol}
    # Stub the pricing model: this file is about the sizing/decision path, and
    # the real model holds on a 5-tick history anyway.
    bot.model = SimpleNamespace(
        name="stub",
        predict=lambda ctx: SimpleNamespace(emitted=True, p_up=0.62,
                                            reason="ok"))
    bot.up_model = None
    bot.guard = RiskGuard(daily_loss_cap=0.0, max_consecutive_losses=5,
                          max_open_rounds=1, daily_loss_cap_pct=0.20,
                          bankroll=lambda: bot.bankroll)
    bot._candle = {}
    bot._streak = {}
    bot._order_cooldown_until = 0.0
    return bot


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = Store(pathlib.Path(d) / "t.db")
        yield s
        s.close()


@pytest.fixture
def no_gates(monkeypatch):
    """Every gate that is not the one under test, switched off: this file is
    about the sizing path, not about whether the gate would have let it in."""
    monkeypatch.setattr("btcpredict.config.settings.signal_persistence", 1)
    monkeypatch.setattr("btcpredict.config.settings.trade_on_candle_open", False)
    monkeypatch.setattr("btcpredict.config.settings.side_filter", "both")
    monkeypatch.setattr("btcpredict.config.settings.up_regime_gate", 0.0)
    monkeypatch.setattr("btcpredict.config.settings.sizing_mode", "fraction")
    monkeypatch.setattr("btcpredict.config.settings.stake_tilt", "inv_z")


def actionable(monkeypatch, *, fair=0.62, quote=0.40):
    """Force the gate open so the sizing path is reached."""
    sig = Signal(side="Up", fair=fair, quote=quote, edge=fair - quote,
                 hurdle=0.0, ev_per_dollar=0.3, act=True, reason="ok",
                 fair_up_model=fair)
    # evaluate_hybrid is what _record actually calls -- patching `evaluate`
    # looked right and silently did nothing.
    monkeypatch.setattr("btcpredict.main.evaluate_hybrid", lambda *a, **k: sig)
    return sig


def test_an_actionable_signal_reaches_sizing_without_raising(store, monkeypatch,
                                                             no_gates):
    """The test that would have caught the missing `import math`: with the
    tilt on, _record computes |z| with math.log before it sizes."""
    bot = make_bot(store)
    actionable(monkeypatch)

    placed: list[float] = []

    async def fake_place(r, sig, *, fair_up, token, stake):
        placed.append(stake)
        return None                             # rejected: no fill row

    bot._place_order = fake_place
    asyncio.run(bot._record(make_round(), "BTCUSDT", trade=True, primary=True))

    assert bot.store.counts()["quotes"] == 1
    rows = bot.store.query("SELECT * FROM decisions")
    assert len(rows) == 1
    assert rows[0]["reason"] == "ok", "an un-gated signal must reach sizing"
    assert placed, "and then must attempt an order"
    # 4% of $80 = $3.20, moved by the tilt and clamped to [floor, cap]
    assert 1.0 <= placed[0] <= 25.0


def test_a_gated_signal_is_recorded_but_not_ordered(store, monkeypatch,
                                                    no_gates):
    """The other half: a signal the gate blocks must not reach the order path,
    but must still leave a decision row carrying the reason."""
    monkeypatch.setattr("btcpredict.config.settings.signal_persistence", 5)
    bot = make_bot(store)
    actionable(monkeypatch)

    async def boom(*a, **k):
        raise AssertionError("a gated signal must not be ordered")

    bot._place_order = boom
    asyncio.run(bot._record(make_round(), "BTCUSDT", trade=True, primary=True))

    rows = bot.store.query("SELECT * FROM decisions")
    assert len(rows) == 1
    assert "held" in rows[0]["reason"]
    assert rows[0]["acted"] == 0


def test_impulse_mode_poll_never_falls_through_to_the_model(store, monkeypatch,
                                                            no_gates):
    """Regression, 2026-09-12: impulse entries moved to the feed's clock, and
    the poll's own trigger was made conditional on `impulse_on_tick` -- which
    left the poll falling through to the MODEL block below it, so the live bot
    would have traded the model once a second and the impulse never.

    The `return` in that branch is what keeps impulse mode impulse-only, so
    this asserts the poll records and decides NOTHING when the trigger belongs
    to the fast path: no model call, no order, no decision row.
    """
    monkeypatch.setattr("btcpredict.config.settings.entry_mode", "impulse")
    monkeypatch.setattr("btcpredict.config.settings.impulse_on_tick", True)
    bot = make_bot(store)
    actionable(monkeypatch)                     # actionable, IF it were consulted

    def must_not_be_asked(ctx):
        raise AssertionError("impulse mode must never consult the model")

    bot.model = SimpleNamespace(name="stub", predict=must_not_be_asked)

    async def boom(*a, **k):
        raise AssertionError("impulse mode must never order from the poll")

    bot._place_order = boom
    asyncio.run(bot._record(make_round(), "BTCUSDT", trade=True, primary=True))

    assert bot.store.counts()["quotes"] == 1, "the poll still records the tick"
    assert bot.store.counts()["decisions"] == 0


def test_both_mode_falls_through_to_the_model_when_the_impulse_is_quiet(
        store, monkeypatch, no_gates):
    """entry_mode="both" is the union arm: the impulse owns a round when it
    fires (on the websocket clock, before this poll), and otherwise the model
    decides exactly as in "model" mode.

    The whole change is that the `return` guarding the impulse branch became
    conditional. This pins the half that is new -- a quiet impulse must reach
    the model -- while the test above still pins that PURE impulse mode does
    not. Measured 2026-09-12: the two gates want the same round only 52 times
    in 830, which is why falling through does not starve the impulse.
    """
    monkeypatch.setattr("btcpredict.config.settings.entry_mode", "both")
    monkeypatch.setattr("btcpredict.config.settings.impulse_on_tick", True)
    bot = make_bot(store)
    actionable(monkeypatch)

    asked = []
    real = bot.model.predict
    bot.model = SimpleNamespace(
        name="stub", predict=lambda ctx: (asked.append(1), real(ctx))[1])

    placed = []

    async def capture(*a, **k):
        placed.append(k)
        return None

    bot._place_order = capture
    asyncio.run(bot._record(make_round(), "BTCUSDT", trade=True, primary=True))

    assert asked, "both mode must consult the model when the impulse is quiet"
    assert bot.store.counts()["decisions"] == 1
    assert placed, "the model's entry must still reach the order path"
    assert placed[0].get("impulse_order") is not True, (
        "a model entry in both mode must NOT be tagged as an impulse order: "
        "that tag skips the model re-check and applies the impulse price cap")


def test_both_mode_still_lets_the_impulse_own_the_round(store, monkeypatch,
                                                        no_gates):
    """CHANGED 2026-09-13: the refusal is no longer what this pins.

    This used to assert that a round the impulse claimed was never re-entered
    by the model, via the shared one-position-per-round rule. Arms are
    independent now and each has its own budget, so the impulse claiming a
    round no longer spends the model's slot -- `guard.on_open(slug)` below
    fills the MAIN pool, which is what still refuses the model's own second
    entry. That invariant (an arm cannot double ITSELF) is the one worth
    keeping here.
    """
    monkeypatch.setattr("btcpredict.config.settings.entry_mode", "both")
    monkeypatch.setattr("btcpredict.config.settings.impulse_on_tick", True)
    bot = make_bot(store)
    actionable(monkeypatch)
    r = make_round()
    bot.guard.on_open(r.slug)                   # the MODEL arm holds it

    async def boom(*a, **k):
        raise AssertionError("the model re-entered a round it already holds")

    bot._place_order = boom
    asyncio.run(bot._record(r, "BTCUSDT", trade=True, primary=True))

    assert bot.store.counts()["decisions"] == 1, "the refusal is still recorded"


def _stale(monkeypatch, **over):
    """Turn the stale-display maker on with the shipped thresholds."""
    monkeypatch.setattr("btcpredict.config.settings.stale_maker_enabled", True)
    for k, v in dict(stale_maker_min_gap=0.05, stale_maker_min_edge=0.20,
                     stale_maker_offset=0.01, stale_maker_wait_s=5.0).items():
        monkeypatch.setattr(f"btcpredict.config.settings.{k}", over.get(k, v))


def test_a_stale_display_rests_below_the_book_instead_of_crossing(
        store, monkeypatch, no_gates):
    """The whole point: the display lags the book by ~2s, and the measured edge
    is only there if we buy BELOW the book (+0.3449/$1 at the mid, t 5.88, vs
    +0.0175 at the ask, t 0.47). So the order must rest, never cross.

    The round's displayed Up price is 0.40 while the gate's quote (the book ask)
    is 0.70 -- a 0.30 stale gap.
    """
    _stale(monkeypatch)
    bot = make_bot(store)
    actionable(monkeypatch, fair=0.90, quote=0.70)
    posted = {}

    async def buy_limit(**kw):
        posted.update(kw)
        return None                             # rested, nobody crossed to us

    bot.broker = SimpleNamespace(mode="paper", buy_limit=buy_limit)

    async def boom(*a, **k):
        raise AssertionError("a stale display must NOT be crossed with a taker")

    bot._place_order = boom
    asyncio.run(bot._record(make_round(), "BTCUSDT", trade=True, primary=True))

    assert posted, "it must post a resting bid"
    assert posted["limit_px"] == 0.41, "post at the stale display plus the offset"
    assert posted["limit_px"] < 0.70, "and strictly below the book, or it crosses"


def test_an_unfilled_rest_leaves_the_attempt_recorded(store, monkeypatch,
                                                      no_gates):
    """Fill rate is the one number the backtest cannot supply, so an unfilled
    attempt has to stay on the record: fills / attempts is only recoverable if
    the attempt keeps its decision row at acted=0."""
    _stale(monkeypatch)
    bot = make_bot(store)
    actionable(monkeypatch, fair=0.90, quote=0.70)

    async def buy_limit(**kw):
        return None

    bot.broker = SimpleNamespace(mode="paper", buy_limit=buy_limit)
    asyncio.run(bot._record(make_round(), "BTCUSDT", trade=True, primary=True))

    rows = bot.store.query("SELECT acted FROM decisions")
    assert len(rows) == 1, "the attempt is recorded"
    assert rows[0]["acted"] == 0, "and stays unpromoted until it actually fills"


def test_a_fresh_display_still_takes_the_normal_taker_path(store, monkeypatch,
                                                          no_gates):
    """No lag, no maker. With the book at 0.42 against a 0.40 display the gap is
    0.02, under stale_maker_min_gap -- there is nothing stale to rest on."""
    _stale(monkeypatch)
    bot = make_bot(store)
    actionable(monkeypatch, fair=0.90, quote=0.42)
    took = []

    async def buy_limit(**kw):
        raise AssertionError("a fresh display must not go to the maker path")

    async def taker(*a, **k):
        took.append(1)
        return None

    bot.broker = SimpleNamespace(mode="paper", buy_limit=buy_limit)
    bot._place_order = taker
    asyncio.run(bot._record(make_round(), "BTCUSDT", trade=True, primary=True))
    assert took, "it must fall back to the taker order"


def test_the_arm_is_inert_while_the_flag_is_off(store, monkeypatch, no_gates):
    """It ships OFF because the fill rate is unmeasured; the flag is the only
    thing standing between a backtest and real money."""
    monkeypatch.setattr("btcpredict.config.settings.stale_maker_enabled", False)
    bot = make_bot(store)
    actionable(monkeypatch, fair=0.90, quote=0.70)
    took = []

    async def buy_limit(**kw):
        raise AssertionError("the flag is off; nothing may rest")

    async def taker(*a, **k):
        took.append(1)
        return None

    bot.broker = SimpleNamespace(mode="paper", buy_limit=buy_limit)
    bot._place_order = taker
    asyncio.run(bot._record(make_round(), "BTCUSDT", trade=True, primary=True))
    assert took, "with the flag off the taker path is unchanged"


def test_the_tilt_moves_the_stake_the_right_way(store, monkeypatch, no_gates):
    """Small |z| means the round is still undecided, which the recording says
    is where the edge is: it must stake MORE, not less."""
    bot = make_bot(store)
    sig = actionable(monkeypatch)
    # With the live `stake_cap_usdt=1.0` BOTH sides clamp to 1.0 and the
    # comparison becomes `1.0 > 1.0` -- the test stops measuring the tilt and
    # starts measuring the cap. It ships 0.0 (off); pin that.
    monkeypatch.setattr("btcpredict.config.settings.stake_cap_usdt", 0.0)
    assert bot._stake_for(sig, 0.05) > bot._stake_for(sig, 3.0)




# The cheap half of the guard: catch the class of error, not just this instance.
_MODULES = {
    "math", "json", "os", "sys", "re", "time", "random", "statistics",
    "itertools", "collections", "datetime", "decimal", "hashlib", "base64",
    "uuid", "asyncio", "logging", "contextlib", "signal", "argparse", "urllib",
    "http", "socket", "sqlite3", "csv", "pathlib", "functools", "operator",
    "bisect", "heapq", "string", "textwrap", "warnings", "traceback",
    "threading", "queue", "subprocess", "shutil", "tempfile", "zipfile",
    "gzip", "pickle", "struct", "copy", "enum", "dataclasses", "typing", "abc",
    "inspect", "platform", "secrets", "hmac", "ssl", "glob", "stat", "errno",
    "multiprocessing", "concurrent", "calendar", "zoneinfo",
}


def _aliases(node: ast.AST) -> set[str]:
    """Names an Import/ImportFrom binds."""
    if isinstance(node, ast.Import):
        return {a.asname or a.name.split(".")[0] for a in node.names}
    if isinstance(node, ast.ImportFrom):
        return {a.asname or a.name for a in node.names}
    return set()


def _own_bindings(scope: ast.AST) -> set[str]:
    """Names bound in this scope, NOT counting anything bound only inside a
    nested function.

    That exclusion is the whole point. The real bug was an `import math` sitting
    inside `refresh_vol_if_stale` while `_record` used math.log: a naive "is math
    imported anywhere in this file?" check answers yes and misses it.
    """
    nested: set[int] = set()
    for n in getattr(scope, "body", []):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nested |= {id(c) for c in ast.walk(n)}
    out: set[str] = set()
    for n in ast.walk(scope):
        if id(n) in nested:
            continue
        out |= _aliases(n)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out |= set(n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            out.add(n.id)
    return out


def undefined_module_usages(where: str, source: str) -> list[str]:
    """`<module>.<attr>` where <module> is stdlib and not visible in that scope.

    Scope-aware: a module imported at the top of the file is visible everywhere,
    one imported inside a function is visible only inside it.
    """
    tree = ast.parse(source)
    module = _own_bindings(tree)
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)):
            continue
        name = node.value.id
        if name not in _MODULES:
            continue
        scope: ast.AST = tree
        p = parents.get(id(node))
        while p is not None:
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = p
                break
            p = parents.get(id(p))
        known = module if scope is tree else _own_bindings(scope) | module
        if name not in known:
            out.append(f"{where}:{node.lineno} uses {name}.{node.attr} "
                       f"without importing {name} in its scope")
    return out


def test_the_missing_import_checker_sees_function_local_imports():
    """Self-test for the guard below, on the exact shape of the real bug: a
    module imported inside one function must not license a use in another."""
    src = (
        "def has_its_own():\n"
        "    import math\n"
        "    return math.log(1.0)\n"
        "\n"
        "\n"
        "def does_not():\n"
        "    return math.sqrt(2.0)\n"
    )
    found = undefined_module_usages("sample.py", src)
    assert len(found) == 1, found
    assert ":7 " in found[0], found          # the sqrt line, not the log line


def test_no_stdlib_module_is_used_without_being_imported():
    """There is no linter in this environment (no ruff/pyflakes/flake8), so this
    walks the AST itself. It is deliberately over-permissive about assignments
    and arguments -- it reports only a module that is genuinely out of scope."""
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for pkg in ("btcpredict", "tools", "tests"):
        for path in sorted((root / pkg).rglob("*.py")):
            offenders += undefined_module_usages(
                str(path.relative_to(root)), path.read_text())
    assert not offenders, "missing imports:\n  " + "\n  ".join(sorted(set(offenders)))


class TestTheRecorderKeepsAWiderBookWindowThanTrading:
    """Ask coverage in the recorded tape decided what could be tuned.

    `ws_book_max_age_s` (2s) is right for trading -- a stale book must never
    price an order -- but it also gates the RECORDER, and off-primary that is
    most of the tape: btc-5m ~100% (it is the primary and gets the REST book
    every poll) against eth-5m 31% and bnb-5m 17%.

    The cost showed up on 2026-09-15 when asked whether each symbol should have
    its own tuned knobs. It could not be answered: at the shipped gates bnb-5m
    had FOUR priceable crossings and btc-5m had 302.

    So the recorder falls back to a wider window and stamps `book_age_ms`. That
    is strictly more information -- a replay filters to whatever freshness it
    wants -- and it must never reach an order.
    """

    class _Book:
        """A ws book that is FRESH for nobody and stale-but-present for all."""
        def __init__(self, age_s):
            self._age = age_s
            self.messages = 0

        def best(self, market_id, max_age_s):
            if self._age > max_age_s:
                return None
            # SIZES INCLUDED, because the real `BookTop` always carries them
            # and the recorder now writes them. A fake that omits a field the
            # production object guarantees does not simplify the test, it just
            # moves the failure to an AttributeError in the recorder.
            return SimpleNamespace(up_bid=0.40, up_ask=0.42,
                                   down_bid=0.58, down_ask=0.60,
                                   up_ask_size=350.0, up_bid_size=120.0,
                                   age_s=lambda: self._age)

    def _row(self, store, monkeypatch, *, age_s, record_window):
        monkeypatch.setattr("btcpredict.config.settings.ws_book_max_age_s", 2.0)
        monkeypatch.setattr(
            "btcpredict.config.settings.ws_book_record_max_age_s", record_window)
        bot = make_bot(store)
        bot.book = None                      # no REST book: off-primary
        bot.ws_book = self._Book(age_s)
        asyncio.run(bot._record(make_round(), "BTCUSDT", trade=False,
                                primary=False))
        rows = bot.store.query("SELECT * FROM quotes")
        assert rows, "the recorder must still write a row"
        return rows[-1]

    def test_a_stale_book_is_recorded_with_its_age(self, store, monkeypatch,
                                                   no_gates):
        r = self._row(store, monkeypatch, age_s=9.0, record_window=30.0)
        assert r["up_ask"] == pytest.approx(0.42), (
            "a 9s-old book is useless for an order and still worth recording")
        assert r["book_age_ms"] == pytest.approx(9000, abs=50), (
            "and the age must be stamped, or the replay cannot filter it")

    def test_a_fresh_book_records_a_small_age(self, store, monkeypatch, no_gates):
        r = self._row(store, monkeypatch, age_s=0.5, record_window=30.0)
        assert r["up_ask"] == pytest.approx(0.42)
        assert r["book_age_ms"] == pytest.approx(500, abs=50)

    def test_the_size_at_the_touch_is_recorded_beside_the_price(
            self, store, monkeypatch, no_gates):
        """⭐ WHAT THE TAPE COULD NEVER ANSWER BEFORE.

        Every recorded row said what the book COST and none said what it HELD,
        so "was our stake actually there to buy?" was unanswerable for the
        whole history -- and that is the question underneath every no-fill.
        `BookTop` has carried these two numbers all along; they were simply
        never written down.
        """
        r = self._row(store, monkeypatch, age_s=0.5, record_window=30.0)
        assert r["up_ask_size"] == pytest.approx(350.0)
        assert r["up_bid_size"] == pytest.approx(120.0)

    def test_the_size_is_stamped_even_on_a_stale_recorded_book(
            self, store, monkeypatch, no_gates):
        """Same rule as `book_age_ms`: record it with its age and let the
        replay decide. A stale size is information; a missing one is not."""
        r = self._row(store, monkeypatch, age_s=9.0, record_window=30.0)
        assert r["up_ask_size"] == pytest.approx(350.0)
        assert r["book_age_ms"] == pytest.approx(9000, abs=50)

    def test_beyond_the_record_window_nothing_is_written(self, store, monkeypatch,
                                                         no_gates):
        r = self._row(store, monkeypatch, age_s=99.0, record_window=30.0)
        assert r["up_ask"] is None
        assert r["book_age_ms"] is None
        assert r["up_ask_size"] is None, (
            "no book means no size: 0 would read as a thin book to a replay")
        assert r["up_bid_size"] is None

    def test_zero_means_off(self, store, monkeypatch, no_gates):
        """0 = OFF falls back to the trading gate, so a 9s book is dropped."""
        r = self._row(store, monkeypatch, age_s=9.0, record_window=0.0)
        assert r["up_ask"] is None, "0 must behave exactly as before this change"

    def test_the_trading_paths_do_not_use_the_record_window(self):
        """The whole safety argument. _cross_tick and the execute path must
        keep asking for ws_book_max_age_s; only _record may widen it."""
        import inspect
        import btcpredict.main as m
        for fn in (m.Bot._cross_tick, m.Bot._cross_execute):
            src = inspect.getsource(fn)
            assert "ws_book_record_max_age_s" not in src, (
                f"{fn.__name__} must never price off the recorder's window")
