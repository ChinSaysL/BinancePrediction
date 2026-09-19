"""EXIT-ON-FLIP: sell the position when the trigger that opened it turns.

WHY THIS EXISTS. Measured on the PM era (`tools/pm_exit_flip.py`, 766-768
entries): the trigger reads the other side before the bell on **62.9%** of
entries, at a median of **1.0 s** after the entry, and the two cohorts are not
close -- P(win | turned) 42.2% against 94.4% for the ones that never turn.
Exiting into the bid there moves the cohort from +0.0773 to +0.1372 per share
(t +4.32 -> +13.69) and it survives a 1.0 bps floor (+0.1389 -> +0.2083).

WHAT THESE TESTS PIN, in the order the failures would hurt:
  * the flag is SHUT by default -- this is the first sell path in the repo;
  * the flip must PERSIST (`cross_exit_confirm_s`) -- a 1 Hz tape oscillates
    across the level by design, and the entry itself fires on that;
  * no exit inside `cross_exit_min_tau_s`, where the book is one-sided;
  * no exit without a readable BID, because `min_px` is derived from it and it
    is the only risk bound on the order;
  * the sale is booked as a SETTLEMENT ROW for the entry's fill, which is what
    stops `_open_cross_stake` counting the position and stops `settle_due`
    paying it out a second time against the round's winner;
  * an UNVERIFIED sale HALTS the watcher rather than risking a second sell of
    a position that may already be gone;
  * a partial sale splits the fill, so no row ever represents shares that no
    longer exist.
"""
from __future__ import annotations

import asyncio
import pathlib
import time
from types import SimpleNamespace

import pytest

from btcpredict.main import Bot

ROOT = pathlib.Path(__file__).resolve().parents[1]
SLUG = "btc-updown-5m-1789730400"


class FakeBroker:
    """Records the sell it was asked for, and answers with a Fill."""
    mode = "live"
    last_failure = None

    def __init__(self, shares=2.0, px=0.54, fee=0.017, unverified=False,
                 result=True, pending=False, confirm=None):
        self.calls = []
        self.confirms = []
        self._shares, self._px, self._fee = shares, px, fee
        self._unverified, self._result = unverified, result
        self._pending, self._confirm = pending, confirm

    async def sell_fak(self, *, slug, side, token_id, shares, min_px=None,
                       slip=0.03, quote=None):
        self.calls.append(dict(slug=slug, side=side, token=token_id,
                               shares=shares, min_px=min_px, slip=slip))
        if self._result is False:
            return None
        from btcpredict.venues.polymarket.broker import Fill
        if self._pending:
            return Fill(slug=slug, side=side, requested_px=min_px,
                        filled_px=self._px, shares=0.0, stake=0.0, fee_paid=0.0,
                        order_id="x1", ts_ms=int(time.time() * 1000),
                        token_id=token_id, raw="test pending", pending=True)
        return Fill(slug=slug, side=side, requested_px=min_px,
                    filled_px=self._px, shares=self._shares,
                    stake=self._shares * self._px, fee_paid=self._fee,
                    order_id="x1", ts_ms=int(time.time() * 1000),
                    token_id=token_id, raw="test", unverified=self._unverified)

    def confirm_sale(self, *, token_id, shares, since_ts, fallback_px=0.0):
        """Answers ONCE, then None -- so a test can watch the state before and
        after the confirmation."""
        self.confirms.append(dict(token=token_id, shares=shares,
                                  since_ts=since_ts, fallback_px=fallback_px))
        got, self._confirm = self._confirm, None
        return got


class FakeStore:
    def __init__(self):
        self.rows = []
        self.conn = SimpleNamespace(execute=lambda *a, **k: None,
                                    commit=lambda: None)

    def insert(self, table, row):
        self.rows.append((table, dict(row)))
        return len(self.rows)

    def query(self, *a, **k):
        return []


class FakeGuard:
    def __init__(self):
        self.settled = []
        self.released = []
        self.opened = []

    def on_settle(self, slug, pnl, reserved=False, pool=None):
        self.settled.append((slug, pnl))

    def on_open(self, slug, *, reserved=False, pool=None):
        self.opened.append(slug)

    def release(self, slug, reserved=False, pool=None):
        self.released.append(slug)


class FakePnl:
    def __init__(self):
        self.added = []

    def add(self, pnl, stake, fee, won):
        self.added.append((pnl, stake, fee, won))


class FakeTop:
    def __init__(self, bid):
        self._bid = bid

    def bid(self, side):
        return self._bid


class FakeWsb:
    def __init__(self, bid):
        self._bid = bid

    def best(self, r, age):
        return FakeTop(self._bid) if self._bid else None


def _bot(*, bid=0.55, sell_shares=2.0, sell_px=0.54, unverified=False,
         sell_result=True, enabled=True, monkeypatch=None, tau_s=120.0,
         pending=False, confirm=None):
    held_side, shares, stake, fee = "Up", 2.0, 1.14, 0.03
    b = Bot.__new__(Bot)
    b.mode = "live"
    b.bankroll = 10.0
    b.broker = FakeBroker(shares=sell_shares, px=sell_px,
                          unverified=unverified, result=sell_result,
                          pending=pending, confirm=confirm)
    b.store = FakeStore()
    b.guard = FakeGuard()
    b.pnl = FakePnl()
    b._wsb = FakeWsb(bid) if bid is not None else None
    # A REAL Fill, not a bare namespace: `settle_due` settles from this object
    # and a partial sale must hand the remainder its own copy with the
    # remainder's sizes (see TestThePartialSplitSettlesItsOwnSize).
    from btcpredict.venues.polymarket.broker import Fill as _F
    b.open_fills = {7: (SLUG, _F(slug=SLUG, side=held_side, requested_px=0.65,
                                 filled_px=0.57, shares=shares, stake=stake,
                                 fee_paid=fee, order_id="o1",
                                 ts_ms=int(time.time() * 1000),
                                 token_id="tok", raw="entry"))}
    b._exit_watch = {SLUG: {
        "fid": 7, "side": held_side, "token": "tok", "shares": shares,
        # 60 s ago, not 2 s: `cross_exit_min_hold_s` (5.0) now refuses to judge
        # a position younger than that, so a fixture entered 2 s ago would make
        # every rule test assert the HOLD rather than the rule. The hold has its
        # own behavioural test below, with a deliberately fresh entry.
        "stake": stake, "fee": fee, "entry_ms": int(time.time() * 1000) - 60_000,
        "did": 3, "px": 0.57, "req": 0.65, "oid": "o1",
        "flip_ms": None, "done": 0, "halted": False}}
    b._ck = lambda name, r: {"look_s": 1.0}.get(name, 0.0)
    b._fill_mode = lambda fid: "cross"
    b._cross_refuse = lambda r, side, why, **k: b.store.rows.append(
        ("decisions", {"reason": why}))
    b._no_strike_seen = set()
    return b


def _round(tau_s: float = 120.0):
    return SimpleNamespace(slug=SLUG, strike=100.0,
                           end_ms=int(time.time() * 1000 + tau_s * 1000))


