"""The cap's decision row must describe a BLOCKED crossing, not an entry.

⛔ THE DUPLICATE TICK. `_cross_fired` is stamped with the claim, BEFORE the
venue round-trip, so the next tick that evaluates the SAME crossing finds the
slot spent. That tick used to write `slot already used this round (1/1) --
crossing DROPPED, priced for the counterfactual only` for a crossing that was
being ENTERED at that very moment.

The live shape, `btc-updown-5m-1789710900` on 2026-09-18:

    05:57:53,320  CROSS ... Up sweep_px=0.630 (fc 77481.32 vs twap60 77477.82,
                  tau 127s) stake=$1.00 -> 26 today      <- the entry
    05:57:53,326  ... Up not taken: slot already used this round (1/1) --
                  crossing DROPPED, priced for the counterfactual only
    05:57:53,329  CROSS take via FAK ...               <- still the first tick
    05:57:55,188  cross exec ... NO FILL in 1.86s | committed $0 of $1.00

The order lines belong to the fire; the round's ONLY `cross into` row is that
one. MEASURED over the Polymarket era, each cap row against its round's own
entry row (n=40): 35 within 1.43 s, one at 5.5 s, three genuinely later
crossings at 10.4 / 23.8 / 41.6 s, and one round that never entered -- so 87.5%
of the population the counterfactual is priced from described entries that were
taken. The band 5.5-10.4 s is EMPTY, which is why the window sits inside it.

The cap itself is unchanged by any of this; only the ROW is suppressed, and the
entry that caused it keeps its own `cross into` row.
"""

from __future__ import annotations

import asyncio
import ast
import pathlib
import tempfile
import time
from collections import deque
from types import SimpleNamespace

import pytest

from btcpredict.feeds.market_feed import Outcome, Round
from btcpredict.main import Bot, CROSS_DUP_TICK_MS
from btcpredict.risk.guards import RiskGuard
from btcpredict.store.db import Store

SRC = pathlib.Path(__file__).resolve().parents[1] / "btcpredict" / "main.py"
STRIKE = 77040.0
SLUG = "btc-updown-5m-1789000000"


