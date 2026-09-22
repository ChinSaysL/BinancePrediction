"""RETRY NOW: the one button that places a real order.

WHY THIS FILE EXISTS SEPARATELY. `tests/test_telegram_notify.py` proves the
BUTTON is wired, which it does with a stub handler -- so it would pass just as
happily if `Bot.retry_entry` placed nothing, placed the wrong side, placed the
same round twice, or blew up on the first tap. This file is about the ACTION.

The `decisions` table below is REAL sqlite, not a stub, because the interesting
part of `_decision_id_for` is its WHERE clause: the round it runs in holds
several rows for one slug+side (earlier refusals, the attempt, and later
non-attempting polls), and selecting the wrong one hands the button a stake of
0.0 and a stale side.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from types import SimpleNamespace

import pytest

from btcpredict.config import settings
from btcpredict.main import Bot

SLUG = "btc-updown-5m-1790076600"
PREFIX = "btc-updown-5m"

DECISIONS_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL, ts_ms INTEGER NOT NULL, tau_s REAL, side TEXT,
    fair REAL, quote REAL, edge REAL, hurdle REAL, sigma_1m REAL, spot REAL,
    oracle REAL, strike REAL, stake REAL, acted INTEGER, reason TEXT)
"""

#: slug, ts_ms, tau_s, side, fair, quote, edge, hurdle, sigma_1m, spot,
#: oracle, strike, stake, acted, reason
ATTEMPT = (SLUG, 1790076420000, 178.0, "Up", 0.74, 0.613, 0.148, 0.04,
           0.00048, 86012.5, 86012.5, 85975.0, 1.88, 0, "edge 0.148")
LATER_REFUSAL = (SLUG, 1790076430000, 170.0, "Up", 0.71, 0.640, 0.070, 0.04,
                 0.00048, 86010.0, 86010.0, 85975.0, 0.0, 0, "spot stale (4.1s)")


