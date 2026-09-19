"""Buying a SETTLED winner the book has not repriced.

WHY THIS ARM EXISTS. After the bell the 60s TWAP window is fully printed, so
the outcome is deterministic -- but venue resolution is not instant and the
book keeps trading. Measured on the recorded tape, post-bell rows with a
genuinely fresh book:

    book_age < 2000ms  27271 rows   88 chances to buy the winner < 0.99
    book_age < 1000ms  17801 rows   52 chances   mean ask 0.8223
    book_age <  500ms   9195 rows   30 chances   mean ask 0.8050

⭐ THE FRESHNESS GUARD IS THE WHOLE TEST. On the SAME tape the complete-set
arb (`up_ask + down_ask < 1`) showed 408 apparent instances -- and exactly
ZERO once a fresh book was required. Every one was a stale frame. This edge
does not vanish under that filter, which is the only reason it is being built;
if the guard is ever removed, the arm will start "finding" the phantom kind.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from btcpredict.config import Settings
from btcpredict.main import Bot


def _round(end_ms=1_000_000, start_ms=700_000):
    return NS(slug="btc-updown-5m-1", start_ms=start_ms, end_ms=end_ms,
              strike=100.0, market_id=7,
              outcomes=[NS(name="Up", token_id="UP"),
                        NS(name="Down", token_id="DOWN")])


class _Book:
    def __init__(self, ask, max_age_seen=None):
        self._ask, self.seen = ask, []

    def best(self, r, max_age_s):
        self.seen.append(max_age_s)
        a = self._ask
        return None if a is None else NS(ask=lambda side: a)


def _bot(monkeypatch, *, ask=0.85, winner=("Up", 4.0), scan=30.0, bankroll=50.0):
    monkeypatch.setattr("btcpredict.config.settings.post_close_scan_s", scan)
    b = Bot.__new__(Bot)
    b.mode = "live"
    b.bankroll = bankroll
    b.broker = NS(mode="live")
    b.ws_book = _Book(ask)
    b._post_close = {}
    b.buys = []

    async def buy(**k):
        b.buys.append(k)
        return NS(price=k["price"])
    b.broker.buy = buy
    b._settled_winner = lambda r, sym: winner
    b._ck = lambda name, r=None: 1.0
    return b


class TestTheKnob:
    def test_it_ships_off(self) -> None:
        assert Settings.model_fields["post_close_scan_s"].default == 0.0

    def test_off_means_the_scan_does_nothing(self, monkeypatch) -> None:
        b = _bot(monkeypatch, scan=0.0)
        b._post_close["btc-updown-5m-1"] = (_round(), "BTCUSDT")
        asyncio.run(b._post_close_scan())
        assert b.buys == []


class TestTheGuards:
    def _run(self, b, now_ms):
        import btcpredict.main as m
        real = m.time.time
        m.time.time = lambda: now_ms / 1000.0
        try:
            asyncio.run(b._post_close_scan())
        finally:
            m.time.time = real

    def test_a_settled_winner_under_the_cap_is_bought(self, monkeypatch) -> None:
        b = _bot(monkeypatch, ask=0.85)
        b._post_close["btc-updown-5m-1"] = (_round(), "BTCUSDT")
        self._run(b, 1_005_000)                       # 5s past the bell
        assert len(b.buys) == 1
        assert b.buys[0]["side"] == "Up"
        assert b.buys[0]["token_id"] == "UP"
        assert b.buys[0]["price"] == pytest.approx(0.85)

    def test_it_will_not_pay_above_the_cap(self, monkeypatch) -> None:
        """Above `post_close_max_px` the book has already repriced and there
        is nothing left to take."""
        monkeypatch.setattr("btcpredict.config.settings.post_close_max_px", 0.97)
        b = _bot(monkeypatch, ask=0.98)
        b._post_close["btc-updown-5m-1"] = (_round(), "BTCUSDT")
        self._run(b, 1_005_000)
        assert b.buys == []

    def test_a_near_tie_is_refused(self, monkeypatch) -> None:
        """⛔ Our reconstruction agrees with the venue on 98.6% of rounds, and
        the 1.4% it gets wrong is concentrated where the two TWAPs nearly
        touch. A near-tie is exactly where we must not bet on our own
        arithmetic."""
        monkeypatch.setattr(
            "btcpredict.config.settings.post_close_min_margin_bps", 1.0)
        b = _bot(monkeypatch, ask=0.85, winner=("Up", 0.2))
        b._post_close["btc-updown-5m-1"] = (_round(), "BTCUSDT")
        self._run(b, 1_005_000)
        assert b.buys == []

    def test_an_unreconstructable_round_is_refused(self, monkeypatch) -> None:
        """No tape, no bet. The trade rests entirely on knowing the outcome."""
        b = _bot(monkeypatch, ask=0.85, winner=None)
        b._post_close["btc-updown-5m-1"] = (_round(), "BTCUSDT")
        self._run(b, 1_005_000)
        assert b.buys == []

    def test_the_book_freshness_bound_is_what_is_passed_down(self, monkeypatch) -> None:
        """⭐ THE GUARD THE WHOLE ARM RESTS ON. The complete-set arb showed 408
        instances on this tape and ZERO with a fresh book. If this stops being
        enforced, the arm starts buying phantoms."""
        monkeypatch.setattr(
            "btcpredict.config.settings.post_close_max_book_age_ms", 500.0)
        b = _bot(monkeypatch, ask=0.85)
        b._post_close["btc-updown-5m-1"] = (_round(), "BTCUSDT")
        self._run(b, 1_005_000)
        assert b.ws_book.seen == [0.5], "the scan must demand a FRESH book"

    def test_a_stale_book_yields_nothing(self, monkeypatch) -> None:
        b = _bot(monkeypatch, ask=None)               # best() returns None
        b._post_close["btc-updown-5m-1"] = (_round(), "BTCUSDT")
        self._run(b, 1_005_000)
        assert b.buys == []

    def test_outside_the_window_the_round_is_dropped(self, monkeypatch) -> None:
        b = _bot(monkeypatch, ask=0.85, scan=30.0)
        b._post_close["btc-updown-5m-1"] = (_round(), "BTCUSDT")
        self._run(b, 1_100_000)                       # 100s past the bell
        assert b.buys == []
        assert b._post_close == {}, "a round past the window must be released"

    def test_before_the_bell_it_does_not_fire(self, monkeypatch) -> None:
        """Before settlement this is just a directional bet at a bad price."""
        b = _bot(monkeypatch, ask=0.85)
        b._post_close["btc-updown-5m-1"] = (_round(), "BTCUSDT")
        self._run(b, 999_000)                         # 1s BEFORE the bell
        assert b.buys == []

    def test_one_attempt_per_round(self, monkeypatch) -> None:
        """A second buy would double the position on a settled outcome for no
        extra edge, and the first one already took the mispriced size."""
        b = _bot(monkeypatch, ask=0.85)
        b._post_close["btc-updown-5m-1"] = (_round(), "BTCUSDT")
        self._run(b, 1_005_000)
        self._run(b, 1_006_000)
        assert len(b.buys) == 1

    def test_paper_mode_never_touches_the_venue(self, monkeypatch) -> None:
        b = _bot(monkeypatch, ask=0.85)
        b.broker = NS(mode="paper", buy=b.broker.buy)
        b._post_close["btc-updown-5m-1"] = (_round(), "BTCUSDT")
        self._run(b, 1_005_000)
        assert b.buys == []

    def test_it_will_not_bet_money_it_does_not_have(self, monkeypatch) -> None:
        b = _bot(monkeypatch, ask=0.85, bankroll=0.10)
        b._post_close["btc-updown-5m-1"] = (_round(), "BTCUSDT")
        self._run(b, 1_005_000)
        assert b.buys == []
