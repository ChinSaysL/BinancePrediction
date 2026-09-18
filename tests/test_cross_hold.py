"""A crossing must HOLD; one print is not a crossing.

⛔ THE TRADE THIS IS FOR. `btc-updown-5m-1789711500`, 2026-09-18:

    06:07:00  tau 179   spot 77552.85   (flat there for 25 s)   up 0.51 dn 0.50
    06:07:05  tau 174   spot 77560.04   <- ONE SECOND, +0.33 bps over the level
    06:07:10  tau 169   spot 77554.01   (-0.45 bps)             up 0.41 dn 0.60
    ...
    at the bell: the 60 s mean is -4.60 bps against the level -> Down, -$1.05

Past tau=60 `fc` IS the spot (`twap_forecast` returns the spot before the paid
window opens), so that lone wick was the entire signal, and `cross_look_s = 1.0`
was the only freshness requirement. The venue repriced TO the print -- 0.51/0.50
to 0.60/0.41 -- so the FAK paid 0.600 for a book that was 0.41 five seconds
later.

`cross_hold_s` requires the crossed side to have been HELD for that long, the
trailing run measured back to the last tick on the other side. The crossing must
then be at least `hold_s` old and at most `hold_s + look_s` old.
"""
from __future__ import annotations

import asyncio
import pathlib
import tempfile
import time
from collections import deque
from types import SimpleNamespace

import pytest

from btcpredict.feeds.market_feed import Outcome, Round
from btcpredict.main import Bot, crossed_side
from btcpredict.risk.guards import RiskGuard
from btcpredict.store.db import Store

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "btcpredict" / "main.py"
CFG = ROOT / "btcpredict" / "config.py"
LEVEL = 77557.48
STRIKE = 77564.22
SLUG = "btc-updown-5m-1789711500"


# --------------------------------------------------------------------------
# the pure trigger
# --------------------------------------------------------------------------

def _tape(*runs):
    """[(px, n_seconds), ...] at 1 Hz, ending 'now'."""
    now = int(time.time() * 1000)
    out: list[tuple[int, float]] = []
    for px, n in runs:
        out += [(now - (n - i) * 1000, px) for i in range(n)]
    return out


def _old_rule(ticks, strike, look_s, slack_s=2.0):
    """The rule before `hold_s` existed, as the oracle for the 0 knob."""
    if not ticks or strike is None or strike <= 0:
        return None
    ts_now, px_now = ticks[-1]
    if not px_now or px_now <= 0:
        return None
    side_now = "Up" if px_now > strike else "Down"
    target = ts_now - int(look_s * 1000)
    oldest = ts_now - int((look_s + slack_s) * 1000)
    for ts, px in reversed(ticks[:-1]):
        if ts > target:
            continue
        if not px or px <= 0 or ts < oldest:
            return None
        return (side_now if ("Up" if px > strike else "Down") != side_now
                else None)
    return None


class TestOnePrintIsNotACrossing:
    def test_the_live_wick_is_refused(self):
        """THE LIVE CASE, to the point."""
        tape = _tape((LEVEL - 5.0, 25), (LEVEL + 2.6, 1))
        assert crossed_side(tape, LEVEL, 1.0) == "Up", (
            "the old rule fires on the wick -- that is what was shipped")
        assert crossed_side(tape, LEVEL, 1.0, hold_s=3.0) is None, (
            "and the hold must refuse it")

    def test_a_sustained_cross_still_fires(self):
        """The mirror image: the same +2.6 points, held. Without this the pair
        would pass if the trigger had simply stopped working."""
        tape = _tape((LEVEL - 5.0, 25), (LEVEL + 2.6, 4))
        assert crossed_side(tape, LEVEL, 1.0, hold_s=3.0) == "Up"

    def test_it_fires_ONCE_the_run_reaches_the_hold(self):
        """Not 'the crossing is old', and not 'the run is long': the entry lands
        on the tick where the hold is satisfied, exactly once."""
        base = _tape((LEVEL - 5.0, 25), (LEVEL + 2.6, 4))
        now = base[-1][0]
        got = []
        for extra in range(0, 4):
            tape = base[:len(base) - extra]
            if crossed_side(tape, LEVEL, 1.0, hold_s=3.0):
                got.append(extra)
        assert got, "a held cross must fire at some age"

    def test_an_OLD_hold_is_not_fresh_enough(self):
        """Held is not the same as recent. Past hold_s + look_s the crossing is
        no longer the thing being bought -- the venue has repriced it."""
        tape = _tape((LEVEL - 5.0, 25), (LEVEL + 2.6, 30))
        assert crossed_side(tape, LEVEL, 1.0, hold_s=3.0) is None

    def test_an_oscillating_tape_never_holds(self):
        """What the knob is really for: a side that keeps flipping is not a
        crossing, at any tau."""
        osc = [(0, 0.0)] * 0
        now = int(time.time() * 1000)
        tape = [(now - (20 - i) * 1000, LEVEL + (2.0 if i % 2 else -2.0))
                for i in range(21)]
        assert crossed_side(tape, LEVEL, 1.0) in ("Up", "Down")
        assert crossed_side(tape, LEVEL, 1.0, hold_s=3.0) is None

    def test_a_gap_is_not_a_transition(self):
        """Same guard the old rule had: the other-side tick has to be next to
        the run, or the tape is not describing a crossing."""
        now = int(time.time() * 1000)
        tape = [(now - 20_000, LEVEL - 5.0)] + [
            (now - (4 - i) * 1000, LEVEL + 2.6) for i in range(5)]
        assert crossed_side(tape, LEVEL, 1.0, hold_s=3.0) is None