def _cap_guard():
    """Every `if` statement in main.py whose test mentions the per-round cap."""
    tree = ast.parse(SRC.read_text())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        names = {n.attr for n in ast.walk(node.test) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        src = ast.dump(node.test)
        if "_cross_fired" in src and "max_per_round" in src:
            out.append(node)
    return out


def test_the_per_round_cap_guard_exists():
    assert _cap_guard(), "the _cross_fired vs max_per_round guard is gone"


def _refuse_calls(node):
    return [c for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr == "_cross_refuse"]


def test_the_cap_guard_records_a_decision_row_not_just_a_log():
    """`_cross_refuse` is the helper that LOGS and INSERTS into `decisions`.
    A bare return, or a bare log.info, leaves the cap's cost unmeasurable --
    a log line cannot be joined against settled P&L."""
    for node in _cap_guard():
        assert _refuse_calls(node), (
            "the max_per_round guard drops the crossing without recording it "
            "(main.py line %d)" % node.lineno
        )


def test_the_recorded_row_carries_what_the_counterfactual_needs():
    """spot + strike give the gap; tau + sigma_1m give z. Without them the row
    cannot be priced, which is the entire point of recording it."""
    for node in _cap_guard():
        for call in _refuse_calls(node):
            kws = {k.arg for k in call.keywords}
            assert {"tau", "spot"} <= kws, (
                "the cap-block decision row must pass tau and spot so z is "
                "recoverable (main.py line %d)" % node.lineno
            )


def test_it_only_records_when_a_crossing_was_actually_detected():
    """A tick with no crossing would have returned at the trigger anyway;
    recording it would invent rows for rounds that never crossed.

    `crossed_side` is a module-level function, so it appears as a Name call --
    not an Attribute call like the `_cross_refuse` and `_ck` methods.
    """
    for node in _cap_guard():
        called = set()
        for c in ast.walk(node):
            if not isinstance(c, ast.Call):
                continue
            if isinstance(c.func, ast.Name):
                called.add(c.func.id)
            elif isinstance(c.func, ast.Attribute):
                called.add(c.func.attr)
        assert "crossed_side" in called, (
            "_cross_refuse is called without resolving the side first, so every "
            "blocked tick records a row whether or not a crossing existed "
            "(main.py line %d)" % node.lineno
        )


def test_the_message_says_the_crossing_is_dropped():
    """Findable by someone asking this exact question."""
    text = SRC.read_text()
    idx = text.find("crossing DROPPED")
    assert idx > 0, "the cap-block reason lost the word DROPPED"
    guard_start = min(n.lineno for n in _cap_guard())
    assert text[:idx].count("\n") + 1 > guard_start


# --------------------------------------------------------------------------
# the duplicate tick
# --------------------------------------------------------------------------

def test_the_window_sits_in_the_EMPTY_band_of_the_measurement():
    """5.5 s separates the duplicates from the genuine later crossings, 10.4 s is
    the first genuine one: anything in between gives the same answer, so the
    value is not fitted and must not be quoted as an optimum."""
    assert 5_500 < CROSS_DUP_TICK_MS < 10_400, (
        f"{CROSS_DUP_TICK_MS} ms is outside the band where the measurement is "
        "silent (5.5-10.4 s over n=40)")


def test_the_constant_carries_the_measurement():
    src = SRC.read_text()
    assert "CROSS_DUP_TICK_MS" in src
    block = src[src.find("CROSS_DUP_TICK_MS") - 1200:
                 src.find("CROSS_DUP_TICK_MS")]
    assert "10.4" in block and "23.8" in block and "41.6" in block, (
        "the constant must carry the deltas it separates")
    assert "EMPTY" in block, ("and the fact that the band between them is "
                              "empty, i.e. this is a separator not a fit")


class FakeFeed:
    def __init__(self, t):
        self.ticks = deque(t)
        self.price = t[-1][1]
        self.ts_ms = t[-1][0]

    def is_fresh(self, max_age_s):
        return True


def _ref_window(start_ms, px, n=60):
    return [(start_ms - (n - i) * 1000, px) for i in range(n)]


def _round(tau_s=20.0):
    now = int(time.time() * 1000)
    return Round(
        slug=SLUG, market_topic_id=1, market_id=2, condition_id="c",
        symbol="BTCUSDT", start_ms=now - 180_000, end_ms=now + int(tau_s * 1000),
        strike=STRIKE, end_price=None, price_feed_id=None, feed_provider=None,
        fee_rate_bps=200, liquidity=5000.0, trade_volume=1000.0, status="OPEN",
        trading_status="OPEN", live_status="OPEN",
        outcomes=[Outcome(name="Up", price=0.30, chance=0.30, token_id="1",
                          market_outcome_id=11, winner=None),
                  Outcome(name="Down", price=0.70, chance=0.70, token_id="2",
                          market_outcome_id=12, winner=None)])


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = Store(pathlib.Path(d) / "t.db")
        yield s
        s.close()


@pytest.fixture
def armed(monkeypatch):
    for k, v in (("cross_enabled", True), ("cross_look_s", 2.0),
                 ("cross_min_tau_s", 3.0), ("cross_max_tau_s", 30.0),
                 ("cross_max_price", 0.50), ("cross_min_price", 0.02),
                 ("cross_max_per_round", 1)):
        monkeypatch.setattr("btcpredict.config.settings." + k, v)


def _bot(store, claimed_ms_ago=None):
    """A round whose ONE slot is already spent, with the entry it belongs to
    either fresh (a duplicate tick) or old (a genuinely later crossing)."""
    now = int(time.time() * 1000)
    r = _round()
    feed = FakeFeed([(now - 3000, 77020.0), (now - 2000, 77020.0),
                     (now, 77035.0)])          # ticks UP through the level
    bot = Bot.__new__(Bot)
    bot.mode = "paper"
    bot.store = store
    bot.book = None
    bot.broker = SimpleNamespace(mode="paper")
    bot.bankroll = 50.0
    bot.open_fills = {}
    bot._primary_spot = feed
    bot.spot = feed
    bot.spots = {"BTCUSDT": feed}
    bot._round = r
    bot._asks = (0.30, 0.70)
    bot._sigma = 0.0002
    bot._cross_fired = {SLUG: 1}
    bot._cross_day = ""
    bot._cross_count = 1
    bot._order_cooldown_until = 0.0
    bot._cross_last_ms = ({SLUG: now - claimed_ms_ago}
                          if claimed_ms_ago is not None else {})
    # a FULL 60 s reference window, so the level resolves off the tape with no
    # kline read even though this fixture has no session
    bot._ticks = {"BTCUSDT": _ref_window(r.start_ms, 77030.0)
                  + [(r.start_ms + 1000, 77020.0)]}
    bot.guard = RiskGuard(daily_loss_cap=0.0, max_consecutive_losses=5,
                          max_open_rounds=1, daily_loss_cap_pct=0.20,
                          bankroll=lambda: 50.0)
    return bot


def _rows(store):
    return list(store.query("select reason from decisions"))


def test_a_duplicate_tick_does_not_write_a_cap_row(store, armed):
    """THE LIVE CASE. The slot was claimed 6 ms ago (the entry is still being
    placed), so this tick is that same crossing, not a second one."""
    bot = _bot(store, claimed_ms_ago=6)
    asyncio.run(bot._cross_tick())
    assert not [r for r in _rows(store) if "slot already used" in r[0]], (
        "the entry's own duplicate tick was recorded as a blocked crossing")


def test_a_genuinely_later_crossing_still_writes_one(store, armed):
    """The mirror image, and the reason this is not a disabling: a crossing
    that arrives 20 s after the entry is a SECOND crossing and its row is the
    only way to price it."""
    bot = _bot(store, claimed_ms_ago=20_000)
    asyncio.run(bot._cross_tick())
    rows = [r for r in _rows(store) if "slot already used" in r[0]]
    assert rows, "a later crossing must still be recorded and priced"


def test_the_window_boundary_is_the_only_thing_that_changes(store, armed):
    """Just inside vs just outside, on the same tape: the record flips."""
    inside = _bot(store, claimed_ms_ago=CROSS_DUP_TICK_MS - 50)
    asyncio.run(inside._cross_tick())
    assert not [r for r in _rows(store) if "slot already used" in r[0]]
    outside = _bot(store, claimed_ms_ago=CROSS_DUP_TICK_MS + 50)
    asyncio.run(outside._cross_tick())
    assert [r for r in _rows(store) if "slot already used" in r[0]]


def test_an_untimed_claim_is_recorded(store, armed):
    """If the claim time is missing (an old process, a fixture) the safe
    default is the pre-change behaviour: record it."""
    bot = _bot(store, claimed_ms_ago=None)
    asyncio.run(bot._cross_tick())
    assert [r for r in _rows(store) if "slot already used" in r[0]]


def test_the_cap_itself_is_untouched(store, armed):
    """This suppresses a ROW, not a trade: the slot is still only spendable
    once, and no order path is reached from the blocked branch."""
    calls = []
    bot = _bot(store, claimed_ms_ago=6)

    async def place(*a, **k):
        calls.append(k)
        return None

    bot._place_order = place
    asyncio.run(bot._cross_tick())
    assert not calls
    assert bot._cross_fired[SLUG] == 1, "the slot count must not move"
