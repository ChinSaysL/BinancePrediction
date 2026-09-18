"""The take price comes from the VENUE's book, not our ladder. Shipped 2026-09-18.

THE BUG THIS CLOSES. The arm decides on `Touch.sweep()` of its own websocket
book and then builds the FAK ceiling from that same number. When the ws ladder
holds a level the venue does not have, the ceiling sits BELOW the ask that
exists and the FAK cannot match -- and the miss looks like latency.

MEASURED on the 75 live order attempts of 09-16..09-18 (`data/live.log`), joined
to the shadow probe's read of the venue's own book:

    of the 20 NO-FILLs with a probe line, 17 had a venue ask ABOVE our 0.65 cap
    mean (venue ask - our limit) = +0.155
    `shadow book xrp-updown-5m-1789704600 Down: venue 0.3200/0.6900 |
     ours 0.3200/0.4200` -> decided 0.42, ceiling 0.54, "no orders found"

OUR `sweep()` READ 0.42 AT THE SAME INSTANT THE VENUE'S BOOK READ 0.69. The
ceiling was then built from the 0.42, so it could not reach a 0.69 ask. The miss
was arithmetic.

WHAT ACTUALLY BOUNDS THE FAK, and why the fix is where it is: on this venue the
order goes out as `place_market_order(..., max_price=p["ceiling"], order_type=
"FAK")` -- the ceiling is `max_px`, and `price` is NOT read by that path at all.
So re-basing the order's price is bookkeeping; re-basing the CEILING is the fix,
and `_take_cap_for` re-runs exactly the arithmetic `_cross_tick` used, against
the venue's ask instead of our sweep.

The retry is the loop that already exists (`order_max_attempts`, 3 sends per
decision, `order_retry_delay_s`, the `order_min_tau_s` floor and the
venue-position re-read that makes doubling impossible); what changes is that a
`fak_unfilled` no longer ends it. Only on the TAKE path: the model and impulse
arms pass no ceiling and keep the single-attempt behaviour.
"""
from __future__ import annotations

import asyncio
import pathlib
import types
from types import SimpleNamespace

import pytest

from btcpredict.config import settings
from btcpredict.main import Bot

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Top:
    """`PmBook.fetch_top`'s shape: `.bid`/`.ask`."""

    def __init__(self, bid: float, ask: float):
        self.bid, self.ask = bid, ask


def make_bot(*, failure="fak_unfilled", tau=90.0):
    """A `Bot` with just the surface `_place_order` touches on the take path."""
    bot = Bot.__new__(Bot)
    bot.mode = "live"
    bot.store = None
    bot.open_fills = {}
    bot._take_last = None
    bot._order_cooldown_until = 0.0
    bot._quote_pre = {}
    bot.refusals = []
    bot.buys = []
    bot.broker = SimpleNamespace(mode="live", last_failure=None)

    async def get_quote(*_a, **_k):
        return {"quoteId": "q", "averagePrice": 0.42, "expireAt": 0,
                "feeRateBps": 200}

    async def buy(**k):
        bot.buys.append(k)
        bot.broker.last_failure = failure
        return None

    async def held():
        return set()

    bot.broker.get_quote = get_quote
    bot.broker.buy = buy
    bot.broker.buy_fak = buy                      # only its NAME is probed
    bot._venue_position_tokens = held
    bot._cross_refuse = lambda r, side, reason, **k: bot.refusals.append(reason)
    r = SimpleNamespace(slug="btc-updown-5m-1789000000",
                        tau_s=lambda _ms: tau)
    sig = SimpleNamespace(side="Up", quote=0.42, fair=0.60, edge=0.1,
                          reason="cross")
    return bot, r, sig


def venue(seq, calls=None):
    """An async `venue_top` stub returning `Top` objects in sequence (last wins).

    `calls` collects how many times the venue was actually read -- the point of
    several tests is that it is NOT read.
    """
    items = list(seq)

    async def _top(_token):
        if calls is not None:
            calls.append(1)
        return items[0] if len(items) == 1 else items.pop(0)

    return _top