def _db(rows=()):
    con = sqlite3.connect(":memory:")
    con.executescript(DECISIONS_DDL)
    for r in rows:
        con.execute(
            "INSERT INTO decisions (slug, ts_ms, tau_s, side, fair, quote, edge,"
            " hurdle, sigma_1m, spot, oracle, strike, stake, acted, reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
    con.commit()
    return con


class Outcome:
    def __init__(self, name, token_id):
        self.name, self.token_id = name, token_id


def _round(slug=SLUG, tau_s=120.0):
    return SimpleNamespace(
        slug=slug, market_id=12345, end_ms=int(time.time() * 1000 + tau_s * 1000),
        outcomes=[Outcome("Up", "tok-up"), Outcome("Down", "tok-dn")])


@pytest.fixture(autouse=True)
def _pin(monkeypatch):
    monkeypatch.setattr(settings, "order_min_tau_s", 5.0)
    monkeypatch.setattr(settings, "min_order_usdt", 1.0)
    monkeypatch.setattr(settings, "min_quote", 0.25)
    monkeypatch.setattr(settings, "max_quote", 0.70)


def make_bot(*, rows=(ATTEMPT,), rnd=None, held=frozenset(), price=0.60,
             fill=True, live=True, guard=(True, "ok"), stopping=False,
             outcomes=True):
    """A `Bot` carrying only what `retry_entry` touches."""
    bot = Bot.__new__(Bot)
    bot.mode = "live"
    bot.store = SimpleNamespace(conn=_db(rows))
    bot.buys, bot.booked, bot.venue_reads = [], [], []
    bot.broker = SimpleNamespace(mode="live" if live else "paper",
                                 last_failure=None)
    bot._stop = SimpleNamespace(is_set=lambda: stopping)
    bot.guard = SimpleNamespace(check=lambda slug: guard)
    rr = rnd if rnd is not None else _round()
    if not outcomes:
        rr.outcomes = []
    bot._mkt = {PREFIX: rr}

    async def _held(*, max_age_s=None):
        bot.venue_reads.append(max_age_s)
        return None if held is None else set(held)

    async def buy(**k):
        bot.buys.append(k)
        if not fill:
            bot.broker.last_failure = "insufficient_balance"
            return None
        return SimpleNamespace(shares=3.1, filled_px=k["price"],
                               requested_px=k["price"], stake=k["stake"],
                               side=k["side"], fee_paid=0.01, order_id="oid",
                               ts_ms=1, mode="live", token_id=k["token_id"],
                               raw={}, pending=False, unverified=False)

    async def _bp(token, market_id):
        return (round(price - 0.01, 4), price)

    async def _book_fill(r, sig, fill, **k):
        bot.booked.append((r, sig, fill, k))

    bot._venue_position_tokens = _held
    bot.broker.buy = buy
    bot.book = SimpleNamespace(best_prices=_bp)
    bot._book_fill = _book_fill
    return bot


def retry(bot, did=1):
    return asyncio.run(bot.retry_entry(did))


# -- the happy path ---------------------------------------------------------

def test_a_retry_places_the_same_side_as_a_market_order_and_books_it():
    bot = make_bot()
    out = retry(bot)
    assert "placed Up 3.1" in out, out
    assert len(bot.buys) == 1
    k = bot.buys[0]
    assert k["side"] == "Up", "the button must not re-decide the side"
    assert k["stake"] == pytest.approx(1.88), "the decision's own stake"
    assert k["token_id"] == "tok-up"
    assert k["price"] == pytest.approx(0.60)
    # A MARKET order with NO ceiling is the same side at ANY price.
    assert k["max_px"] == pytest.approx(0.70), "the FOK must carry a ceiling"
    # Booked through the ordinary path, so the guard slot, the ledger, the exit
    # watcher and the ENTRY notification all behave as for any other fill.
    assert len(bot.booked) == 1
    _r, sig, _fill, kw = bot.booked[0]
    assert kw["did"] == 1 and kw["arm"] == "model"
    assert kw["token"] == "tok-up"
    assert sig.side == "Up" and "manual retry" in sig.reason


def test_the_retry_leaves_the_guard_claim_to_book_fill():
    """`retry_entry` must not claim a slot itself.

    `_book_fill` claims via `guard.on_open`, and that claim is what the exit
    watcher and settlement release. Claiming here as well would double it -- and
    for the reserved pool `reserved_open` is an INT COUNTER, not a set, so a
    double claim never comes back down and the arm silently loses a slot for
    the life of the process.
    """
    bot = make_bot()
    claimed = []
    bot.guard.on_open = lambda slug, **k: claimed.append(slug)
    retry(bot)
    assert bot.booked, "the fill was not booked at all -- nothing else ran"
    assert claimed == [], f"retry_entry claimed the slot itself: {claimed}"


# -- the refusals that protect money ----------------------------------------

def test_it_refuses_when_the_venue_already_holds_the_rounds_token():
    """The one irreversible mistake available from this method."""
    bot = make_bot(held={"tok-up"})
    out = retry(bot)
    assert "already holds Up" in out, out
    assert bot.buys == [], "a double buy is the worst possible outcome here"


def test_it_refuses_when_the_position_list_cannot_be_read():
    """An unreadable list is the ABSENCE of evidence, not evidence of nothing."""
    bot = make_bot(held=None)
    out = retry(bot)
    assert "cannot read the venue's positions" in out, out
    assert bot.buys == []


def test_it_forces_a_FRESH_position_read_rather_than_the_cached_one():
    """The cache exists to keep 60ms off the order path on every poll; a manual
    money-moving action should not inherit its staleness."""
    bot = make_bot()
    retry(bot)
    assert bot.venue_reads == [0.0], bot.venue_reads


def test_it_respects_a_risk_halt_instead_of_overruling_it():
    """A button must not push a trade through the daily loss cap."""
    bot = make_bot(guard=(False, "halted: daily loss cap 5.00 hit"))
    out = retry(bot)
    assert "refused: halted" in out, out
    assert bot.buys == []


def test_it_respects_the_open_rounds_bound():
    bot = make_bot(guard=(False, "max open rounds (6)"))
    out = retry(bot)
    assert "max open rounds" in out and bot.buys == []


def test_it_refuses_an_ask_outside_the_traded_band():
    """slippage_bps is 300 (3%), so without this a drifted book fills up to 3%
    through a quote the arm's own band would have refused."""
    bot = make_bot(price=0.95)
    out = retry(bot)
    assert "outside the traded band" in out, out
    assert bot.buys == []


def test_it_refuses_the_bottom_of_the_band_too():
    bot = make_bot(price=0.10)
    out = retry(bot)
    assert "outside the traded band" in out and bot.buys == []


def test_it_refuses_once_the_round_has_rolled():
    """The market in the decision no longer exists."""
    rolled = _round(slug="btc-updown-5m-1790076900")
    bot = make_bot(rnd=rolled)
    out = retry(bot)
    assert "no longer the open round" in out, out
    assert bot.buys == []


def test_it_refuses_an_empty_round_cache():
    bot = make_bot()
    bot._mkt = {}
    out = retry(bot)
    assert "no longer the open round" in out and bot.buys == []


def test_it_refuses_inside_the_tau_floor():
    bot = make_bot(rnd=_round(tau_s=2.0))
    out = retry(bot)
    assert "only 2s left" in out, out
    assert bot.buys == []


def test_it_refuses_while_the_bot_is_shutting_down():
    """A fill placed during shutdown risks an order the process never books."""
    bot = make_bot(stopping=True)
    out = retry(bot)
    assert "shutting down" in out and bot.buys == []


def test_it_refuses_in_paper_mode():
    bot = make_bot(live=False)
    out = retry(bot)
    assert "not in live mode" in out and bot.buys == []


def test_it_refuses_a_stake_under_the_venue_minimum():
    tiny = ATTEMPT[:12] + (0.50,) + ATTEMPT[13:]
    bot = make_bot(rows=(tiny,))
    out = retry(bot)
    assert "under the venue minimum" in out and bot.buys == []


def test_it_refuses_a_decision_with_no_side():
    none_side = ATTEMPT[:3] + ("NONE",) + ATTEMPT[4:]
    bot = make_bot(rows=(none_side,))
    out = retry(bot)
    assert "no side" in out and bot.buys == []


def test_it_reports_an_unknown_decision_without_raising():
    bot = make_bot()
    assert "not found" in retry(bot, did=999999)
    assert bot.buys == []


def test_it_reports_a_failed_order_a_second_time():
    bot = make_bot(fill=False)
    out = retry(bot)
    assert "still failed" in out and "not enough USDT" not in out
    assert bot.booked == []


def test_it_refuses_when_there_is_no_ask():
    bot = make_bot()
    bot.book = None
    out = retry(bot)
    assert "no ask to take" in out and bot.buys == []


def test_it_refuses_when_the_round_carries_no_token():
    bot = make_bot(outcomes=False)
    out = retry(bot)
    assert "no Up token" in out and bot.buys == []


# -- which decision row the button points at --------------------------------

def test_the_decision_lookup_skips_a_later_non_attempting_row():
    """The failure happens mid-round, and by then the round holds later rows
    with `acted=0` and no size. Picking one would hand the button stake $0.00."""
    bot = make_bot(rows=(ATTEMPT, LATER_REFUSAL))
    assert bot._decision_id_for(SLUG, "Up") == 1


def test_the_decision_lookup_is_empty_when_nothing_was_ever_attempted():
    bot = make_bot(rows=(LATER_REFUSAL,))
    assert bot._decision_id_for(SLUG, "Up") is None


def test_the_decision_lookup_is_scoped_to_the_side():
    down = ATTEMPT[:3] + ("Down",) + ATTEMPT[4:]
    bot = make_bot(rows=(down,))
    assert bot._decision_id_for(SLUG, "Up") is None
    assert bot._decision_id_for(SLUG, "Down") == 1


def test_the_decision_lookup_never_raises_on_a_dead_store():
    """It runs inside the order path; a missing button must degrade to a
    text-only notification, never to an exception."""
    bot = make_bot()
    bot.store = SimpleNamespace(conn=None)
    assert bot._decision_id_for(SLUG, "Up") is None