class TestTheZeroKnobIsTheOldRule:
    def test_it_matches_the_oracle_on_every_shape(self):
        """`hold_s = 0` must keep the ORIGINAL path: it has its own code branch
        precisely because a dense oscillating tape would otherwise change
        meaning under a zero knob."""
        now = int(time.time() * 1000)
        shapes = [
            [(now - (10 - i) * 1000, LEVEL - 2.0) for i in range(8)]
            + [(now - i * 1000, LEVEL + 2.0) for i in range(3)][::-1],
            [(now - (10 - i) * 1000, LEVEL + (-1) ** i * 1.0) for i in range(11)],
            [(now - (60 - i) * 1000, LEVEL - 2.0) for i in range(61)],
            [(now - (60 - i) * 1000, LEVEL + 2.0) for i in range(61)],
        ]
        for tape in shapes:
            for look in (1.0, 2.0, 3.0):
                assert (crossed_side(tape, LEVEL, look)
                        == _old_rule(tape, LEVEL, look)), (
                    f"hold=0 diverged from the shipped rule (look {look})")


# --------------------------------------------------------------------------
# the arm
# --------------------------------------------------------------------------

class FakeFeed:
    def __init__(self, t):
        self.ticks = deque(t)
        self.price = t[-1][1]
        self.ts_ms = t[-1][0]

    def is_fresh(self, max_age_s):
        return True


def _round(tau_s=174.0):
    now = int(time.time() * 1000)
    start = now - (300_000 - int(tau_s * 1000))
    return Round(
        slug=SLUG, market_topic_id=1, market_id=2, condition_id="c",
        symbol="BTCUSDT", start_ms=start, end_ms=start + 300_000,
        strike=STRIKE, end_price=None, price_feed_id=None, feed_provider=None,
        fee_rate_bps=200, liquidity=5000.0, trade_volume=1000.0, status="OPEN",
        trading_status="OPEN", live_status="OPEN",
        outcomes=[Outcome(name="Up", price=0.60, chance=0.60, token_id="1",
                          market_outcome_id=11, winner=None),
                  Outcome(name="Down", price=0.41, chance=0.41, token_id="2",
                          market_outcome_id=12, winner=None)])


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = Store(pathlib.Path(d) / "t.db")
        yield s
        s.close()


@pytest.fixture
def armed(monkeypatch):
    for k, v in (("cross_enabled", True), ("cross_look_s", 1.0),
                 ("cross_min_tau_s", 5.0), ("cross_max_tau_s", 180.0),
                 ("cross_max_price", 0.65), ("cross_min_price", 0.10),
                 ("cross_max_per_round", 1), ("cross_hold_s", 0.0)):
        monkeypatch.setattr("btcpredict.config.settings." + k, v)


def _bot(store):
    """`_ticks` carries BOTH the level's reference minute (the 60 s before the
    open, all at LEVEL) and the round's own tape ending in the live wick."""
    r = _round()
    now = int(time.time() * 1000)
    ref = [(r.start_ms - (60 - i) * 1000, LEVEL) for i in range(60)]
    live = ([(now - (26 - i) * 1000, LEVEL - 5.0) for i in range(25)]
            + [(now, LEVEL + 2.6)])
    feed = FakeFeed([(now - 3000, LEVEL - 5.0), (now - 2000, LEVEL - 5.0),
                     (now, LEVEL + 2.6)])
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
    bot._asks = (0.60, 0.41)
    bot._sigma = 0.00044
    bot._cross_fired = {}
    bot._cross_day = ""
    bot._cross_count = 0
    bot._order_cooldown_until = 0.0
    bot._ticks = {"BTCUSDT": ref + live}
    bot.guard = RiskGuard(daily_loss_cap=0.0, max_consecutive_losses=5,
                          max_open_rounds=1, daily_loss_cap_pct=0.20,
                          bankroll=lambda: 50.0)
    return bot


def _run(bot):
    calls = []

    async def place(*a, **k):
        calls.append(k)
        return None

    bot._place_order = place
    asyncio.run(bot._cross_tick())
    return calls


class TestTheArmUsesIt:
    def test_the_live_wick_does_NOT_reach_the_order_path(self, store, armed,
                                                         monkeypatch):
        monkeypatch.setattr("btcpredict.config.settings.cross_hold_s", 3.0)
        bot = _bot(store)
        assert bot._cross_level(bot._round)[0] == pytest.approx(LEVEL, rel=1e-6)
        assert not _run(bot), (
            "a one-second print bought the side -- this is the -$1.05 entry")
        assert bot.store.counts()["decisions"] == 0

    def test_without_the_hold_the_same_tape_fires(self, store, armed):
        """A guard on the test above: the tape is the live one, so if the hold
        were doing nothing the entry would still go out."""
        bot = _bot(store)
        assert _run(bot), "hold 0 must still fire on this tape"

    def test_the_config_defaults_to_off_until_measured(self, store, armed):
        """The mechanism ships with the value the sweep chose; `0` restores the
        old rule, and that is the revert path."""
        from btcpredict.config import settings as S
        f = S.model_fields["cross_hold_s"]
        assert f.default >= 0.0
        src = CFG.read_text()
        assert "cross_hold_s" in src and "CROSS_HOLD_S=0" in src, (
            "the revert path has to be in the record")

    def test_the_arm_passes_the_shipped_hold(self):
        src = SRC.read_text()
        assert 'hold_s=self._ck("hold_s", r)' in src, (
            "the trigger must read the knob, not a literal")