def place(bot, r, sig, monkeypatch, *, venue_top=None, ceiling=0.54,
          attempts=3, delay=0.0, venue_token="tok"):
    monkeypatch.setattr("btcpredict.config.settings.pm_order_type", "fak")
    monkeypatch.setattr("btcpredict.config.settings.order_type", "market")
    monkeypatch.setattr("btcpredict.config.settings.order_max_attempts", attempts)
    monkeypatch.setattr("btcpredict.config.settings.order_retry_delay_s", delay)
    monkeypatch.setattr("btcpredict.config.settings.trade_on_candle_open", False)
    if venue_top is not None:
        monkeypatch.setattr("btcpredict.main.venue_top", venue_top)
    asyncio.run(bot._place_order(
        r, sig, fair_up=0.40, token="tok", stake=1.0,
        skip_pretrade_gates=True, take_px=0.42, limit_ceiling=ceiling,
        venue_token=venue_token))


# ---------------------------------------------------------------------------
# the base
# ---------------------------------------------------------------------------

def test_the_ceiling_is_built_from_the_venue_ask_not_our_ladder(monkeypatch):
    """Our ladder 0.42, the venue 0.60: the ceiling must clear the venue.

    Under the old base this sent `max_px` 0.54 (0.42 + buffer) -- below the ask
    that existed, which is the measured miss. ONE attempt here so the price is
    the subject; the retry has its own tests below."""
    bot, r, sig = make_bot()
    place(bot, r, sig, monkeypatch, venue_top=venue([Top(0.30, 0.60)]),
          attempts=1)
    assert len(bot.buys) == 1
    k = bot.buys[0]
    assert k["max_px"] == pytest.approx(0.65), (
        "the ceiling must be re-derived from the venue's 0.60, capped at the "
        "fill cap 0.65")
    assert k["max_px"] > 0.54, "this is the number the old base produced"
    assert k["price"] == pytest.approx(0.60), (
        "the order is priced at the ask that exists, not at our stale sweep")


def test_a_venue_ask_above_the_cap_is_refused_before_the_venue_is_called(
        monkeypatch):
    """17 of the 20 probed misses were this case. It must be a REFUSAL with a
    decision row, not a FAK round-trip that cannot match."""
    bot, r, sig = make_bot()
    place(bot, r, sig, monkeypatch, venue_top=venue([Top(0.85, 0.90)]))
    assert bot.buys == [], "nothing may be sent: no take at <= 0.65 can match"
    assert bot.broker.last_failure == "above_fill_cap"
    assert len(bot.refusals) == 1
    assert "above the fill cap" in bot.refusals[0]
    assert "venue" in bot.refusals[0], (
        "the refusal must name the VENUE's book -- that is the number that "
        "differs from ours")


def test_an_unreadable_venue_book_keeps_the_old_base(monkeypatch):
    """A failed read is not a reason to refuse a trade the arm's own gates
    approved; it falls back to the behaviour that shipped before this."""
    bot, r, sig = make_bot()
    place(bot, r, sig, monkeypatch, venue_top=venue([None]), attempts=1)
    assert len(bot.buys) == 1
    assert bot.buys[0]["price"] == pytest.approx(0.42)
    assert bot.buys[0]["max_px"] == pytest.approx(0.54)


def test_the_model_path_never_reads_the_venue(monkeypatch):
    """No ceiling = not a take arm. One network call saved per model order, and
    the old behaviour byte for byte."""
    calls = []
    bot, r, sig = make_bot()
    place(bot, r, sig, monkeypatch, venue_top=venue([Top(0.30, 0.60)], calls),
          venue_token=None, ceiling=None, attempts=1)
    assert calls == [], "the venue book must not be read without a take ceiling"
    assert len(bot.buys) == 1
    assert bot.buys[0]["price"] == pytest.approx(0.42)
    assert "max_px" not in bot.buys[0], (
        "a ceiling is the take path's marker; without one nothing may bound "
        "the price")
    assert bot._take_last is None


