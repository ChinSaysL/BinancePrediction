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
    asyncio.run(bot._record(make_round(), "BTCUSDT", trade=True))

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
    asyncio.run(bot._record(make_round(), "BTCUSDT", trade=True))

    rows = bot.store.query("SELECT * FROM decisions")
    assert len(rows) == 1
    assert "held" in rows[0]["reason"]
    assert rows[0]["acted"] == 0


def test_the_tilt_moves_the_stake_the_right_way(store, monkeypatch, no_gates):
    """Small |z| means the round is still undecided, which the recording says
    is where the edge is: it must stake MORE, not less."""
    bot = make_bot(store)
    sig = actionable(monkeypatch)
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