def _tape(side: str, level: float = 100.0, n: int = 3):
    """A tape `crossed_side` reads as a crossing into `side`.

    The rule needs a tick at least `look_s` (1 s) OLDER than the newest, no more
    than `look_s + slack_s` (3 s) old, sitting on the OTHER side -- so the
    timestamps have to be real milliseconds. A tape spanning 4 ms returns None
    whatever the prices do.
    """
    lo, hi = level - 0.5, level + 0.5
    px_side, px_other = (hi, lo) if side == "Up" else (lo, hi)
    t = 1_000_000
    return [(t, px_other), (t + 1500, px_side), (t + 2000, px_side)], px_side


def _quiet(level: float = 100.0, side: str = "Down"):
    """A tape with NO CROSSING -- every tick already settled on `side`.

    ⛔ THIS IS THE SHAPE OF A SUSTAINED FLIP, and it is the case the exit
    exists for. `crossed_side` reports a crossing EVENT, so once the signal has
    moved to the other side and STAYED there it returns None forever: the live
    btc-updown-5m-1789750800 had exactly ONE opposite reading and then sat
    there while the position went 0.64 -> 0.07. A fire rule that needs a second
    crossing can never act on it, which is why the confirmation is a STATE
    question and only the arming is an event.

    `side` is the side the signal SITS on, so `_quiet(side="Down")` while
    holding Up is "the flip is still on" and `_quiet(side="Up")` is "it died".
    """
    t = 1_000_000
    px = (level + 0.5) if side == "Up" else (level - 0.5)
    return [(t, px), (t + 1500, px), (t + 2000, px)], px


def _run(b, r, sig_side, level=100.0):
    ticks, _ = _tape(sig_side, level)
    b._cross_signal = lambda r, t, tau: (ticks, "spot")
    # `asyncio.run`, not `get_event_loop()`: 3.14 has no current-loop
    # fallback on the main thread and the latter raises RuntimeError.
    return asyncio.run(b._cross_exit_check(r, level, ticks))


def _quiet_run(b, r, level=100.0, side="Down"):
    ticks, _ = _quiet(level, side)
    b._cross_signal = lambda r, t, tau: (ticks, "spot")
    return asyncio.run(b._cross_exit_check(r, level, ticks))


def _fire(b, r, sig_side="Down", level=100.0):
    """Arm on one opposite reading, then fire on the next.

    The rule is "the FLIP persisted `confirm_s`", so it takes two readings by
    construction: the first stamps `flip_ms`, the second fires once the window
    has run. Tests age `flip_ms` rather than sleeping.
    """
    first = _run(b, r, sig_side, level)
    if first is True:
        return True
    w = b._exit_watch.get(SLUG)
    if w is not None and w.get("flip_ms"):
        w["flip_ms"] -= 10_000          # the flip has now persisted
    return _run(b, r, sig_side, level)


class TestTheFlag:
    def test_the_exit_is_shut_by_default(self):
        """⛔ THE FIELD DEFAULT, NOT `Settings()`. Instantiating resolves the
        `.env`, so this assertion used to pass only on machines that happened
        not to enable the exit -- it could not tell "we ship it off" from "this
        box has it off", and it failed the moment a deploy turned it on. The
        claim being guarded is about the SHIPPED default, which is env-free.
        The first sell path in this repo must ship off, like
        cross_train_enabled and every other new order path."""
        from btcpredict.config import Settings
        assert Settings.model_fields["cross_exit_enabled"].default is False

    def test_nothing_sells_while_it_is_shut(self, monkeypatch):
        b = _bot(monkeypatch=monkeypatch)
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            False)
        r = _round()
        for _ in range(4):
            assert _run(b, r, "Down") is False
        assert b.broker.calls == []

    def test_the_knobs_ride_with_the_flag(self):
        cfg = (ROOT / "btcpredict" / "config.py").read_text()
        for frag in ("cross_exit_confirm_s", "cross_exit_min_tau_s",
                     "cross_exit_slip", "pm_exit_flip.py"):
            assert frag in cfg, frag


