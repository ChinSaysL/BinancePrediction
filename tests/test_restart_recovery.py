"""A restart must not orphan an open position."""
import asyncio
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from btcpredict.execution.paper import PaperBroker
from btcpredict.main import Bot
from btcpredict.store.db import Store


def make_bot(store):
    bot = Bot.__new__(Bot)                      # skip network setup
    bot.mode = "paper"
    bot.store = store
    bot.open_fills = {}
    # settle_due moves this ledger by the settlement P&L (see _reconcile_bankroll);
    # Bot.__init__ always sets it, and these tests bypass __init__.
    bot.bankroll = 100.0
    bot._bankroll_drift = 0
    from btcpredict.risk.guards import RiskGuard
    bot.guard = RiskGuard(daily_loss_cap=10.0, max_consecutive_losses=5,
                          max_open_rounds=1)
    bot.broker = PaperBroker()
    from btcpredict.calibration.report import PnLSummary
    bot.pnl = PnLSummary()
    return bot


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = Store(Path(d) / "t.db")
        yield s
        s.close()


def test_unsettled_fill_is_restored(store):
    fid = store.insert("fills", {
        "decision_id": 1, "slug": "btc-updown-5m-1", "ts_ms": 1000, "mode": "paper",
        "side": "Up", "requested_px": 0.60, "filled_px": 0.62, "shares": 1.6129,
        "stake": 1.0, "fee_paid": 0.02, "order_id": "paper-1", "raw": "",
    })
    bot = make_bot(store)
    bot._restore_open_positions()

    # Keyed by fill id now, so two fills in one round cannot collide.
    assert fid in bot.open_fills
    restored_slug, fill = bot.open_fills[fid]
    assert restored_slug == "btc-updown-5m-1"
    assert fill.side == "Up"
    assert fill.shares == pytest.approx(1.6129)
    assert fill.stake == pytest.approx(1.0)
    # The guard must also know the round is occupied again.
    assert not bot.guard.check("btc-updown-5m-1")[0]


def test_already_settled_fill_is_not_restored(store):
    fid = store.insert("fills", {
        "decision_id": 1, "slug": "done", "ts_ms": 1000, "mode": "paper",
        "side": "Up", "requested_px": 0.6, "filled_px": 0.62, "shares": 1.6,
        "stake": 1.0, "fee_paid": 0.02, "order_id": "p1", "raw": "",
    })
    store.insert("settlements", {"fill_id": fid, "slug": "done", "ts_ms": 2000,
                                 "won": 1, "payout": 1.6, "pnl": 0.58, "mode": "paper"})
    bot = make_bot(store)
    bot._restore_open_positions()
    assert bot.open_fills == {}


def test_other_modes_are_not_restored(store):
    store.insert("fills", {
        "decision_id": 1, "slug": "live-one", "ts_ms": 1000, "mode": "live",
        "side": "Up", "requested_px": 0.6, "filled_px": 0.62, "shares": 1.6,
        "stake": 1.0, "fee_paid": 0.02, "order_id": "L1", "raw": "",
    })
    bot = make_bot(store)          # paper broker
    bot._restore_open_positions()
    assert bot.open_fills == {}    # a live fill must not be settled by the paper broker


def test_restored_position_then_settles(store):
    store.insert("fills", {
        "decision_id": 1, "slug": "r1", "ts_ms": 1000, "mode": "paper",
        "side": "Up", "requested_px": 0.6, "filled_px": 0.62, "shares": 1.6129,
        "stake": 1.0, "fee_paid": 0.02, "order_id": "p1", "raw": "",
    })
    store.upsert_round({"slug": "r1", "start_ms": 0, "end_ms": 1,
                        "market_topic_id": 99, "winner": "Up"})
    bot = make_bot(store)
    bot._restore_open_positions()
    asyncio.run(bot.settle_due())

    assert bot.open_fills == {}
    rows = store.query("SELECT * FROM settlements WHERE slug='r1'")
    assert len(rows) == 1 and rows[0]["won"] == 1
    assert bot.guard.check("r2")[0]      # the round slot is free again