def test_the_cap_arithmetic_is_the_arms_own_bound(monkeypatch):
    """`_take_cap_for` re-runs `_cross_tick`'s arithmetic -- max_price, the
    inverse buffer, the multiple bound, then the fill cap.

    ⛔ THE KNOBS ARE PINNED HERE. The first version of this test hardcoded the
    config DEFAULTS (buffer 0.25, inverse 0.125, cap 0.65) and passed locally
    while failing on the VPS, whose live `.env` sets `cross_limit_buffer_inv=0.2`
    -- the same value, different arithmetic (0.30 -> 0.44 there, 0.39 here).
    A test of the formula must not depend on which profile is deployed.
    """
    monkeypatch.setattr("btcpredict.config.settings.cross_limit_buffer", 0.25)
    monkeypatch.setattr("btcpredict.config.settings.cross_limit_buffer_inv", 0.125)
    monkeypatch.setattr("btcpredict.config.settings.cross_max_price", 0.65)
    monkeypatch.setattr("btcpredict.config.settings.cross_max_fill_px", 0.65)
    monkeypatch.setattr("btcpredict.config.settings.cross_max_fill_multiple", 0.0)
    bot, r, _sig = make_bot()
    assert bot._take_cap_for(r, 0.60, None) == pytest.approx(0.65)
    assert bot._take_cap_for(r, 0.30, None) == pytest.approx(0.39)
    assert bot._take_cap_for(r, 0.80, None) == pytest.approx(0.65)   # fill cap
    monkeypatch.setattr("btcpredict.config.settings.cross_max_price", 0.95)
    monkeypatch.setattr("btcpredict.config.settings.cross_max_fill_px", 0.95)
    assert bot._take_cap_for(r, 0.80, None) == pytest.approx(0.83), (
        "with the caps out of the way the inverse buffer is what is left")
    monkeypatch.setattr("btcpredict.config.settings.cross_max_fill_multiple", 1.5)
    assert bot._take_cap_for(r, 0.10, None) == pytest.approx(0.15), (
        "1.5 x 0.10 = 0.15 binds where the absolute buffer would give 0.21")


def test_the_fill_cap_override_still_wins(monkeypatch):
    """The train arm passes its own cap; the helper must honour it."""
    monkeypatch.setattr("btcpredict.config.settings.cross_limit_buffer", 0.25)
    monkeypatch.setattr("btcpredict.config.settings.cross_limit_buffer_inv", 0.125)
    monkeypatch.setattr("btcpredict.config.settings.cross_max_price", 0.95)
    monkeypatch.setattr("btcpredict.config.settings.cross_max_fill_px", 0.95)
    bot, r, _sig = make_bot()
    assert bot._take_cap_for(r, 0.80, 0.90) == pytest.approx(0.83)
    assert bot._take_cap_for(r, 0.80, 0.20) == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# the retry
# ---------------------------------------------------------------------------

def test_a_fak_that_finds_nothing_is_re_priced_and_re_sent(monkeypatch):
    """The venue moved between our read and its match: that is answerable, and
    the answer is a NEW read, not the same ceiling again."""
    bot, r, sig = make_bot()
    place(bot, r, sig, monkeypatch,
          venue_top=venue([Top(0.30, 0.60), Top(0.30, 0.62)]))
    assert len(bot.buys) == 3, (
        "all three attempts run: the venue keeps moving and the arm keeps "
        "re-reading (the stub holds 0.62 after the second read)")
    assert [k["price"] for k in bot.buys] == [0.60, 0.62, 0.62], (
        "each send must be priced off a FRESH read of the venue")
    assert all(k["max_px"] == pytest.approx(0.65) for k in bot.buys)