class TestTheFlipMustPersist:
    def test_one_print_does_not_sell(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        b = _bot(monkeypatch=monkeypatch)
        r = _round()
        # First reading arms the watcher; the confirmation window has not run.
        assert _run(b, r, "Down") is False
        assert b.broker.calls == []
        assert b._exit_watch[SLUG]["flip_ms"] is not None

    def test_a_flip_that_goes_quiet_still_fires(self, monkeypatch):
        """THE REGRESSION THIS FILE EXISTS FOR (live 2026-09-18 12:52).

        `crossed_side` is an EVENT detector: the tick after a crossing returns
        None while the spot still sits on that side. The first version cleared
        the latch on that None, so the flip could never be held for confirm_s --
        the watcher armed SIX times on three positions and sold NOTHING while
        they went to the bell and lost. A quiet tick must not disarm.
        """
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        b = _bot(monkeypatch=monkeypatch)          # confirm_s = 3.0 (default)
        r = _round()
        assert _run(b, r, "Down") is False, "the first reading only arms it"
        arm_ms = b._exit_watch[SLUG]["flip_ms"]
        assert arm_ms is not None, "the first opposite reading arms it"
        for _ in range(4):
            _quiet_run(b, r)          # no new crossing; the signal SITS on Down
        assert b._exit_watch[SLUG]["flip_ms"] == arm_ms, (
            "a sustained flip produces no further crossing EVENT -- that must "
            "not clear the arming")
        assert b.broker.calls == []
        # ...and it fires on a QUIET tick once the flip has persisted, with no
        # second crossing ever arriving. This is the live btc-1789750800 case.
        b._exit_watch[SLUG]["flip_ms"] -= 10_000
        assert _quiet_run(b, r) is True, (
            "a sustained flip must sell without a second crossing event")
        assert len(b.broker.calls) == 1
    def test_a_persistent_flip_sells_once(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_confirm_s",
                            0.0)
        b = _bot(monkeypatch=monkeypatch)
        r = _round()
        assert _fire(b, r) is True
        assert len(b.broker.calls) == 1
        c = b.broker.calls[0]
        assert c["side"] == "Up" and c["shares"] == 2.0 and c["token"] == "tok"
        assert c["min_px"] is None, (
            "the arm passes no floor -- the broker derives it from its own bid "
            "(the 13:08 refusal was the arm reading a second book)")
        assert c["slip"] == pytest.approx(0.03)
        # ...and it does not sell twice.
        assert _run(b, r, "Down") is False
        assert len(b.broker.calls) == 1
        assert SLUG not in b._exit_watch, "a closed position stops being watched"


class TestTheSellCannotInventAPosition:
    """THE AUDIT FINDINGS (2026-09-18, after the armed-but-did-not-sell bug).

    The dangerous shape is a FAILED READBACK becoming a completed sale:
    `_position_size` answers 0.0 for an unreadable position, so `sold = held -
    left` reads as "the whole position sold" -- and the caller books a
    settlement row, releases the slot and drops the fill, while the shares are
    still on-chain outside every cap. Same class as the 2026-09-17 incident
    (`an unreadable readback is not an unfilled order`), inverted.
    """

    def test_unreadable_is_not_zero_any_more(self):
        import inspect
        import btcpredict.venues.polymarket.broker as B
        src = inspect.getsource(B.PmBroker._position_size_checked)
        assert "return None" in src, (
            "the sell path needs a question that can say 'I do not know'")
        sink = inspect.getsource(B.PmBroker._sell_fak_body)
        assert "_position_size_checked" in sink
        assert sink.count("_position_size(") == 0, (
            "the sell body must never use the 0.0-for-unreadable read")

    def test_a_failed_after_read_is_PENDING_not_a_halt(self):
        """The flag CHANGED from `unverified` (halt) to `pending` (keep asking)
        on 2026-09-18. The contract underneath is the same -- NOTHING is booked
        from an unread read -- but the caller no longer halts the position into
        limbo: `confirm_sale` is asked again on later ticks, which is what
        closes a sale the venue's position list was simply behind on.
        """
        b = _sell_bot(positions=[3.0, None])
        fill = b._sell_fak_body(slug=SLUG, side="Up", token_id="tok",
                                shares=3.0, min_px=0.50)
        assert fill is not None and fill.pending is True
        assert fill.unverified is False, "pending is not 'we do not know'"
        assert fill.shares == 0.0, "nothing may be booked from a failed read"
        assert b.last_failure == "pending_confirm"

    def test_the_sale_is_sized_from_the_venue_not_our_ledger(self):
        # We believe 3.0 shares; the venue holds 2.0. A sell of 3.0 would fill
        # only 2.0 and then report `3.0 - 0.0` = a 3.0-share sale.
        b = _sell_bot(positions=[2.0, 0.0])
        fill = b._sell_fak_body(slug=SLUG, side="Up", token_id="tok",
                                shares=3.0, min_px=0.50)
        assert b.asked == 2.0, "the order must be sized to the real holding"
        assert fill.shares == 2.0

    def test_an_unreadable_position_STILL_ORDERS_from_the_ledger(self):
        """⛔ REVERSED ON PURPOSE (2026-09-18). The old rule -- no positive
        position read, no order -- made the FAST flips unsellable. Measured
        live: the venue's list showed 0 shares 4 s after a CONFIRMED buy and
        1.9607 only 60 s later, while the replay puts the median flip 1.0 s
        after the entry. The list is now a CAP on the size, never a permission;
        an order for shares we do not hold comes back `fak_unfilled`.
        """
        b = _sell_bot(positions=[None, None])
        fill = b._sell_fak_body(slug=SLUG, side="Up", token_id="tok",
                                shares=3.0, min_px=0.50)
        assert b.asked == 3.0, "the ledger is the authority for the size"
        assert fill is not None and fill.pending is True

    def test_the_trade_record_confirms_a_sale_the_list_missed(self):
        """The venue's own trades, windowed from the ORDER and not the entry,
        so our own buy cannot be read back as the sale -- and they carry the
        real price and fee.
        """
        b = _sell_bot(positions=[3.0] * 12, trades=(3.0, 0.42, 0.0107))
        fill = b._sell_fak_body(slug=SLUG, side="Up", token_id="tok",
                                shares=3.0, min_px=0.50)
        assert fill is not None and fill.shares == 3.0
        assert fill.filled_px == pytest.approx(0.42), "the venue's vwap"
        assert fill.fee_paid == pytest.approx(0.0107)
        assert fill.pending is False

    def test_confirm_sale_reads_the_position_first_then_the_trades(self):
        # a position that DROPPED is a sale even with no trade read at all
        b = _sell_bot(positions=[1.0], trades=None)
        got = b.confirm_sale(token_id="tok", shares=3.0, since_ts=0.0)
        assert got is not None and got[0] == pytest.approx(2.0)
        # and a position that has not moved falls back to the trade record
        b = _sell_bot(positions=[3.0], trades=(3.0, 0.5, 0.02))
        got = b.confirm_sale(token_id="tok", shares=3.0, since_ts=0.0)
        assert got == (pytest.approx(3.0), pytest.approx(0.5),
                       pytest.approx(0.02))
        # nothing visible either way -> still unknown, ask again later
        b = _sell_bot(positions=[3.0], trades=None)
        assert b.confirm_sale(token_id="tok", shares=3.0, since_ts=0.0) is None


def _sell_bot(*, positions, bid=0.55, trades=None):
    """A PmBroker body with a scripted position list and no network."""
    from btcpredict.venues.polymarket.broker import PmBroker, PmBrokerConfig
    b = PmBroker.__new__(PmBroker)
    b.cfg = PmBrokerConfig()
    b.cfg.confirm_budget_s = 0.25      # keep these tests fast; the real one is 2.5
    b.asked = None
    b.last_failure = None
    b.last_order_error = None
    b.unverified = 0
    b.venue_calls = 0
    seq = list(positions)
    b._position_size_checked = lambda tok: (seq.pop(0) if seq else 0.0)
    b._trades_since = lambda tok, since: trades
    b.round_for = lambda slug: None
    b.warm_now = lambda tok: None
    b._log = lambda *a, **k: None
    b._now = lambda: 1789735800.0

    class _Book:
        def best(self, token_id, age):
            return None if bid is None else SimpleNamespace(bid=bid)

    class _Client:
        def place_market_order(self, *, token_id, side, shares, min_price,
                               order_type):
            b.asked = shares
            b.side_sent = side
            b.floor_sent = min_price
            return {"ok": True, "order_id": "o1", "status": "matched"}

    b.book = _Book()
    b._ensure_client = lambda: _Client()
    return b


class TestTheGuardsThatProtectTheOrder:
    def test_no_exit_inside_the_last_seconds(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_confirm_s",
                            0.0)
        b = _bot(monkeypatch=monkeypatch)
        r = _round(tau_s=3.0)                  # inside cross_exit_min_tau_s
        assert _run(b, r, "Down") is False
        assert b.broker.calls == []

    def test_no_bid_means_no_sale(self):
        """⛔ THE FLOOR COMES FROM THE BROKER'S OWN BOOK, NOT THE ARM'S.

        Live 2026-09-18 13:08:51: the watcher FIRED and then refused with
        `exit: no fresh bid to sell into`, because the arm read a second book
        to compute `min_px`. The broker's book is the live PM feed; when that
        one is unreadable the broker refuses, and nothing is sent.
        """
        b = _sell_bot(positions=[2.0], bid=None)
        fill = b._sell_fak_body(slug=SLUG, side="Up", token_id="tok",
                                shares=2.0, slip=0.03)
        assert fill is None and b.asked is None
        assert b.last_failure == "no_fresh_book"

    def test_the_broker_derives_the_floor_from_its_own_bid(self):
        b = _sell_bot(positions=[2.0], bid=0.55)
        b._sell_fak_body(slug=SLUG, side="Up", token_id="tok",
                         shares=2.0, slip=0.03)
        assert b.floor_sent == pytest.approx(0.52), "bid - slip, on the tick grid"

    def test_no_bid_means_no_sale_main_side(self):
        """The arm no longer owns a bid check at all: one source of truth."""
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_exit_execute)
        assert "wsb.best" not in src, (
            "reading a second book is what refused the 13:08 exit")
        assert "slip=settings.cross_exit_slip" in src

    def test_an_unverified_sale_halts_the_watcher(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_confirm_s",
                            0.0)
        b = _bot(unverified=True, sell_shares=0.0, monkeypatch=monkeypatch)
        r = _round()
        _fire(b, r)
        assert len(b.broker.calls) == 1
        assert b._exit_watch[SLUG]["halted"] is True, (
            "the position may already be gone -- a second sell would be naked")
        assert _run(b, r, "Down") is False
        assert len(b.broker.calls) == 1

    def test_a_failed_sale_retries_next_tick(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_confirm_s",
                            0.0)
        b = _bot(sell_result=False, monkeypatch=monkeypatch)
        r = _round()
        _fire(b, r)
        assert len(b.broker.calls) == 1
        assert b._exit_watch[SLUG]["halted"] is False
        assert b.store.rows == [], "a no-fill books nothing"