def test_guard_seeding_respects_persisted_reset(store):
    # A --resume marker must survive restarts: settlements before the marker
    # are ignored, so a reset day does not re-halt on the next boot.
    now_ms = int(time.time() * 1000)
    for slug, ts, pnl in (("old-loss", now_ms - 60_000, -50.0),
                          ("fresh", now_ms, 10.0)):
        fid = store.insert("fills", {
            "decision_id": 1, "slug": slug, "ts_ms": ts, "mode": "live",
            "side": "Up", "requested_px": 0.6, "filled_px": 0.62, "shares": 1.6,
            "stake": 1.0, "fee_paid": 0.02, "order_id": slug, "raw": "",
        })
        store.insert("settlements", {"fill_id": fid, "slug": slug, "ts_ms": ts,
                                     "won": 0, "payout": 0, "pnl": pnl,
                                     "mode": "live"})
    store.set_meta("guard_reset_ms", str(now_ms - 30_000))

    bot = make_bot(store)
    bot.mode = "live"
    bot.broker = SimpleNamespace(mode="live")
    bot._seed_guard_state()

    assert bot.guard.state.daily_pnl == pytest.approx(10.0)


def test_guard_seeding_without_reset_marker_counts_all(store):
    # Without a marker the old loss is counted and the guard refuses to trade.
    now_ms = int(time.time() * 1000)
    fid = store.insert("fills", {
        "decision_id": 1, "slug": "old-loss", "ts_ms": now_ms, "mode": "live",
        "side": "Up", "requested_px": 0.6, "filled_px": 0.62, "shares": 1.6,
        "stake": 1.0, "fee_paid": 0.02, "order_id": "old-loss", "raw": "",
    })
    store.insert("settlements", {"fill_id": fid, "slug": "old-loss",
                                 "ts_ms": now_ms, "won": 0, "payout": 0,
                                 "pnl": -50.0, "mode": "live"})
    bot = make_bot(store)
    bot.mode = "live"
    bot.broker = SimpleNamespace(mode="live")
    bot._seed_guard_state()

    assert bot.guard.state.daily_pnl == pytest.approx(-50.0)
    assert not bot.guard.check("some-round")[0]


def _add_live_loss(store, slug, ts_ms, pnl=-1.0):
    fid = store.insert("fills", {
        "decision_id": 1, "slug": slug, "ts_ms": ts_ms, "mode": "live",
        "side": "Up", "requested_px": 0.6, "filled_px": 0.62, "shares": 1.6,
        "stake": 1.0, "fee_paid": 0.02, "order_id": slug, "raw": "",
    })
    store.insert("settlements", {"fill_id": fid, "slug": slug, "ts_ms": ts_ms,
                                 "won": 0, "payout": 0, "pnl": pnl,
                                 "mode": "live"})


def test_guard_seeding_counts_only_today_streak(store):
    """Yesterday's losses must not halt a fresh day: the streak is day-scoped
    exactly like the daily budget."""
    now_ms = int(time.time() * 1000)
    yesterday = now_ms - 26 * 3600 * 1000
    for i in range(5):
        _add_live_loss(store, f"yesterday-{i}", yesterday + i * 60_000)
    _add_live_loss(store, "today", now_ms)

    bot = make_bot(store)
    bot.mode = "live"
    bot.broker = SimpleNamespace(mode="live")
    bot._seed_guard_state()

    assert bot.guard.state.consecutive_losses == 1      # not 6
    assert bot.guard.check("fresh")[0]                  # so trading continues


def test_guard_seeding_halts_on_a_streak_still_running_today(store):
    """The flip side: a restart mid-streak must not launder it."""
    now_ms = int(time.time() * 1000)
    for i in range(5):                                  # make_bot halts at 5
        _add_live_loss(store, f"live-{i}", now_ms - (5 - i) * 60_000)

    bot = make_bot(store)
    bot.mode = "live"
    bot.broker = SimpleNamespace(mode="live")
    bot._seed_guard_state()

    assert bot.guard.state.consecutive_losses == 5
    allowed, why = bot.guard.check("fresh")
    assert not allowed and "consecutive" in why
    # ...and it expires with the day rather than needing a manual resume.
    bot.guard.state._day = "1999-01-01"
    assert bot.guard.check("tomorrow")[0]