def test_the_number_of_sends_is_bounded_by_order_max_attempts(monkeypatch):
    bot, r, sig = make_bot()
    place(bot, r, sig, monkeypatch, venue_top=venue([Top(0.30, 0.60)]))
    assert len(bot.buys) == 3, (
        "`order_max_attempts` is the bound: 3 sends per decision")


def test_one_attempt_when_the_arm_is_configured_for_one(monkeypatch):
    bot, r, sig = make_bot()
    place(bot, r, sig, monkeypatch, venue_top=venue([Top(0.30, 0.60)]),
          attempts=1)
    assert len(bot.buys) == 1


def test_a_retry_does_not_start_into_a_nearly_over_round(monkeypatch):
    """The floor is the existing one, and it still applies: an attempt needs
    enough round left to land before the bell."""
    monkeypatch.setattr("btcpredict.config.settings.order_min_tau_s", 5.0)
    bot, r, sig = make_bot(tau=2.0)
    place(bot, r, sig, monkeypatch, venue_top=venue([Top(0.30, 0.60)]))
    assert len(bot.buys) == 1, "with 2s of round left only the first may run"


def test_an_unknown_outcome_still_stops_the_loop(monkeypatch):
    """Only a CONFIRMED no-match is retryable. A timeout may have left an order
    behind, and re-placing that is how exposure gets doubled."""
    bot, r, sig = make_bot(failure="unconfirmed")
    place(bot, r, sig, monkeypatch, venue_top=venue([Top(0.30, 0.60)]))
    assert len(bot.buys) == 1


def test_the_take_numbers_are_recorded_for_the_exec_line(monkeypatch):
    bot, r, sig = make_bot()
    place(bot, r, sig, monkeypatch, venue_top=venue([Top(0.30, 0.60)]))
    assert bot._take_last["src"] == "venue"
    assert bot._take_last["base"] == pytest.approx(0.60)
    assert bot._take_last["cap"] == pytest.approx(0.65)
    assert bot._take_last["attempts"] == 3

    bot2, r2, sig2 = make_bot()
    place(bot2, r2, sig2, monkeypatch, venue_top=venue([None]))
    assert bot2._take_last["src"] == "ours", (
        "the exec line must say which book priced the order")


def test_the_log_names_the_sent_ceiling_apart_from_our_own():
    """`limit` on the exec line is OUR ladder's ceiling; the order carried the
    venue-derived one. They differ by design (0.58 vs 0.65 on the 2026-09-18
    eth-15m fill) and the log must not make them look like one number."""
    src = (ROOT / "btcpredict" / "main.py").read_text()
    i = src.index('log.info("%s take %s %s: base %.4f')
    block = src[i:i + 400]
    assert "sent ceiling %.2f" in block
    assert "cap %.2f" not in block


# ---------------------------------------------------------------------------
# the wiring and the record
# ---------------------------------------------------------------------------

def test_the_executor_asks_for_the_venue_base_only_on_the_cross_path():
    src = (ROOT / "btcpredict" / "main.py").read_text()
    i = src.index("limit_ceiling=ceil_side, fill_cap=fill_cap,")
    block = src[i:i + 500]
    assert "venue_token=" in block
    assert 'self._ck("venue_take", r)' in block
    assert "not train" in block, (
        "the train arm's ceiling has its own arithmetic and must keep it")


def test_config_states_what_it_replaced_and_how_to_revert():
    cfg = (ROOT / "btcpredict" / "config.py").read_text()
    assert "cross_venue_take: bool = True" in cfg
    assert "CROSS_VENUE_TAKE=false" in cfg
    assert "17 had a venue ask above our 0.65 cap" in cfg
    assert "+0.155" in cfg
    assert settings.cross_venue_take is True


def test_the_order_path_fetch_is_time_bounded():
    """The probe can afford 3/4 s on its own task; the ORDER cannot."""
    src = (ROOT / "btcpredict" / "main.py").read_text()
    i = src.index("async def venue_top(")
    block = src[i:src.index("def ws_follow_rounds")]
    assert "connect=2.0, sock_read=2.0" in block
    assert "connect=3.0" not in block