class TestTheLedger:
    def test_the_sale_closes_the_position_in_every_consumer(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_confirm_s",
                            0.0)
        b = _bot(monkeypatch=monkeypatch)
        r = _round()
        _fire(b, r)
        settled = [row for tbl, row in b.store.rows if tbl == "settlements"]
        assert len(settled) == 1
        s = settled[0]
        assert s["fill_id"] == 7 and s["mode"] == "cross" and s["void"] == 0
        assert s["payout"] == pytest.approx(2.0 * 0.54 - 0.017)
        assert s["pnl"] == pytest.approx(s["payout"] - (1.14 + 0.03))
        assert 7 not in b.open_fills, (
            "_open_cross_stake counts fills with NO settlements row, and "
            "settle_due iterates open_fills -- both must stop seeing this fill")
        assert b.guard.settled and b.guard.settled[0][0] == SLUG
        assert b.bankroll == pytest.approx(10.0 + s["pnl"])
        assert b.pnl.added and b.pnl.added[0][0] == pytest.approx(s["pnl"])

    def test_a_partial_sale_splits_the_fill(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_confirm_s",
                            0.0)
        b = _bot(sell_shares=0.5, monkeypatch=monkeypatch)
        r = _round()
        _fire(b, r)
        fills = [row for tbl, row in b.store.rows if tbl == "fills"]
        assert len(fills) == 1, "the remainder must exist as its own row"
        assert fills[0]["shares"] == pytest.approx(1.5)
        assert fills[0]["stake"] == pytest.approx(1.14 * 1.5 / 2.0)
        w = b._exit_watch[SLUG]
        assert w["shares"] == pytest.approx(1.5) and w["fid"] in b.open_fills

    def test_a_partial_sale_keeps_the_slot_it_still_needs(self, monkeypatch):
        """AUDIT: `on_settle` releases the guard slot. On a PARTIAL sale the
        remainder is still open, so releasing it lets the arm open another
        position against a bound it believes it has freed."""
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_confirm_s",
                            0.0)
        b = _bot(sell_shares=0.5, monkeypatch=monkeypatch)
        r = _round()
        _fire(b, r)
        assert b.guard.settled, "the realised P&L must still be recorded"
        assert SLUG in b.guard.opened, (
            "the slot must be re-claimed for the shares still held")
        assert SLUG in b._exit_watch, "and the remainder stays watched"

    def test_a_full_sale_gives_the_slot_back_once(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_confirm_s",
                            0.0)
        b = _bot(monkeypatch=monkeypatch)
        r = _round()
        _fire(b, r)
        assert b.guard.opened == [], "nothing is still held, so nothing re-claims"
        assert SLUG not in b._exit_watch


class TestTheWatcherIsWiredIntoTheTick:
    def test_it_runs_before_the_per_round_cap(self):
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_tick)
        assert "_cross_exit_check" in src
        assert src.index("_cross_exit_check") < src.index(
            "'max_per_round', r)"), (
            "a held position already has max_per_round spent, so the cap "
            "returns first and a watcher after it never sees the tick")

    def test_a_broker_that_cannot_sell_gets_no_watcher(self):
        import inspect
        import btcpredict.main as m
        # The guards live in ONE place now (the audit found the booking and
        # restore paths could drift), so this asserts the helper and that both
        # call sites go through it.
        arm = inspect.getsource(m.Bot._arm_exit_watch)
        assert 'hasattr(self.broker, "sell_fak")' in arm
        assert "cross_exit_enabled" in arm
        for src in (inspect.getsource(m.Bot._book_fill),
                    inspect.getsource(m.Bot._restore_open_positions)):
            assert "_arm_exit_watch" in src, (
                "every path that opens a position must arm the watcher -- the "
                "restore path had no registration at all until the audit")


class TestTheSaleTheVenueWasSlowToShow:
    """⛔ THE LIVE FAILURE OF 2026-09-18, AND ITS FIX.

    Four exits fired and ALL FOUR SOLD at the venue (`pm SELL ... status
    matched ack ~500 ms`) -- and not one was booked, because the confirmation
    asked the position list, which lags a fill by 0.7-3.5 s and a fresh BUY by
    60 s. The old outcome was `unverified` -> halt, so nothing was written and
    `settle_due` later booked the round's result in the sale's place (or voided
    it), depending only on whether someone else held shares in that token.
    A matched-but-invisible sale is now PENDING: the position stays watched, no
    second order is sent, and `confirm_sale` books it when the venue catches up.
    """

    def _pending_bot(self, monkeypatch, **kw):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_confirm_s",
                            0.0)
        return _bot(monkeypatch=monkeypatch, pending=True, **kw)

    def test_a_matched_sale_is_pending_not_halted(self, monkeypatch):
        b = self._pending_bot(monkeypatch)
        assert _fire(b, _round(), "Down") is True
        w = b._exit_watch[SLUG]
        assert not w.get("halted"), "pending must not halt the watcher"
        assert w.get("pending"), "the placed sale is remembered for confirmation"
        assert w["done"] == 1, "the position's one order is spent"
        assert b.store.rows == [], "nothing is booked before the venue answers"

    def test_no_second_order_while_the_first_is_pending(self, monkeypatch):
        b = self._pending_bot(monkeypatch)
        _fire(b, _round(), "Down")
        _run(b, _round(), "Down")
        assert len(b.broker.calls) == 1, "a pending sale already spent the order"

    def test_a_late_confirmation_books_the_row(self, monkeypatch):
        b = self._pending_bot(monkeypatch, confirm=(2.0, 0.50, 0.017))
        _fire(b, _round(), "Down")                 # places -> pending
        assert _run(b, _round(), "Down") is True   # confirms -> books
        rows = [r for t, r in b.store.rows if t == "settlements"]
        assert len(rows) == 1, b.store.rows
        assert rows[0]["fill_id"] == 7
        assert rows[0]["payout"] == pytest.approx(2.0 * 0.50 - 0.017)
        assert rows[0]["pnl"] == pytest.approx(
            2.0 * 0.50 - 0.017 - (1.14 + 0.03))
        assert SLUG not in b._exit_watch, "a closed position stops being watched"
        assert len(b.broker.calls) == 1, "no second order, ever"

    def test_the_pending_check_runs_BEFORE_the_one_order_rail(self):
        """It has to: `cross_exit_max_per_position` would pop the watch first,
        and the confirmation could never run -- which is exactly how the live
        sales stayed unbooked."""
        import inspect
        import btcpredict.main as m
        src = inspect.getsource(m.Bot._cross_exit_check)
        assert (src.index('if w.get("pending"):')
                < src.index(">= settings.cross_exit_max_per_position")), \
            "the pending confirmation must precede the one-order rail"


