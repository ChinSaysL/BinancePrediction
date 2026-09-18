"""The underlying must come from the SLUG, never from `r.symbol`.

THE INCIDENT, live 2026-09-16 21:59:34:

    cross bnb-updown-5m-1789595700 Down not taken: round already moved
    1109.7bps, gate is 7.8

A 5-minute BNB round cannot move 11.1%. The recorded data agrees: across that
round BNB's spot ran 720.50-721.17, i.e. 9.4bps of travel on a 720.925 strike.

The number was BTC's. The round object reaching the arm was stamped
"BTCUSDT" -- `PmRoundSource`/`PmMarketFeed` take `symbol=DEFAULT_SYMBOL` and
nothing constructs them per family -- so every per-symbol lookup keyed on
`r.symbol` read BTC's tick buffer. BTC had travelled $80.00 that round:

    1e4 * 80.00 / 720.925  = 1109.7   <- the bnb message
    1e4 * 80.00 / 2405.835 =  332.5   <- the eth message, same second

Both arms refused on it. The range gate is a max, so a 10-100x overstatement
of the range refuses EVERY round: the families added on 2026-09-16 could not
trade at all, and neither could `_train_is_fresh`/`_train_age_ms`, which read
the same buffer and would compare BTC's price to a 720.925 strike.

These tests pin the resolution rule and the two numbers above.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from btcpredict.config import SYMBOL_BY_PREFIX
from btcpredict.main import Bot, _stamp_symbol, symbol_for_round

#: The live bnb round, to the cent.
BNB_SLUG = "bnb-updown-5m-1789595700"
BNB_STRIKE = 720.925
ETH_SLUG = "eth-updown-5m-1789595700"
ETH_STRIKE = 2405.835
BTC_RANGE = 80.00          # BTC's own travel that round, in dollars
BTC_STRIKE = 76019.115


def _round(slug=BNB_SLUG, strike=BNB_STRIKE, symbol="BTCUSDT", start_ago=300):
    """A round as the venue's source hands it over: slug right, symbol BTC."""
    return SimpleNamespace(slug=slug, strike=strike, symbol=symbol,
                           start_ms=int(time.time() * 1000) - start_ago * 1000)


def _ticks(*specs):
    now = int(time.time() * 1000)
    return [(now - ago, px) for ago, px in specs]


def test_symbol_comes_from_the_slug_not_the_field():
    assert symbol_for_round(_round()) == "BNBUSDT"
    assert symbol_for_round(_round(slug=ETH_SLUG, strike=ETH_STRIKE)) == "ETHUSDT"
    for prefix, sym in SYMBOL_BY_PREFIX.items():
        r = _round(slug="%s-1789595700" % prefix, symbol="BTCUSDT")
        assert symbol_for_round(r) == sym


def test_unknown_slug_still_falls_back_to_the_field():
    """Fixtures and single-market tests build rounds with no MARKETS slug."""
    r = SimpleNamespace(strike=100.0, symbol="BTCUSDT")
    assert symbol_for_round(r) == "BTCUSDT"
    r2 = SimpleNamespace(slug="xyz-updown-5m-1789595700", symbol="FOOUSDT",
                         strike=1.0)
    assert symbol_for_round(r2) == "FOOUSDT"


def _bot_with_both_buffers(bnb_px=720.5):
    bot = Bot.__new__(Bot)                       # skip network setup
    bot._ticks = {
        "BTCUSDT": _ticks((290, BTC_STRIKE - BTC_RANGE), (10, BTC_STRIKE)),
        "BNBUSDT": _ticks((290, bnb_px), (10, bnb_px + 0.68)),
    }
    return bot


def test_range_gate_reads_this_family_buffer():
    """The live regression: 9.4bps of travel, not 1109.7."""
    bot = _bot_with_both_buffers()
    got = bot._round_range_bps(_round())
    assert got == pytest.approx(9.4, abs=0.1)
    assert got < 20                       # anything near 1109 is the bug


def test_the_bug_signature_is_still_reachable_if_the_key_regresses():
    """Pin the arithmetic that produced 1109.7, so a regression is recognisable.

    This is what the wrong buffer computes: BTC's dollars over BNB's strike.
    It is the assertion above that must fail if the keying regresses, but
    having the number itself written down makes the log message identifiable.
    """
    assert 1e4 * BTC_RANGE / BNB_STRIKE == pytest.approx(1109.7, abs=0.1)
    assert 1e4 * BTC_RANGE / ETH_STRIKE == pytest.approx(332.5, abs=0.1)
    # ...and BTC's own read, which is the one that was always correct.
    assert 1e4 * BTC_RANGE / BTC_STRIKE == pytest.approx(10.5, abs=0.1)


def test_freshness_uses_this_family_buffer():
    """BTC sitting 1000s of bps from a BNB strike must not veto BNB."""
    bot = _bot_with_both_buffers(bnb_px=BNB_STRIKE)       # in-band BNB sample
    assert bot._train_is_fresh(_round(), 15.0) is True


def test_age_is_measured_against_this_family_strike():
    bot = _bot_with_both_buffers(bnb_px=BNB_STRIKE)
    age = bot._train_age_ms(_round(), 15.0)
    assert age is not None and age < 60_000


def test_stamp_symbol_fixes_the_round_at_the_source():
    r = _round()
    assert r.symbol == "BTCUSDT"
    assert _stamp_symbol(r, "bnb-updown-5m") is r
    assert r.symbol == "BNBUSDT"


def test_stamp_symbol_tolerates_a_frozen_round():
    """Never let a book-keeping fix raise into the poll loop."""
    class Frozen:
        __slots__ = ()

        @property
        def symbol(self):
            return "BTCUSDT"

    assert _stamp_symbol(Frozen(), "bnb-updown-5m") is not None


def test_every_per_symbol_lookup_goes_through_the_helper():
    """Source guard: no `_ticks` read may key off `r.symbol` again.

    One occurrence of `getattr(r, "symbol", "")` is legitimate -- the fallback
    INSIDE `symbol_for_round`, which is what keeps fixtures working. Anything
    more means a reader is bypassing the helper again.
    """
    import pathlib
    src = pathlib.Path("btcpredict/main.py").read_text()
    assert src.count('getattr(r, "symbol", "")') == 1
    # `_round_range_bps`, `_train_is_fresh`, `_train_age_ms`, `_cross_level`
    # and `_cross_signal` are the readers; every one of them asks the helper
    # for the symbol.
    assert src.count(").get(symbol_for_round(r)) or []") == 5