class TestTheHysteresisBand:
    """⛔ THE DEFECT THIS CLASS EXISTS FOR (live 2026-09-18).

    The exit ran `crossed_side` with no `hold_s` and applied no margin floor,
    while the ENTRY applies both. So the buy threshold sat at `level + floor`
    and the sell threshold at `level + 0.00`: a position was opened `floor` bps
    past the level and closed by ANY tick back on it, however small and however
    brief. `crossed_side` fires on the FIRST tick past the level, so by
    construction every entry starts life sitting on it.

        sol-updown-5m-1789743000, consecutive lines in the live log:
          14:52:35  not taken: only -0.20 bps of its own margin, floor is 0.25
          14:52:36 / 14:52:42 / 14:52:43   the same refusal, four in all
          14:53:20.626  OPEN CROSS sol ... Down @0.540
          14:53:25.064  pm sell sol ... Down            <- 4.44 s later

    The round refused to OPEN four times on a move it then CLOSED on.
    """

    def _bot_with_floor(self, monkeypatch, floor=1.0):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        b = _bot(monkeypatch=monkeypatch)
        b._ck = lambda name, r: {"look_s": 1.0, "min_margin_bps": floor,
                                 "hold_s": 0.0}.get(name, 0.0)
        return b

    @staticmethod
    def _marginal(level=100.0, bps=0.1):
        """A REAL crossing into Down, `bps` past the level and no further.

        `_tape` puts the tick 0.5 away from a level of 100.0 -- that is 50 bps,
        which clears any floor. To exercise the floor the distance has to be
        set in the units the gate reads.
        """
        px = level * (1.0 - bps / 1e4)
        t = 1_000_000
        return [(t, level + 0.5), (t + 1500, px), (t + 2000, px)]

    def test_a_flip_inside_the_floor_does_not_sell(self, monkeypatch):
        """The move the ENTRY would refuse must not trigger the EXIT."""
        b = self._bot_with_floor(monkeypatch, floor=1.0)
        r = _round()
        ticks = self._marginal(bps=0.1)           # 0.1 bps, floor is 1.0
        b._cross_signal = lambda r, t, tau: (ticks, "spot")
        for _ in range(6):
            assert asyncio.run(b._cross_exit_check(r, 100.0, ticks)) is False
        assert b.broker.calls == [], (
            "a 0.1 bps reversal closed a position the arm would not have "
            "opened on 0.1 bps -- that is the zero-width trapdoor")
        # ⛔ IT MAY ARM -- IT MUST NOT SELL. The floor belongs on the STATE,
        # not on the crossing: `crossed_side` fires on the FIRST tick past the
        # level, so a crossing is zero-margin by construction and a floor on the
        # ARMING never arms at all (live 21:08:04, eth-1789765500: a real flip at
        # -0.30 bps against a 0.50 floor, never armed, 0.600 -> 0.00).
        assert b._exit_watch[SLUG]["flip_ms"] is not None, (
            "a crossing must arm -- the floor is applied to the fire, not here")

    def test_the_same_move_past_the_floor_does_arm(self, monkeypatch):
        """The control: a move that clears the floor arms AND can fire."""
        b = self._bot_with_floor(monkeypatch, floor=1.0)
        r = _round()
        ticks = self._marginal(bps=5.0)           # 5 bps, floor is 1.0
        b._cross_signal = lambda r, t, tau: (ticks, "spot")
        assert asyncio.run(b._cross_exit_check(r, 100.0, ticks)) is False
        assert b._exit_watch[SLUG]["flip_ms"] is not None, (
            "a move past the floor must arm")

    def test_a_flip_past_the_floor_sells(self, monkeypatch):
        """...and the same machinery still fires once the move is real."""
        b = self._bot_with_floor(monkeypatch, floor=0.1)
        assert _fire(b, _round(), "Down", level=100.0) is True
        assert len(b.broker.calls) == 1

    def test_the_exit_passes_hold_s_like_the_entry(self):
        """A LONE PRINT IS NOT A CROSSING -- the entry says so and passes
        `hold_s`; the exit must read the same knob rather than defaulting to
        the refuted 0.0 behaviour."""
        src = (ROOT / "btcpredict" / "main.py").read_text()
        i = src.index("async def _cross_exit_check")
        j = src.index("def _confirm_pending_sale", i)
        body = src[i:j]
        assert 'hold_s=self._ck("hold_s", r)' in body, (
            "the exit's crossed_side must take the entry's hold_s")
        assert 'self._ck("min_margin_bps", r)' in body, (
            "the exit must apply the entry's margin floor")


class TestTheConfirmationRunsFromTheFlip:
    """`flip_ms` was stamped and never read: the gate compared against
    `entry_ms + 1000`, so `cross_exit_confirm_s` meant "wait 4 s after the BUY,
    then leave on the first opposite tick" -- not "the flip persisted". Live,
    every exit fired at 4.30-4.44 s after its own entry, the floor exactly."""

    def test_an_old_entry_does_not_licence_an_instant_sale(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        b = _bot(monkeypatch=monkeypatch)             # confirm_s = 3.0
        r = _round()
        b._exit_watch[SLUG]["entry_ms"] -= 600_000    # entered ten minutes ago
        assert _run(b, r, "Down") is False, (
            "an ancient entry must not let the FIRST flip print sell -- that "
            "is the entry-clocked gate, and it is what shipped")
        assert b._exit_watch[SLUG]["flip_ms"] is not None
        assert b.broker.calls == []

    def test_the_clock_is_the_flip_not_the_entry(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        b = _bot(monkeypatch=monkeypatch)
        r = _round()
        assert _run(b, r, "Down") is False            # arms
        b._exit_watch[SLUG]["flip_ms"] -= 10_000      # the FLIP has persisted
        assert _run(b, r, "Down") is True
        assert len(b.broker.calls) == 1

    def test_a_reversal_back_to_our_side_kills_the_latch(self, monkeypatch):
        """A reading back on the side we HOLD is the flip dying -- a different
        event from a quiet tick, and the two must not be conflated."""
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        b = _bot(monkeypatch=monkeypatch)
        r = _round()
        assert _run(b, r, "Down") is False            # arms
        assert b._exit_watch[SLUG]["flip_ms"] is not None
        assert _run(b, r, "Up") is False              # crossed back to us
        assert b._exit_watch[SLUG]["flip_ms"] is None, (
            "the signal returned to the side we hold: the flip is over")
        assert b.broker.calls == []


class TestTheTauGateIsTheEdge:
    """⭐ The one side condition that separates losers from winners.

    `tools/pm_exit_explore.py` swept 24 rules -- the exit's own margin floor, a
    giveback clause, a draw-down clause, confirm_s and combinations. Only the
    tau gate survives on both samples: on the tape it takes the loser/winner
    value ratio from x1.56 to x2.23, and on the 95 live fills it cuts 6 losers
    (+4.185) and ZERO winners, where the shipped tau=8 cut 1 winner.
    """

    def test_the_gate_defaults_to_the_measured_value(self):
        """8.0 -> 60.0 on 2026-09-19, THE SAME NUMBER THAT WAS RETRACTED EARLIER
        THAT DAY, and it must not be mistaken for the retraction being ignored.

        The first 60.0 came from a sweep run on `pm_exit_flip.py:walk()`, which
        latched its exit candidate on the first tick after the entry -- so it
        could only ever fire on oscillating signals and priced every exit at
        roughly the entry price. That evidence is dead and stays dead.

        This 60.0 comes from the joint entry-x-exit search under the full live
        gate chain (`tools/pm_joint_exit.py`), where the winning cell is
        margin 4.0 at xmin 60: TEST $+45.39/$+42.14 against holding's
        $+27.00/$+22.19, and it beats holding at every sale completion rate
        down to 0.39. 8.0's own reason -- the book goes one-sided at the bell --
        is not contradicted; 60 is simply further from it.
        See config.cross_exit_min_tau_s."""
        from btcpredict.config import Settings
        assert Settings().cross_exit_min_tau_s == 60.0

    def test_a_flip_inside_the_gate_does_not_sell(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_min_tau_s",
                            60.0)
        b = _bot(monkeypatch=monkeypatch)
        r = _round(tau_s=45.0)                # inside the gate
        for _ in range(4):
            assert _run(b, r, "Down") is False
        assert b.broker.calls == [], (
            "inside a minute the book is one-sided and a late flip is noise "
            "about a round that is already decided")

    def test_a_flip_outside_the_gate_still_sells(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        b = _bot(monkeypatch=monkeypatch)
        assert _fire(b, _round(tau_s=150.0), "Down") is True
        assert len(b.broker.calls) == 1

    def test_the_exit_floor_is_not_raised_above_the_entrys(self):
        """⛔ REFUTED, and the sweep is quoted in the config: an exit floor
        above the entry's cuts MORE winner value than it saves (x0.66 at 1.0
        bps, x0.37 at 2.0, x0.02 at 3.0). The band is symmetric on purpose --
        `_cross_exit_check` reads the ENTRY's `min_margin_bps`, not a knob of
        its own, and adding one would re-open a measured dead end."""
        src = (ROOT / "btcpredict" / "main.py").read_text()
        i = src.index("async def _cross_exit_check")
        j = src.index("def _confirm_pending_sale", i)
        body = src[i:j]
        assert 'self._ck("min_margin_bps", r)' in body
        assert "cross_exit_min_margin" not in src, (
            "a separate exit floor was measured and refused; see "
            "config.cross_exit_min_tau_s")


class TestASustainedFlipSells:
    """⛔ THE LIVE REGRESSION THIS CLASS EXISTS FOR (btc-updown-5m-1789750800).

        ENTRY 17:02:09 Up @0.640 (margin +1.508 bps)
        17:02:12 tau 167  FLIP to Down (m -0.60)  ARMED
        ...one opposite reading, EVER. The spot sat on the Down side for the
        rest of the round (-1.45, -6.77, -8.70, -2.70, -5.22 bps) and the Up
        token went 0.64 -> 0.07 while the watcher waited for a second crossing.

    `crossed_side` is an EVENT detector, so a SUSTAINED flip -- the exact case
    the exit exists for -- produces exactly ONE reading. A fire rule that lives
    inside the "this tick is a crossing into the other side" branch can only
    fire on an OSCILLATING signal. The confirmation is therefore a STATE
    question; only the arming is an event.
    """

    def _armed(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        b = _bot(monkeypatch=monkeypatch)
        assert _run(b, _round(), "Down") is False          # the crossing arms
        assert b._exit_watch[SLUG]["flip_ms"] is not None
        return b

    def test_it_fires_with_no_second_crossing(self, monkeypatch):
        b = self._armed(monkeypatch)
        b._exit_watch[SLUG]["flip_ms"] -= 10_000           # the flip persisted
        assert _quiet_run(b, _round(), side="Down") is True
        assert len(b.broker.calls) == 1

    def test_it_does_not_fire_before_the_window(self, monkeypatch):
        b = self._armed(monkeypatch)
        for _ in range(5):
            assert _quiet_run(b, _round(), side="Down") is False
        assert b.broker.calls == [], "confirm_s has not run yet"

    def test_the_signal_coming_back_disarms_it(self, monkeypatch):
        b = self._armed(monkeypatch)
        assert _quiet_run(b, _round(), side="Up") is False  # back on our side
        assert b._exit_watch[SLUG]["flip_ms"] is None, (
            "the signal sits on the side we hold: the flip is over")
        b._exit_watch[SLUG]["flip_ms"] = None
        for _ in range(4):
            _quiet_run(b, _round(), side="Down")
        assert b.broker.calls == [], (
            "a disarmed watcher needs a fresh CROSSING to re-arm, not a state")

    def test_the_fire_check_is_not_inside_the_crossing_branch(self):
        """Structural, because the bug is invisible in any single-tick test:
        the fire must not be reachable only when this tick is a crossing."""
        src = (ROOT / "btcpredict" / "main.py").read_text()
        i = src.index("async def _cross_exit_check")
        j = src.index("def _confirm_pending_sale", i)
        body = src[i:j]
        assert "side_now" in body, "the exit must compute the STATE"
        # the arming is the only place `now_side` (the EVENT) may gate
        after = body[body.index("if w.get(\"flip_ms\") is None:"):]
        assert "now_side" not in after.split("# ---- ARMED")[-1], (
            "the fire path must not depend on a fresh crossing event")


class TestDustIsNotASale:
    """⛔ LIVE 2026-09-18 17:42, btc-updown-15m-1789752600.

        pm sell: sizing down 1.7693 -> 1.7692 shares to what the venue holds
        sale PLACED, confirmation pending
        "sale confirmed late -- booking 9.7e-05 sh @ 0.2900"

    The venue sold ALL 1.8 shares at 0.305 (+$0.54 on the account). The
    position list simply had not caught up -- it read 1.769103 against the
    1.7692 asked about -- and `left < shares - 1e-9` called that 9.7e-05
    difference a completed sale. The fill was split, the watcher stopped, and
    1.7692 shares stayed in the ledger for `settle_due` to pay out against the
    round's winner. Three exits died this way (15:02, 15:03, 17:42).

    `_trades_since` KNEW the real size; `sold = min(shares - left, traded)`
    threw it away.
    """

    def test_the_constant_exists_and_is_a_real_fraction(self):
        from btcpredict.venues.polymarket import broker as B
        assert 0.0 < B._MIN_SALE_FRACTION <= 1.0

    def test_a_dust_delta_is_not_confirmed_as_a_sale(self):
        from btcpredict.venues.polymarket.broker import PmBroker
        b = PmBroker.__new__(PmBroker)
        b._position_size_checked = lambda tok: 1.769103   # the list, behind
        b._trades_since = lambda tok, ts: (1.7692, 0.29, 0.0)
        got = PmBroker.confirm_sale(b, token_id="t", shares=1.7692,
                                    since_ts=0.0, fallback_px=0.29)
        # The trade record IS convincing, so the sale is confirmed in FULL --
        # what must never happen is booking the 9.7e-05 dust delta.
        assert got is not None
        assert got[0] > 1.7, f"booked {got[0]} shares, not the real sale"

    def test_dust_with_no_trade_record_is_unknown_not_a_sale(self):
        from btcpredict.venues.polymarket.broker import PmBroker
        b = PmBroker.__new__(PmBroker)
        b._position_size_checked = lambda tok: 1.769103
        b._trades_since = lambda tok, ts: None
        assert PmBroker.confirm_sale(b, token_id="t", shares=1.7692,
                                     since_ts=0.0) is None, (
            "neither source is convincing: ask again, do not book dust")

    def test_a_real_delta_still_confirms(self):
        from btcpredict.venues.polymarket.broker import PmBroker
        b = PmBroker.__new__(PmBroker)
        b._position_size_checked = lambda tok: 0.0        # genuinely gone
        b._trades_since = lambda tok, ts: (1.7692, 0.29, 0.01)
        got = PmBroker.confirm_sale(b, token_id="t", shares=1.7692,
                                    since_ts=0.0)
        assert got is not None and got[0] > 1.7


class TestTheConfirmationUsesWhatWasSent:
    """⛔ LIVE 2026-09-18 18:32, eth-updown-5m-1789756200 Down.

        OPEN CROSS ... stake=$1.26 @0.630          (ledger: 2 shares)
        pm sell: sizing down 2 -> 1.8181 shares to what the venue holds
        sale PLACED, confirmation pending
        "sale confirmed late -- booking 2 sh @ 0.2800"

    The venue had bought 1.818182 @ 0.55 and sold 1.810000 @ 0.31. The ORDER
    sized down correctly; the CONFIRMATION did not, because it asked
    `confirm_sale(shares=w["shares"])` -- the ledger's 2.0, inflated by the
    unverified entry path booking `shares` at `ceiling`. With `left = 0`,
    `sold = shares - left = 2.0`, `frac = 1.0`, so it wrote no split row and
    the 0.008182-share remainder became invisible to the bot while still
    sitting on-chain.
    """

    def test_the_broker_publishes_the_size_it_sent(self):
        src = (ROOT / "btcpredict" / "venues" / "polymarket"
               / "broker.py").read_text()
        i = src.index("def _sell_fak_body")
        j = src.index("def confirm_sale", i)
        assert "self.last_sell_size = float(sell_shares)" in src[i:j], (
            "the caller cannot confirm against a size it does not know")

    def test_pending_carries_the_sent_size_not_the_ledgers(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_confirm_s",
                            0.0)
        b = _bot(monkeypatch=monkeypatch, pending=True)
        b.broker.last_sell_size = 1.8181        # what the venue could take
        b._exit_watch[SLUG]["shares"] = 2.0     # what the ledger believes
        _fire(b, _round(), "Down")
        pend = b._exit_watch[SLUG]["pending"]
        assert len(pend) > 2, "the sent size must ride with the pending record"
        assert pend[2] == pytest.approx(1.8181), (
            f"confirming against {pend[2]} would book a full close")

    def test_the_confirmation_is_asked_about_the_sent_size(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_confirm_s",
                            0.0)
        b = _bot(monkeypatch=monkeypatch, pending=True,
                 confirm=(1.8181, 0.31, 0.0))
        b.broker.last_sell_size = 1.8181
        b._exit_watch[SLUG]["shares"] = 2.0
        _fire(b, _round(), "Down")               # places -> pending
        _run(b, _round(), "Down")                # confirms
        assert b.broker.confirms, "confirm_sale was never called"
        assert b.broker.confirms[0]["shares"] == pytest.approx(1.8181), (
            "asked about the ledger's 2.0 rather than the 1.8181 sent")


class TestTheFloorIsOnTheStateNotTheCrossing:
    """⛔ LIVE 2026-09-18 21:07:47, eth-updown-5m-1789765500 Up @0.600.

        21:08:04  crossed_side reads Down (logged "slot already used this
                  round") -- a real flip 17 s after the entry
        ...no `cross exit` line, ever. The position rode 0.600 -> 0.00.

    The arming condition required `past_floor` AT THE CROSSING. But
    `crossed_side` fires on the FIRST tick past the level, so a crossing is
    zero-margin BY CONSTRUCTION -- the entry's own note says so. The live log
    shows the flip at -0.30 bps against a 0.50 floor. The floor belongs on the
    STATE, once the signal has moved clear, not on the instant it crosses.
    """

    def test_a_zero_margin_crossing_still_arms(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        b = _bot(monkeypatch=monkeypatch)
        b._ck = lambda name, r: {"look_s": 1.0, "min_margin_bps": 0.5,
                                 "hold_s": 0.0}.get(name, 0.0)
        r = _round()
        ticks = TestTheHysteresisBand._marginal(bps=0.1)   # a real crossing, 0.1 bps
        b._cross_signal = lambda r, t, tau: (ticks, "spot")
        assert asyncio.run(b._cross_exit_check(r, 100.0, ticks)) is False
        assert b._exit_watch[SLUG]["flip_ms"] is not None, (
            "the crossing must arm; requiring the floor here arms never")
        assert b.broker.calls == [], "but it must not SELL inside the floor"

    def test_it_then_sells_once_the_state_clears_the_floor(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        b = _bot(monkeypatch=monkeypatch)
        b._ck = lambda name, r: {"look_s": 1.0, "min_margin_bps": 0.5,
                                 "hold_s": 0.0}.get(name, 0.0)
        r = _round()
        arm = TestTheHysteresisBand._marginal(bps=0.1)     # crossing, sub-floor
        b._cross_signal = lambda r, t, tau: (arm, "spot")
        asyncio.run(b._cross_exit_check(r, 100.0, arm))
        b._exit_watch[SLUG]["flip_ms"] -= 10_000           # the flip persisted
        # the signal has now moved clear of the level on the other side
        ticks, _ = _quiet(100.0, side="Down")
        b._cross_signal = lambda r, t, tau: (ticks, "spot")
        assert asyncio.run(b._cross_exit_check(r, 100.0, ticks)) is True
        assert len(b.broker.calls) == 1

    def test_the_arming_has_no_floor_in_the_source(self):
        """Structural: the fire path keeps the floor, the arm must not."""
        src = (ROOT / "btcpredict" / "main.py").read_text()
        i = src.index("async def _cross_exit_check")
        j = src.index("def _confirm_pending_sale", i)
        body = src[i:j]
        arm = body[body.index('if w.get("flip_ms") is None:'):]
        arm = arm.split("# ---- ARMED")[0]
        assert "past_floor" not in arm.split("w[\"flip_ms\"] = now_ms")[0].split(
            "if now_side is not None")[-1], (
            "a crossing is zero-margin by construction; a floor here never arms")


class TestThePartialSplitSettlesItsOwnSize:
    """⛔ LIVE 2026-09-18 21:33, btc-updown-5m-1789767000 Up.

        fid 984  sold 1.69 of 1.69491 @0.34  -> pnl -0.4777   (correct)
        fid 985  held 0.004914 sh, cost $0.0030
                 -> settled **-1.0287**, the FULL original cost
        round booked -1.5064 against a true -0.4807

    `settle_due` settles from the IN-MEMORY Fill, not the DB row, and the split
    handed the remainder `self.open_fills[fid][1]` -- the ORIGINAL object. The
    DB row carried the right stake all along; only the object passed to
    `settle()` was wrong, which is exactly why the ledger and the venue
    disagreed by roughly a whole stake on every exited round.
    """

    def test_the_remainder_fill_carries_the_remainder_size(self, monkeypatch):
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_confirm_s",
                            0.0)
        # sell only part of the 2.0 held -> a split must happen
        b = _bot(monkeypatch=monkeypatch, sell_shares=1.5, sell_px=0.40)
        orig = b.open_fills[7][1]
        assert _fire(b, _round(), "Down") is True
        new = [k for k in b.open_fills if k != 7]
        assert new, "a partial sale must leave a remainder fill open"
        rem = b.open_fills[new[0]][1]
        assert rem is not orig, (
            "the remainder must not reuse the original Fill -- settle_due reads "
            "this object, and the original carries the WHOLE position's stake")
        assert rem.shares == pytest.approx(0.5), rem.shares
        assert rem.stake < orig.stake, (
            f"remainder stake {rem.stake} must be below the original "
            f"{orig.stake} or the dust settles as the whole position")
        assert rem.fee_paid <= orig.fee_paid

    def test_the_original_object_is_not_mutated(self, monkeypatch):
        """`dataclasses.replace` copies; mutating in place would corrupt the
        settlement of the SOLD leg that is booked from the same object."""
        # The flip rule is still supported and still reachable, but it is no
        # longer the DEFAULT -- `cross_exit_margin_bps` ships at 4.0 and leaves
        # on the state alone. These tests exercise the event-armed path, so
        # they pin the knob to 0 rather than depending on a default that moved.
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_margin_bps",
                            0.0)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_confirm_s",
                            0.0)
        b = _bot(monkeypatch=monkeypatch, sell_shares=1.5, sell_px=0.40)
        orig = b.open_fills[7][1]
        before = (orig.shares, orig.stake, orig.fee_paid)
        _fire(b, _round(), "Down")
        assert (orig.shares, orig.stake, orig.fee_paid) == before


# ---------------------------------------------------------------------------
# THE MARGIN RULE -- 2026-09-19
# ---------------------------------------------------------------------------

class TestTheMarginExitRule:
    """Leave on the signal STATE, with no crossing event required.

    That one difference is what separates a rule that loses from one that wins:
    a SUSTAINED flip produces exactly ONE crossing, so the event-armed rule
    waits for a second that never comes. Seven rules on identical entries,
    slots and sale mechanics -- off TEST $+19.17 (win 63.1%), flip 3.0 TEST
    $+27.05 (43.9%), margin 4.0 TEST $+39.46 (57.1%).
    """

    def test_it_ships_the_threshold_that_survives_the_sale_not_landing(self):
        from btcpredict.config import Settings
        s = Settings()
        assert s.cross_exit_margin_bps == 4.0
        assert s.cross_exit_min_tau_s == 60.0

    def test_zero_restores_the_event_armed_rule(self):
        """0 = OFF is this repo's convention, and here OFF must mean "use the
        flip rule", not "never exit" -- the flip path has to stay reachable."""
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "btcpredict" / "main.py").read_text()
        i = src.index('xb = float(settings.cross_exit_margin_bps)')
        blk = src[i:i + 400]
        assert "if xb > 0:" in blk
        assert 'if w.get("flip_ms") is None:' in src[i:i + 1200], (
            "the event-armed rule must still follow it")

    def test_it_fires_on_state_alone_without_waiting_for_a_crossing(self):
        """The whole point: no `crossed_side` event in the fire condition."""
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "btcpredict" / "main.py").read_text()
        i = src.index('xb = float(settings.cross_exit_margin_bps)')
        blk = src[i:src.index('if w.get("flip_ms") is None:', i)]
        assert "side_now == held or abs(margin) < xb" in blk
        assert "now_side" not in blk, (
            "the margin rule must not consult the crossing EVENT at all")

    def test_the_execution_sensitivity_is_recorded_beside_the_knob(self):
        """4.0 ships over the 0.5 the search ranked first, and the reason is
        that it halves the exposure to a sale that does not land. If that table
        is lost, someone will 'optimise' the threshold back down."""
        cfg = (pathlib.Path(__file__).resolve().parents[1]
               / "btcpredict" / "config.py").read_text()
        j = cfg.index("cross_exit_margin_bps: float =")
        block = cfg[max(0, j - 2600):j]
        assert "0.39" in block and "0.88" in block, "the rate sweep must ride along"
        assert "CROSS_EXIT_MARGIN_BPS=0" in block, "the revert must be stated"


class TestTheMinimumHold:
    """A position must be held a few seconds before the exit may judge it.

    The decision and the FILL are a median 1.03 s apart live (p90 1.79 s,
    n=30) because a missed FAK re-prices and retries, so a position is born on
    a signal already a second old. Judging it immediately judges the latency.
    """

    def test_it_ships_the_measured_guard(self):
        from btcpredict.config import Settings
        assert Settings().cross_exit_min_hold_s == 5.0

    def test_it_is_measured_from_the_fill_not_the_decision(self):
        """`entry_ms` is the FILL's timestamp -- that is the whole point, and a
        future refactor that points it at the decision would silently undo
        this guard."""
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "btcpredict" / "main.py").read_text()
        i = src.index("hold_ms = int(settings.cross_exit_min_hold_s * 1000)")
        blk = src[i:i + 400]
        assert 'w.get("entry_ms")' in blk
        assert "now_ms - ems < hold_ms" in blk
        j = src.index('"entry_ms": int(entry_ms or 0)')
        arm = src[max(0, j - 1200):j]
        assert "entry_ms=fill.ts_ms" in src, "entry_ms must come from the FILL"

    def test_it_guards_BOTH_exit_rules(self):
        """The failure is generic -- a sale seconds after the fill -- so the
        guard sits before the rule choice, not inside the margin branch."""
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "btcpredict" / "main.py").read_text()
        hold = src.index("hold_ms = int(settings.cross_exit_min_hold_s * 1000)")
        margin = src.index("xb = float(settings.cross_exit_margin_bps)")
        flip = src.index('if w.get("flip_ms") is None:', margin)
        assert hold < margin < flip, (
            "the hold must precede both the margin rule and the flip rule")

    def test_zero_turns_it_off(self):
        """0 = off is the repo convention and the stated revert."""
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "btcpredict" / "main.py").read_text()
        i = src.index("hold_ms = int(settings.cross_exit_min_hold_s * 1000)")
        assert "if hold_ms > 0:" in src[i:i + 200]

    def test_a_fresh_position_is_not_sold(self, monkeypatch):
        """The behaviour, not just the source: a position filled moments ago
        must survive a signal that would otherwise sell it."""
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        b = _bot(monkeypatch=monkeypatch)
        b._exit_watch[SLUG]["entry_ms"] = int(time.time() * 1000) - 500
        assert _run(b, _round(), "Down") is False
        assert b.broker.calls == [], "nothing may be sold inside the hold"

    def test_and_is_sold_once_the_hold_has_passed(self, monkeypatch):
        """...and the same signal must sell it once the guard expires, or the
        guard is a mute button rather than a delay."""
        monkeypatch.setattr("btcpredict.config.settings.cross_exit_enabled",
                            True)
        b = _bot(monkeypatch=monkeypatch)
        b._exit_watch[SLUG]["entry_ms"] = int(time.time() * 1000) - 60_000
        _run(b, _round(), "Down")
        assert b.broker.calls, "past the hold the exit must be free to act"

    def test_the_losing_bucket_is_recorded_beside_the_knob(self):
        """It ships on n=7, so the reason has to travel with it -- otherwise it
        reads as an unjustified delay and gets tuned away."""
        cfg = (pathlib.Path(__file__).resolve().parents[1]
               / "btcpredict" / "config.py").read_text()
        j = cfg.index("cross_exit_min_hold_s: float =")
        block = cfg[max(0, j - 2200):j]
        assert "-2.38" in block, "the losing 0-5s bucket must be stated"
        assert "-8.07" in block or "-1.70" in block, "the upper bound too"
        assert "CROSS_EXIT_MIN_HOLD_S=0" in block, "the revert must be stated"
